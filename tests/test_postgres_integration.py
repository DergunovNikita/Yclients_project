import os
from datetime import date, datetime, time

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import dashboard_service
import sync_orchestrator
import sync_worker
from database import Database, run_migrations
from sync_control import SyncControlService
from sync_jobs import SyncJobService
from models import (
    Appointment,
    Client,
    Company,
    Group,
    PlanStaffInput,
    Service,
    ServiceLabel,
    Staff,
    StaffSchedule,
    SyncJob,
    SyncSourceState,
    Transaction,
)


TEST_DATABASE_URL = os.getenv('TEST_DATABASE_URL')

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason='TEST_DATABASE_URL is not set')


@pytest.fixture
def pg_session_factory():
    engine = create_engine(TEST_DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA IF EXISTS public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
        conn.execute(text('DROP SCHEMA IF EXISTS system CASCADE'))
    run_migrations(TEST_DATABASE_URL)
    session_local = sessionmaker(bind=engine)
    try:
        yield session_local
    finally:
        engine.dispose()


def test_migration_creates_sync_jobs_and_typed_columns(pg_session_factory):
    session = pg_session_factory()
    try:
        result = session.execute(text("""
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'system' AND table_name = 'sync_jobs' AND column_name = 'requested_at'
        """)).scalar_one()
        assert result == 'timestamp without time zone'

        result = session.execute(text("""
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'appointments' AND column_name = 'date'
        """)).scalar_one()
        assert result == 'date'

        plan_settings_tables = session.execute(text("""
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
              AND table_name IN ('plan_branch_settings', 'plan_staff_inputs')
            ORDER BY table_name
        """)).scalars().all()
        assert plan_settings_tables == ['plan_branch_settings', 'plan_staff_inputs']

        plan_settings_indexes = session.execute(text("""
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname IN (
                'uq_plan_branch_setting_period_company',
                'uq_plan_staff_input_period_company_staff'
              )
            ORDER BY indexname
        """)).scalars().all()
        assert plan_settings_indexes == [
            'uq_plan_branch_setting_period_company',
            'uq_plan_staff_input_period_company_staff',
        ]

        schedule_index = session.execute(text("""
            SELECT indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND indexname = 'ix_staff_schedules_company_staff_date_slots'
        """)).scalar_one()
        assert schedule_index == 'ix_staff_schedules_company_staff_date_slots'
    finally:
        session.close()


def test_advisory_lock_prevents_parallel_runs(pg_session_factory):
    session_one = pg_session_factory()
    session_two = pg_session_factory()
    control = SyncControlService()
    try:
        assert control.acquire_lock(session_one) is True
        assert control.acquire_lock(session_two) is False
        control.release_lock(session_one)
        assert control.acquire_lock(session_two) is True
    finally:
        session_one.close()
        session_two.close()


def _bind_database_to_test_url() -> Database:
    url = make_url(TEST_DATABASE_URL)
    return Database(url.host, url.port or 5432, url.database, url.username, url.password or '')


def _create_full_schema(engine) -> None:
    """Fresh public+system schema via Base.metadata directly, not via Alembic.

    Alembic's baseline migration (0001) rebuilds public/system in two separate
    Base.metadata.create_all() calls filtered to a hardcoded table subset, against
    whatever models.py looks like *today* -- pre-existing and unrelated to this fix, but
    replaying the full migration chain against a genuinely empty database currently
    fails (a public table gets an FK to a system table migration 0001 doesn't yet
    create). Creating every table from the live models in one call sidesteps that: full
    metadata graph, correctly ordered by SQLAlchemy across all tables at once.
    """
    from models import Base

    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA IF EXISTS public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
        conn.execute(text('DROP SCHEMA IF EXISTS system CASCADE'))
        conn.execute(text('CREATE SCHEMA system'))
    Base.metadata.create_all(bind=engine)


def test_advisory_lock_survives_commit_churn_on_a_shared_pool():
    """pg_try_advisory_lock/pg_advisory_unlock are session-level: tied to one physical
    connection, not to a transaction. An ORM Session returns its connection to the pool
    on every commit(), so once a *second* session shares that same pool and holds a
    transaction open across several statements before its own commit (exactly how
    execute_sync's own pipeline session behaves now that it reuses run_sync_job's
    Database/pool), the two sessions' checkouts can swap: the second session ends up
    holding the exact connection that acquired the lock, and releasing the lock on the
    first session's *current* connection silently unlocks nothing.

    This reproduced 100% of the time against real Postgres before run_sync_job started
    pinning the lock to its own dedicated Connection instead of the ORM Session used for
    run/job bookkeeping. Assert the lock is actually gone afterward, not just that no
    exception was raised.
    """
    database = _bind_database_to_test_url()
    control = SyncControlService()
    lock_conn = database.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    control_db = database.get_db()
    pipeline_db = database.get_db()  # mimics execute_sync's own session on the shared pool
    try:
        assert control.acquire_lock(lock_conn) is True

        # Mimic run_sync_job's own churn (create_run + several set_state calls), then
        # many cycles of the pipeline holding a transaction open across two statements
        # before its commit, interleaved with more control_db commits in between (like
        # progress_callback firing mid-step) -- the exact pattern that swapped
        # connections when the lock lived on control_db instead of its own Connection.
        for _ in range(5):
            control_db.execute(text('SELECT 1'))
            control_db.commit()
        for _ in range(15):
            pipeline_db.execute(text('SELECT 1'))
            pipeline_db.execute(text('SELECT 1'))
            for _ in range(3):
                control_db.execute(text('SELECT 1'))
                control_db.commit()
            pipeline_db.commit()

        control.release_lock(lock_conn)
    finally:
        lock_conn.close()
        control_db.close()
        pipeline_db.close()
        database.engine.dispose()

    verify_engine_db = _bind_database_to_test_url()
    verify_session = verify_engine_db.get_db()
    try:
        assert control.acquire_lock(verify_session) is True, (
            'advisory lock is still held after release_lock() -- it was unlocked on the '
            'wrong connection'
        )
        control.release_lock(verify_session)
    finally:
        verify_session.close()
        verify_engine_db.engine.dispose()


def test_run_sync_job_releases_its_lock_when_execute_sync_shares_the_pool(monkeypatch, tmp_path):
    """End-to-end guard for the same bug via the real orchestrator entry point: a queued
    job (job_id set) is the routine automatic-schedule path, where progress_callback
    fires on every pipeline step and commits control_db each time. Stub execute_sync to
    do real multi-statement work on the *shared* database it receives, call
    progress_callback repeatedly (as every real step does), and confirm run_sync_job
    still hands back a clean lock afterward.
    """
    schema_db = _bind_database_to_test_url()
    _create_full_schema(schema_db.engine)
    schema_db.engine.dispose()

    # Production keeps this Database alive: init_database() stores it in database.py's
    # module-level `db_instance`, so its pooled connections are never closed. Hold a
    # reference here too — without it the Database is garbage-collected the moment
    # run_sync_job returns, the connections close, PostgreSQL drops the advisory lock with
    # them, and this test passes even when the lock was released on the wrong connection.
    held_databases = []

    def _held_database(*args, **kwargs):
        database = _bind_database_to_test_url()
        held_databases.append(database)
        return database

    monkeypatch.setattr(sync_orchestrator, 'init_database', _held_database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))

    def fake_execute_sync(*, progress_callback=None, database=None, **_kwargs):
        pipeline_db = database.get_db()
        try:
            for step in range(12):
                pipeline_db.execute(text('SELECT 1'))
                pipeline_db.execute(text('SELECT 1'))
                if progress_callback:
                    progress_callback({
                        'status': 'running',
                        'current_stage': f'step-{step}',
                        'progress_pct': step * 8,
                    })
                pipeline_db.commit()
        finally:
            pipeline_db.close()
        return {
            'success': True,
            'step_results': [{'name': 'fake', 'success': True}],
            'window_start': None,
            'window_end': None,
            'companies_count': 1,
        }

    monkeypatch.setattr(sync_orchestrator, 'execute_sync', fake_execute_sync)

    setup_db = _bind_database_to_test_url()
    setup_session = setup_db.get_db()
    try:
        job = SyncJobService().enqueue_job(setup_session, 'incremental', 'pytest')
        job_id = job.id
    finally:
        setup_session.close()
        setup_db.engine.dispose()

    result = sync_orchestrator.run_sync_job(
        mode='incremental',
        trigger_type='queued',
        initiator='pytest',
        job_id=job_id,
    )
    assert result['status'] == 'success'

    verify_db = _bind_database_to_test_url()
    verify_session = verify_db.get_db()
    control = SyncControlService()
    try:
        assert control.acquire_lock(verify_session) is True, (
            'run_sync_job left the advisory lock stuck on a connection its own '
            'release_lock() call never touched'
        )
        control.release_lock(verify_session)
    finally:
        verify_session.close()
        verify_db.engine.dispose()
        for database in held_databases:
            database.engine.dispose()


def test_worker_processes_queued_job(pg_session_factory, monkeypatch):
    service = SyncJobService()
    session = pg_session_factory()
    try:
        job = service.enqueue_job(session, 'incremental', 'pytest')
    finally:
        session.close()

    class BoundDatabase:
        def __init__(self, session_factory):
            self._session_factory = session_factory

        def get_db(self):
            return self._session_factory()

    monkeypatch.setattr(sync_worker, 'init_database', lambda *args, **kwargs: BoundDatabase(pg_session_factory))
    monkeypatch.setattr(sync_worker, 'run_sync_job', lambda **kwargs: {'status': 'success', 'run_id': 77})
    assert sync_worker.process_next_job() is True

    session = pg_session_factory()
    try:
        saved = session.get(SyncJob, job.id)
        assert saved.status == 'success'
        assert saved.run_id == 77
        assert saved.finished_at is not None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_postgres_administrator_attribution_uses_local_shifts_and_role_periods(
    pg_session_factory,
):
    async_url = make_url(TEST_DATABASE_URL).set(drivername='postgresql+asyncpg')
    engine = create_async_engine(async_url)
    try:
        async with AsyncSession(engine) as db:
            db.add_all([
                Group(id=1, title='G1'),
                Company(
                    id=1,
                    title='Salon',
                    group_id=1,
                    timezone='Europe/Moscow',
                    reporting_start_date=date(2025, 1, 1),
                ),
                Staff(id=1, name='Barber', position='Барбер', company_id=1),
                Staff(id=2, name='Role transition', position='Администратор', company_id=1),
                Client(
                    id=1,
                    name='Client',
                    company_id=1,
                    visits_count=1,
                    last_visit_date=date(2025, 1, 31),
                ),
                Service(id=10, title='Extra', company_id=1),
                ServiceLabel(
                    service_id=10,
                    company_id=1,
                    is_extra=True,
                    source='dashboard',
                    updated_at=datetime(2025, 1, 1),
                ),
                PlanStaffInput(
                    period_start=date(2025, 1, 1),
                    period_end=date(2025, 1, 31),
                    company_id=1,
                    staff_id=2,
                    staff_category='administrator',
                    updated_at=datetime(2025, 1, 1),
                ),
                PlanStaffInput(
                    period_start=date(2025, 2, 1),
                    period_end=date(2025, 2, 28),
                    company_id=1,
                    staff_id=2,
                    staff_category='barber',
                    updated_at=datetime(2025, 2, 1),
                ),
                PlanStaffInput(
                    period_start=date(2025, 3, 1),
                    period_end=date(2025, 3, 31),
                    company_id=1,
                    staff_id=2,
                    staff_category='administrator',
                    updated_at=datetime(2025, 3, 1),
                ),
                StaffSchedule(
                    staff_id=2,
                    company_id=1,
                    date=date(2025, 1, 10),
                    slot_from=time(10),
                    slot_to=time(12),
                ),
                StaffSchedule(
                    staff_id=2,
                    company_id=1,
                    date=date(2025, 1, 10),
                    slot_from=time(10),
                    slot_to=time(12),
                ),
                StaffSchedule(
                    staff_id=2,
                    company_id=1,
                    date=date(2025, 3, 10),
                    slot_from=time(22),
                    slot_to=time(2),
                ),
                StaffSchedule(
                    staff_id=2,
                    company_id=1,
                    date=date(2025, 2, 1),
                    slot_from=time(0),
                    slot_to=time(3),
                ),
                SyncSourceState(
                    company_id=1,
                    source=dashboard_service.STAFF_SCHEDULE_SOURCE,
                    period_start=date(2024, 12, 31),
                    period_end=date(2025, 12, 31),
                    synced_at=datetime(2025, 12, 31),
                ),
            ])
            await db.flush()
            for appointment_id, appointment_date, appointment_datetime, master_id, qty in (
                (1, date(2025, 1, 10), datetime(2025, 1, 10, 7, 30), 1, 2),
                (2, date(2025, 2, 10), datetime(2025, 2, 10, 7, 30), 2, 5),
                (3, date(2025, 3, 11), datetime(2025, 3, 10, 22, 30), 1, 7),
            ):
                db.add_all([
                    Appointment(
                        id=appointment_id,
                        company_id=1,
                        staff_id=master_id,
                        date=appointment_date,
                        datetime=appointment_datetime,
                        attendance=1,
                    ),
                    Transaction(
                        id=appointment_id,
                        appointment_id=appointment_id,
                        company_id=1,
                        service_id=10,
                        service_title='Extra',
                        amount=qty,
                    ),
                ])
            db.add_all([
                Appointment(
                    id=10,
                    company_id=1,
                    staff_id=1,
                    client_id=1,
                    date=date(2025, 1, 31),
                    datetime=datetime(2025, 1, 31, 12),
                    attendance=1,
                ),
                Appointment(
                    id=11,
                    company_id=1,
                    staff_id=1,
                    client_id=1,
                    date=date(2025, 2, 10),
                    datetime=datetime(2025, 2, 10, 10),
                    create_date=datetime(2025, 1, 31, 22, 30),
                    attendance=0,
                ),
            ])
            await db.commit()

            metrics = await dashboard_service._admin_extra_service_metrics(
                db,
                date(2025, 1, 1),
                date(2025, 12, 31),
                1,
                [2],
                factual_at=datetime(2026, 1, 1),
                admin_periods_by_staff={
                    2: [
                        (date(2025, 1, 1), date(2025, 1, 31)),
                        (date(2025, 3, 1), date(2025, 12, 31)),
                    ]
                },
            )
            plan_fact = await dashboard_service.fetch_plan_fact(
                db,
                date(2025, 1, 1),
                date(2025, 12, 31),
                company_id=1,
                factual_at=datetime(2026, 1, 1),
            )

            assert metrics[2] == {
                'extra_services_qty': 9.0,
                'extra_services_denominator': 2.0,
            }
            staff_group = next(group for group in plan_fact['groups'] if group['staff_id'] == 2)
            cells = {cell['code']: cell for cell in staff_group['metrics']}
            assert cells['extra_services_qty']['fact'] == 14.0
            assert cells['extra_services_pct']['fact'] == pytest.approx(466.67, abs=0.01)
            assert cells['opz_qty']['fact'] == 0.0
            opz_events = await dashboard_service._opz_events(
                db,
                date(2025, 1, 1),
                date(2025, 12, 31),
                1,
                factual_at=datetime(2026, 1, 1),
            )
            assert [event.event_date for event in opz_events] == [date(2025, 2, 1)]

            db.add(Appointment(
                id=4,
                company_id=1,
                staff_id=1,
                date=date(2025, 4, 10),
                datetime=None,
                attendance=1,
            ))
            await db.commit()
            scope = await dashboard_service._administrator_service_scope(
                db,
                date(2025, 1, 1),
                date(2025, 12, 31),
                2,
                factual_at=datetime(2026, 1, 1),
            )
            assert scope.source_status == 'partial'
            assert scope.missing_sources == ('appointments_detail',)

            saved_inputs = (
                await db.execute(
                    select(PlanStaffInput)
                    .where(PlanStaffInput.staff_id == 2)
                    .order_by(PlanStaffInput.period_start)
                )
            ).scalars().all()
            assert [row.staff_category for row in saved_inputs] == [
                'administrator',
                'barber',
                'administrator',
            ]
    finally:
        await engine.dispose()
