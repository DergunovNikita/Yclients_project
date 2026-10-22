import os
from pathlib import Path
from datetime import date, datetime, time

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

import dashboard_service
from clock import utcnow
import sync_orchestrator
import sync_worker
from database import Database
from scripts.bootstrap_db import bootstrap_database
from sync_control import SyncControlService
from sync_jobs import SyncJobService
from models import (
    Appointment,
    Client,
    Company,
    Group,
    PlanBranchSetting,
    PlanStaffInput,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalRefreshToken,
    PortalUser,
    ServiceCatalog,
    ServiceLabel,
    Staff,
    StaffSchedule,
    SyncJob,
    SyncJobEvent,
    SyncRun,
    SyncSourceState,
    Transaction,
    YClientsCredential,
)


TEST_DATABASE_URL = os.getenv('TEST_DATABASE_URL')

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason='TEST_DATABASE_URL is not set')


@pytest.fixture
def pg_session_factory():
    database = _bind_database_to_test_url()
    engine = database.engine
    with engine.begin() as conn:
        conn.execute(text('DROP SCHEMA IF EXISTS public CASCADE'))
        conn.execute(text('CREATE SCHEMA public'))
        conn.execute(text('DROP SCHEMA IF EXISTS system CASCADE'))
    # Same path migrate.py takes on an empty database: replaying the chain from 0001 is unsupported.
    bootstrap_database(database)
    session_local = sessionmaker(bind=engine)
    try:
        yield session_local
    finally:
        engine.dispose()


def test_bootstrap_stamps_the_database_it_built(pg_session_factory):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    root = Path(__file__).resolve().parent.parent
    cfg = Config(str(root / 'alembic.ini'))
    cfg.set_main_option('script_location', str(root / 'alembic'))
    session = pg_session_factory()
    try:
        stamped = session.execute(text('SELECT version_num FROM alembic_version')).scalar_one()
    finally:
        session.close()
    assert stamped == ScriptDirectory.from_config(cfg).get_current_head()


def test_bootstrap_creates_typed_columns(pg_session_factory):
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
    finally:
        session.close()


def test_bootstrap_enforces_one_plan_row_per_period_and_branch(pg_session_factory):
    session = pg_session_factory()
    try:
        session.add(Group(id=1, title='G1'))
        session.flush()
        session.add(Company(id=1, title='Salon', group_id=1))
        session.flush()
        session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
        session.flush()
        period = {'period_start': date(2025, 1, 1), 'period_end': date(2025, 1, 31), 'company_id': 1}
        now = datetime(2025, 1, 1)
        session.add_all([
            PlanBranchSetting(**period, updated_at=now),
            PlanStaffInput(**period, staff_id=1, staff_category='barber', updated_at=now),
        ])
        session.commit()

        for duplicate in (
            PlanBranchSetting(**period, updated_at=now),
            PlanStaffInput(**period, staff_id=1, staff_category='barber', updated_at=now),
        ):
            session.add(duplicate)
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()
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

    # Production keeps this Database alive: the worker builds one engine and reuses it for every
    # job, so its pooled connections are never closed. Hold a reference here too — without it
    # the Database is garbage-collected the moment run_sync_job returns, the connections close,
    # PostgreSQL drops the advisory lock with them, and this test passes even when the lock was
    # released on the wrong connection.
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
        # sync_jobs.run_id is a real foreign key: the faked run has to exist.
        run = SyncRun(trigger_type='pytest', mode='incremental', status='success', started_at=utcnow())
        session.add(run)
        session.commit()
        run_id = run.id
        job = service.enqueue_job(session, 'incremental', 'pytest')
    finally:
        session.close()

    class BoundDatabase:
        def __init__(self, session_factory):
            self._session_factory = session_factory

        def get_db(self):
            return self._session_factory()

    monkeypatch.setattr(sync_worker, 'init_database', lambda *args, **kwargs: BoundDatabase(pg_session_factory))
    monkeypatch.setattr(sync_worker, 'run_sync_job', lambda **kwargs: {'status': 'success', 'run_id': run_id})
    assert sync_worker.process_next_job() is True

    session = pg_session_factory()
    try:
        saved = session.get(SyncJob, job.id)
        assert saved.status == 'success'
        assert saved.run_id == run_id
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
            # One flush per row: without relationship() between these models the unit of work
            # does not order inserts by foreign key, and PostgreSQL enforces them.
            for row in [
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
                ServiceCatalog(company_id=1, service_id=10, title='Extra', updated_at=datetime(2025, 1, 1)),
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
                ),
            ]:
                db.add(row)
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

            january = await dashboard_service.fetch_plan_fact(
                db,
                date(2025, 1, 1),
                date(2025, 1, 31),
                company_id=1,
                factual_at=datetime(2026, 1, 1),
            )
            plan_fact = await dashboard_service.fetch_plan_fact(
                db,
                date(2025, 1, 1),
                date(2025, 12, 31),
                company_id=1,
                factual_at=datetime(2026, 1, 1),
            )

            # January: the barber's visit with the extras (2) is credited to him; the other January visit has none.
            january_group = next(group for group in january['groups'] if group['staff_id'] == 2)
            january_cells = {cell['code']: cell for cell in january_group['metrics']}
            assert january_cells['extra_services_qty']['fact'] == 2.0
            staff_group = next(group for group in plan_fact['groups'] if group['staff_id'] == 2)
            cells = {cell['code']: cell for cell in staff_group['metrics']}
            assert cells['extra_services_qty']['fact'] == 14.0
            # 14 extras over four visits: two credited in January, his own in February, one in March.
            assert cells['extra_services_pct']['fact'] == pytest.approx(350.0)
            assert cells['opz_qty']['fact'] == 0.0
            # The visit is rebooked at 22:30 UTC on 31 January, which is already 1 February in Moscow.
            for window, expected in (
                ((date(2025, 1, 1), date(2025, 1, 31)), 0),
                ((date(2025, 2, 1), date(2025, 2, 28)), 1),
            ):
                summary = await dashboard_service.fetch_summary(
                    db, *window, company_id=1, factual_at=datetime(2026, 1, 1),
                )
                assert summary['visit_metrics']['opz_qty'] == expected, window

            db.add(Appointment(
                id=4,
                company_id=1,
                staff_id=1,
                date=date(2025, 4, 10),
                datetime=None,
                attendance=1,
            ))
            await db.commit()
            # The per-session caches in db.info assume a session is one request and facts do not change
            # under it; a new fact means a new request in production, so start from an empty cache.
            db.info.clear()
            summary = await dashboard_service.fetch_summary(
                db,
                date(2025, 1, 1),
                date(2025, 12, 31),
                company_id=1,
                staff_id=2,
                factual_at=datetime(2026, 1, 1),
            )
            assert summary['source_status'] == 'partial'
            assert summary['missing_sources'] == ['appointments_detail']

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


def _tenant_with_user_and_credential(session):
    """One tenant whose user wrote an audit event and whose credential is referenced by a sync job and its event."""
    now = datetime(2026, 1, 1)
    session.add(PortalAccount(id=1, label='Tenant', created_at=now))
    session.flush()
    session.add_all([
        PortalUser(
            id=1, portal_account_id=1, email='owner@example.com', password_hash='x', role='owner', created_at=now
        ),
        YClientsCredential(
            id=1, portal_account_id=1, title='Cred', partner_token_encrypted='x', login_encrypted='x',
            password_encrypted='x', created_at=now, updated_at=now,
        ),
    ])
    session.flush()
    session.add_all([
        PortalAuditEvent(id=1, actor_user_id=1, portal_account_id=1, action='plan_settings.updated', created_at=now),
        PortalRefreshToken(id=1, user_id=1, token_hash='h' * 64, expires_at=now, created_at=now),
        SyncJob(id=1, mode='incremental', status='success', portal_account_id=1, credential_id=1, requested_at=now),
    ])
    session.flush()
    session.add(SyncJobEvent(id=1, job_id=1, portal_account_id=1, credential_id=1, status='success', created_at=now))
    session.commit()


def test_deleting_a_user_who_wrote_audit_events_keeps_the_events(pg_session_factory):
    session = pg_session_factory()
    try:
        _tenant_with_user_and_credential(session)

        session.execute(text('DELETE FROM system.portal_users WHERE id = 1'))
        session.commit()

        assert session.execute(select(PortalAuditEvent.actor_user_id)).scalar_one() is None
        assert session.execute(select(PortalRefreshToken.id)).first() is None
    finally:
        session.close()


def test_deleting_a_credential_referenced_by_sync_jobs_keeps_the_history(pg_session_factory):
    session = pg_session_factory()
    try:
        _tenant_with_user_and_credential(session)

        session.execute(text('DELETE FROM system.yclients_credentials WHERE id = 1'))
        session.commit()

        assert session.execute(select(SyncJob.credential_id)).scalar_one() is None
        assert session.execute(select(SyncJobEvent.credential_id)).scalar_one() is None
    finally:
        session.close()


@pytest.mark.asyncio
async def test_postgres_opz_split_counts_first_visits_per_branch(pg_session_factory):
    """The tuple IN and the grouped first-day join only run on PostgreSQL in production."""
    async_url = make_url(TEST_DATABASE_URL).set(drivername='postgresql+asyncpg')
    engine = create_async_engine(async_url)

    def visit(id, company_id, client_id, day, created=None, attendance=1):
        return Appointment(
            id=id, company_id=company_id, client_id=client_id, staff_id=company_id, date=day,
            datetime=datetime.combine(day, time(12)), attendance=attendance,
            create_date=created or datetime(2024, 1, 1, 9),
        )

    try:
        async with AsyncSession(engine) as db:
            for row in [
                Group(id=1, title='G1'),
                Company(id=1, title='Salon', group_id=1, timezone='UTC'),
                Company(id=2, title='Second salon', group_id=1, timezone='UTC'),
                Staff(id=1, name='Barber one', position='Барбер', company_id=1),
                Staff(id=2, name='Barber two', position='Барбер', company_id=2),
                # Branch 1: client 1 first comes on March 3 and is rebooked twice; client 2 is a repeat.
                visit(1, 1, 1, date(2025, 3, 3)),
                visit(2, 1, 1, date(2025, 3, 20), created=datetime(2025, 3, 3, 13)),
                visit(3, 1, 1, date(2025, 4, 10), created=datetime(2025, 3, 20, 13), attendance=0),
                visit(4, 1, 2, date(2025, 1, 10)),
                visit(5, 1, 2, date(2025, 3, 5)),
                # Branch 2 reuses client id 2: its first visit there, rebooked.
                visit(6, 2, 2, date(2025, 3, 12)),
                visit(7, 2, 2, date(2025, 4, 2), created=datetime(2025, 3, 12, 13), attendance=0),
            ]:
                db.add(row)
                await db.flush()
            await db.commit()

            metrics = (await dashboard_service.fetch_summary(
                db, date(2025, 3, 1), date(2025, 3, 31), allowed_company_ids=[1, 2],
            ))['visit_metrics']
            facts = await dashboard_service.fetch_year_over_year_facts(
                db, date(2025, 1, 1), date(2025, 12, 31), None, None, [1, 2],
            )
            second_branch = (await dashboard_service.fetch_summary(
                db, date(2025, 3, 1), date(2025, 3, 31), company_id=2, factual_at=datetime(2026, 1, 1),
            ))['visit_metrics']

            assert (metrics['first_visits'], metrics['repeat_visits']) == (2, 2)
            assert (metrics['opz_first_visit_qty'], metrics['opz_repeat_visit_qty']) == (2.0, 1.0)
            assert facts['monthly'][2025][3]['first_visits'] == 2
            assert (second_branch['first_visits'], second_branch['repeat_visits']) == (1, 0)
            assert (second_branch['opz_first_visit_qty'], second_branch['opz_repeat_visit_qty']) == (1.0, 0.0)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgres_branch_order_puts_the_saved_order_first_and_unsaved_branches_by_opening_date(
    pg_session_factory,
):
    """NULLS LAST and the correlated sort_order subquery are PostgreSQL behaviour; the row lock is a no-op elsewhere."""
    async_url = make_url(TEST_DATABASE_URL).set(drivername='postgresql+asyncpg')
    engine = create_async_engine(async_url)
    try:
        async with AsyncSession(engine) as db:
            db.add_all([
                Group(id=1, title='G1'),
                PortalAccount(id=1, label='Tenant', created_at=datetime(2026, 1, 1)),
            ])
            await db.flush()
            db.add_all([
                Company(id=5, title='Echo', group_id=1, reporting_start_date=date(2025, 1, 1)),
                Company(id=3, title='Charlie', group_id=1, reporting_start_date=date(2024, 6, 1)),
                Company(id=9, title='Alpha', group_id=1, reporting_start_date=date(2024, 6, 1)),
                Company(id=7, title='Bravo', group_id=1),
                Company(id=2, title='Delta', group_id=1),
            ])
            await db.flush()
            db.add_all([PortalBranch(portal_account_id=1, company_id=company) for company in (5, 3, 9, 7, 2)])
            await db.commit()

            async def listed():
                rows = await dashboard_service.fetch_branches(db, [2, 3, 5, 7, 9], force_allowed=True)
                return [row['id'] for row in rows]

            # Opening date, no date last, id as the tiebreak (3 and 9 open the same day).
            assert await listed() == [3, 9, 5, 2, 7]

            await dashboard_service.save_branch_order(db, 1, [2, 5, 7, 9, 3])
            await db.commit()
            assert await listed() == [2, 5, 7, 9, 3]
            assert [row['id'] for row in await dashboard_service.fetch_tenant_branch_order(db, 1)] == [2, 5, 7, 9, 3]

            # A branch the owner never placed goes after every placed one, then by the default key.
            await db.execute(text('UPDATE system.portal_branches SET sort_order = NULL WHERE company_id IN (3, 5)'))
            await db.commit()
            assert await listed() == [2, 7, 9, 3, 5]

            with pytest.raises(ValueError):
                await dashboard_service.save_branch_order(db, 1, [2, 5, 7, 9])
            await db.rollback()

            await dashboard_service.save_branch_order(db, 1, [])
            await db.commit()
            assert await listed() == [3, 9, 5, 2, 7]
            sort_orders = (await db.execute(select(PortalBranch.sort_order))).scalars().all()
            assert sort_orders == [None] * 5
    finally:
        await engine.dispose()
