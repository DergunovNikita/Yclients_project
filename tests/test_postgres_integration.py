import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

import sync_worker
from database import run_migrations
from sync_control import SyncControlService
from sync_jobs import SyncJobService
from models import SyncJob


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
    monkeypatch.setattr(sync_worker, 'run_services_label_sync_if_due', lambda db: {'status': 'skipped'})

    assert sync_worker.process_next_job() is True

    session = pg_session_factory()
    try:
        saved = session.get(SyncJob, job.id)
        assert saved.status == 'success'
        assert saved.run_id == 77
        assert saved.finished_at is not None
    finally:
        session.close()
