from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sync_worker
from models import Base, SyncJob, SyncJobEvent, SyncRun, SyncState, SyncStepRun, YClientsCredential
from sync_control import SyncControlService
from sync_jobs import SyncJobService


@contextmanager
def _sqlite_session(tables):
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=tables)
    session_local = sessionmaker(bind=engine)
    session = session_local()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_set_state_serializes_datetime_to_isoformat():
    with _sqlite_session([SyncState.__table__]) as session:
        service = SyncControlService()
        value = datetime(2026, 3, 29, 10, 30, 45, 123456)

        service.set_state(session, 'last_run_started_at', value)

        saved = session.get(SyncState, 'last_run_started_at')
        assert saved is not None
        assert saved.value == '2026-03-29T10:30:45.123456'


def test_status_payload_includes_last_successful_sync_at():
    with _sqlite_session([SyncState.__table__, SyncRun.__table__]) as session:
        service = SyncControlService()

        service.set_state(session, 'last_successful_sync_at', datetime(2026, 5, 29, 10, 3, 59))
        payload = service.get_status_payload(session)

        assert payload['last_successful_sync_at'] == '2026-05-29T10:03:59'


def _retention_session():
    """One session for both retention sweeps.

    Shared deliberately: each sweep owns one pair of tables (runs+step_runs,
    jobs+job_events) and neither is the other's cascade, so the assertions that matter
    most are the ones proving a sweep left the other pair alone. That needs all four
    present in the same session.
    """
    return _sqlite_session([
        SyncRun.__table__,
        SyncStepRun.__table__,
        SyncJob.__table__,
        SyncJobEvent.__table__,
        SyncState.__table__,
    ])


def _run(run_id, status, started_at, finished_at=None):
    return SyncRun(
        id=run_id,
        mode='incremental',
        trigger_type='manual',
        status=status,
        initiator='pytest',
        started_at=started_at,
        finished_at=finished_at,
    )


def test_purge_old_runs_deletes_old_finished_runs_and_their_step_runs():
    with _retention_session() as session:
        old = datetime(2026, 1, 1)
        recent = datetime(2026, 5, 25)
        session.add(_run(1, 'success', old, old))
        session.add(_run(2, 'success', recent, recent))
        session.add(SyncStepRun(run_id=1, step_name='Клиенты', status='success', created_at=old))
        session.commit()

        deleted = SyncControlService().purge_old_runs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 1
        assert session.get(SyncRun, 1) is None
        assert session.query(SyncStepRun).filter(SyncStepRun.run_id == 1).count() == 0
        assert session.get(SyncRun, 2) is not None


def test_purge_old_runs_never_deletes_a_running_run_regardless_of_age():
    with _retention_session() as session:
        # A newer finished run makes id=1 clearly not "the latest run", isolating the
        # running-status guard from the separate always-keep-the-latest-run safety net below.
        session.add(_run(1, 'running', datetime(2020, 1, 1)))
        session.add(_run(2, 'success', datetime(2026, 5, 25), datetime(2026, 5, 25)))
        session.commit()

        deleted = SyncControlService().purge_old_runs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 0
        running = session.get(SyncRun, 1)
        assert running is not None
        assert running.status == 'running'


def test_purge_old_runs_keeps_the_single_latest_run_even_if_stale():
    """If sync has been broken longer than the window, get_status_payload() must still be
    able to report a last_run instead of suddenly seeing none at all."""
    with _retention_session() as session:
        session.add(_run(1, 'failed', datetime(2026, 1, 1), datetime(2026, 1, 1)))
        session.commit()

        deleted = SyncControlService().purge_old_runs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 0
        assert session.get(SyncRun, 1) is not None


def test_purge_old_runs_disabled_when_retention_days_not_positive():
    with _retention_session() as session:
        session.add(_run(1, 'success', datetime(2020, 1, 1), datetime(2020, 1, 1)))
        session.commit()

        assert SyncControlService().purge_old_runs(session, retention_days=0) == 0
        assert session.get(SyncRun, 1) is not None


def test_purge_old_runs_nulls_the_dangling_sync_job_reference():
    """SyncJobService.finish_job stamps job.run_id once and nothing ever clears it, so every
    auto-enqueued run (the routine incremental/refresh/full cadence, see AGENTS.md) keeps a
    sync_jobs row pointing at it forever. sync_jobs.run_id has no ON DELETE, so without nulling
    it first, retention would either violate that FK (real PostgreSQL) or leave a dangling id.
    The job row itself -- status, timings, step_results -- must survive untouched.
    """
    with _retention_session() as session:
        old = datetime(2026, 1, 1)
        session.add(_run(1, 'success', old, old))
        session.add(_run(2, 'success', datetime(2026, 5, 25), datetime(2026, 5, 25)))
        session.add(SyncJob(
            mode='incremental', initiator='auto-worker', status='success',
            company_ids=[], progress_pct=100, current_stage='success', step_results=[],
            cancel_requested=False, requested_at=old, finished_at=old, run_id=1,
        ))
        session.commit()

        deleted = SyncControlService().purge_old_runs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 1
        assert session.get(SyncRun, 1) is None
        job = session.query(SyncJob).one()
        assert job.run_id is None
        assert job.status == 'success'
        assert job.current_stage == 'success'


def test_purge_old_runs_if_due_throttles_to_once_per_interval():
    with _retention_session() as session:
        session.add(_run(1, 'success', datetime(2026, 1, 1), datetime(2026, 1, 1)))
        # started_at sits strictly between the first and third call's cutoffs (30 days back
        # from `now` and from `now + 25h`): not yet stale at call 1, but stale by call 3 --
        # isolating the throttle itself (call 2 must skip even though this has aged into
        # eligibility) from the retention window advancing with `now` (call 3 must still catch
        # it). Ids stay in chronological order, as create_run() always assigns them in reality.
        session.add(_run(2, 'success', datetime(2026, 5, 2, 20, 0), datetime(2026, 5, 2, 20, 0)))
        session.add(_run(3, 'success', datetime(2026, 5, 25), datetime(2026, 5, 25)))
        session.commit()

        service = SyncControlService()
        now = datetime(2026, 6, 1, 8, 0, 0)

        first = service.purge_old_runs_if_due(session, retention_days=30, interval_hours=24, now=now)
        assert first == 1
        assert session.get(SyncRun, 1) is None
        assert session.get(SyncRun, 2) is not None

        skipped = service.purge_old_runs_if_due(
            session, retention_days=30, interval_hours=24, now=now + timedelta(hours=1),
        )
        assert skipped is None
        assert session.get(SyncRun, 2) is not None  # already stale now, but the throttle must skip it

        ran_again = service.purge_old_runs_if_due(
            session, retention_days=30, interval_hours=24, now=now + timedelta(hours=25),
        )
        assert ran_again == 1
        assert session.get(SyncRun, 2) is None
        assert session.get(SyncRun, 3) is not None


def _auto_sync_session():
    return _sqlite_session([
        YClientsCredential.__table__,
        SyncJob.__table__,
        SyncRun.__table__,
        SyncState.__table__,
    ])


def _credential(portal_account_id: int) -> YClientsCredential:
    now = datetime(2026, 5, 1, 10, 0, 0)
    return YClientsCredential(
        portal_account_id=portal_account_id,
        title=f'Tenant {portal_account_id}',
        partner_token_encrypted='token',
        login_encrypted='login',
        password_encrypted='password',
        is_active=True,
        needs_reauth=False,
        created_at=now,
        updated_at=now,
    )


def test_auto_sync_enqueues_due_tenant(monkeypatch):
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
        session.add(_credential(7))
        session.commit()

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)
        jobs = session.query(SyncJob).all()

        assert result['enqueued'] == 1
        assert len(jobs) == 1
        assert jobs[0].portal_account_id == 7
        assert jobs[0].initiator == sync_worker.AUTO_SYNC_INITIATOR


@pytest.mark.asyncio
async def test_async_enqueue_job_persists_normalized_queued_defaults(async_session):
    job = await SyncJobService().async_enqueue_job(
        async_session,
        ' Full ',
        'dashboard',
        portal_account_id=7,
        credential_id=11,
        company_ids=[2, 2, 3],
    )

    saved = await async_session.get(SyncJob, job.id)

    assert saved is not None
    assert saved.id == job.id
    assert saved.mode == 'full'
    assert saved.initiator == 'dashboard'
    assert saved.status == 'queued'
    assert saved.portal_account_id == 7
    assert saved.credential_id == 11
    assert saved.company_ids == [2, 3]
    assert saved.progress_pct == 0
    assert saved.current_stage == 'queued'
    assert saved.step_results == []
    assert saved.cancel_requested is False
    assert saved.requested_at is not None


def test_auto_sync_retries_tenant_needing_reauth(monkeypatch):
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
        credential = _credential(7)
        credential.needs_reauth = True
        session.add(credential)
        session.commit()

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)
        jobs = session.query(SyncJob).all()

        assert result['enqueued'] == 1
        assert len(jobs) == 1
        assert jobs[0].portal_account_id == 7


def test_reap_stale_jobs_fails_orphaned_running_and_keeps_fresh():
    now = datetime(2026, 5, 1, 12, 0, 0)

    with _auto_sync_session() as session:
        stale = SyncJob(
            mode='incremental', initiator='auto-worker', status='running',
            portal_account_id=7, company_ids=[], progress_pct=50,
            current_stage='Сотрудники', step_results=[], cancel_requested=False,
            requested_at=now - timedelta(hours=30), started_at=now - timedelta(hours=30),
        )
        fresh = SyncJob(
            mode='incremental', initiator='auto-worker', status='running',
            portal_account_id=8, company_ids=[], progress_pct=10,
            current_stage='Клиенты', step_results=[], cancel_requested=False,
            requested_at=now - timedelta(minutes=3), started_at=now - timedelta(minutes=3),
        )
        session.add_all([stale, fresh])
        session.commit()

        reaped = SyncJobService().reap_stale_jobs(session, max_running_minutes=120, now=now)

        session.refresh(stale)
        session.refresh(fresh)
        assert reaped == 1
        assert stale.status == 'failed'
        assert stale.finished_at is not None
        assert 'stale' in (stale.error_message or '').lower()
        assert fresh.status == 'running'


def test_auto_sync_skips_recent_or_active_jobs(monkeypatch):
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
        session.add_all([_credential(7), _credential(8)])
        session.add(SyncJob(
            mode='incremental',
            initiator='pytest',
            status='success',
            portal_account_id=7,
            company_ids=[],
            progress_pct=100,
            current_stage='success',
            step_results=[],
            cancel_requested=False,
            requested_at=now - timedelta(hours=1),
            finished_at=now - timedelta(hours=1),
        ))
        session.add(SyncJob(
            mode='incremental',
            initiator='pytest',
            status='queued',
            portal_account_id=8,
            company_ids=[],
            progress_pct=0,
            current_stage='queued',
            step_results=[],
            cancel_requested=False,
            requested_at=now - timedelta(hours=5),
        ))
        session.commit()

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)

        assert result['enqueued'] == 0
        assert result['skipped'] == 2
        assert session.query(SyncJob).count() == 2


def test_auto_sync_skips_after_recent_global_sync(monkeypatch):
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
        session.add(_credential(7))
        SyncControlService().set_state(session, 'last_successful_sync_at', now - timedelta(minutes=30))

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)

        assert result == {
            'status': 'ok',
            'enqueued': 0,
            'skipped': 1,
            'reason': 'recent_global_sync',
        }
        assert session.query(SyncJob).count() == 0


def test_run_sync_job_locks_on_a_connection_no_pooled_session_can_take(monkeypatch, tmp_path):
    """The advisory lock must live on its own connection, not on the bookkeeping Session.

    pg_advisory_lock belongs to a physical connection, not to a transaction, and an ORM
    Session returns its connection to the pool on every commit(). `run_sync_job` commits
    repeatedly (create_run, set_state, every progress_callback) while the pipeline session
    works on the same pool, so a lock taken on that Session gets released on whichever
    connection it happens to hold at the end: pg_advisory_unlock() answers false, the lock
    stays on an abandoned pooled connection that init_database's module-level global keeps
    open, and every later run reports 'already_running' until the process dies.

    Reproduced against real PostgreSQL. This test needs no database: it pins the structural
    invariant that the acquire and the release target one dedicated connection, which is
    what makes the reproduction impossible. tests/test_postgres_integration.py proves the
    behaviour itself, but never runs in CI (it needs TEST_DATABASE_URL).
    """
    import sync_orchestrator

    sessions = []
    lock_calls = {}

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.committed = 0

        def commit(self):
            self.committed += 1

        def close(self):
            pass

    class FakeEngine:
        def __init__(self):
            self.connections = []

        def connect(self):
            conn = FakeConnection()
            self.connections.append(conn)
            return conn

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()

        def get_db(self):
            session = FakeSession()
            sessions.append(session)
            return session

    database = FakeDatabase()
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock',
                        lambda self, db: lock_calls.setdefault('acquire', db) and True or True)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'release_lock',
                        lambda self, db: lock_calls.__setitem__('release', db))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'cleanup_stale_runs',
                        lambda self, db: None)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'create_run',
                        lambda self, db, *a, **k: SyncRun(id=1, started_at=datetime.utcnow()))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'set_state', lambda *a, **k: None)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'finish_run', lambda *a, **k: None)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'get_status_payload',
                        lambda self, db: {})
    monkeypatch.setattr(sync_orchestrator, 'execute_sync', lambda **kwargs: {
        'success': True, 'step_results': [], 'window_start': None,
        'window_end': None, 'companies_count': 0,
    })
    monkeypatch.setattr(sync_orchestrator.TelegramNotifier, 'send', lambda self, message: None)
    monkeypatch.setattr(sync_orchestrator, 'build_log_path', lambda *a, **k: str(tmp_path / 'x.log'))

    sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert 'acquire' in lock_calls and 'release' in lock_calls
    assert lock_calls['acquire'] is lock_calls['release'], (
        'the lock was acquired and released on different objects'
    )
    assert lock_calls['acquire'] not in sessions, (
        'the lock lives on a pooled ORM Session: a commit hands that connection back to the '
        'pool and the unlock lands on a different connection'
    )
    assert isinstance(lock_calls['acquire'], FakeConnection)
    assert lock_calls['acquire'].closed, 'the dedicated lock connection was never closed'


def test_run_sync_job_releases_lock_when_create_run_raises(monkeypatch, tmp_path):
    """cleanup_stale_runs()/create_run() run after the lock is acquired but, before this
    fix, before the try/finally that releases it. A DB error there (constraint violation,
    dropped connection) used to leave lock_conn open and the advisory lock held on it
    forever, exactly the 'every later run reports already_running' failure mode the
    dedicated connection was introduced to fix -- just reached through a different trigger.
    """
    import sync_orchestrator

    lock_calls = {}

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def commit(self):
            pass

        def close(self):
            pass

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()

        def get_db(self):
            return FakeSession()

    database = FakeDatabase()
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock',
                        lambda self, db: lock_calls.setdefault('acquire', db) and True or True)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'release_lock',
                        lambda self, db: lock_calls.__setitem__('release', db))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'cleanup_stale_runs',
                        lambda self, db: None)

    def _raise_create_run(self, db, *a, **k):
        raise RuntimeError('simulated DB error creating the run row')

    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'create_run', _raise_create_run)
    monkeypatch.setattr(sync_orchestrator, 'build_log_path', lambda *a, **k: str(tmp_path / 'x.log'))

    with pytest.raises(RuntimeError):
        sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert 'release' in lock_calls, 'create_run raised and the lock was never released'
    assert lock_calls['acquire'].closed, 'create_run raised and the lock connection was never closed'


def test_run_sync_job_releases_lock_when_build_log_path_raises(monkeypatch, tmp_path):
    """build_log_path() used to run as a bare statement between the acquire and the
    try/except that owns lock cleanup. Its mkdir(parents=True, exist_ok=True) raises on a
    permissions problem, a read-only mount or a full disk -- with the lock already held at
    that point, the exception used to escape with lock_conn (and control_db) still open,
    leaking the advisory lock forever: every later sync would report 'already_running'.
    """
    import sync_orchestrator

    lock_calls = {}

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.closed = False

        def commit(self):
            pass

        def close(self):
            self.closed = True

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()
            self.control_db = FakeSession()

        def get_db(self):
            return self.control_db

    database = FakeDatabase()
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock',
                        lambda self, db: lock_calls.setdefault('acquire', db) and True or True)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'release_lock',
                        lambda self, db: lock_calls.__setitem__('release', db))

    def _raise_build_log_path(*_a, **_k):
        raise OSError("[Errno 30] Read-only file system: '/var/log/sync'")

    monkeypatch.setattr(sync_orchestrator, 'build_log_path', _raise_build_log_path)

    with pytest.raises(OSError):
        sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert 'release' in lock_calls, 'build_log_path() raised and the lock was never released'
    assert lock_calls['acquire'].closed, 'build_log_path() raised and the lock connection was never closed'
    assert database.control_db.closed, 'build_log_path() raised and control_db was never closed'


def test_run_sync_job_closes_connections_when_already_running_status_lookup_raises(monkeypatch, tmp_path):
    """The already_running early return used to call get_status_payload(control_db) before
    closing anything. That helper issues three queries; if any of them raised, neither
    lock_conn nor control_db was closed. acquire_lock() returned False here, so no advisory
    lock is at stake, but both pooled connections leaked -- and since init_database keeps the
    engine alive in a module-level global, a long-running worker leaks one connection per
    failed attempt until the pool is exhausted.
    """
    import sync_orchestrator

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()
            self.control_db = FakeSession()
            self.lock_conn = None

        def get_db(self):
            return self.control_db

    database = FakeDatabase()
    original_connect = database.engine.connect

    def _tracking_connect():
        conn = original_connect()
        database.lock_conn = conn
        return conn

    monkeypatch.setattr(database.engine, 'connect', _tracking_connect)
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock', lambda self, db: False)

    def _raise_get_status_payload(self, db):
        raise RuntimeError('simulated DB error reading sync status')

    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'get_status_payload', _raise_get_status_payload)

    with pytest.raises(RuntimeError):
        sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert database.lock_conn is not None and database.lock_conn.closed, (
        'get_status_payload() raised and lock_conn was never closed'
    )
    assert database.control_db.closed, 'get_status_payload() raised and control_db was never closed'


def test_run_sync_job_closes_connections_when_acquire_lock_itself_raises(monkeypatch, tmp_path):
    """acquire_lock() can raise on its own (dropped connection, statement timeout) before it
    ever tells us whether the lock was taken. lock_conn and control_db were already checked
    out of the pool at that point and must still be closed.
    """
    import sync_orchestrator

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()
            self.control_db = FakeSession()
            self.lock_conn = None

        def get_db(self):
            return self.control_db

    database = FakeDatabase()
    original_connect = database.engine.connect

    def _tracking_connect():
        conn = original_connect()
        database.lock_conn = conn
        return conn

    monkeypatch.setattr(database.engine, 'connect', _tracking_connect)
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))

    def _raise_acquire_lock(self, db):
        raise RuntimeError('simulated connection drop during pg_try_advisory_lock')

    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock', _raise_acquire_lock)

    with pytest.raises(RuntimeError):
        sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert database.lock_conn is not None and database.lock_conn.closed, (
        'acquire_lock() raised and lock_conn was never closed'
    )
    assert database.control_db.closed, 'acquire_lock() raised and control_db was never closed'


def test_run_sync_job_closes_connections_when_release_lock_raises_after_create_run_failure(monkeypatch, tmp_path):
    """release_lock() itself can raise (lock_conn going stale is exactly the failure mode
    it exists to guard against). The except-block after create_run()/build_log_path()
    failure calls release_lock() then lock_conn.close()/control_db.close() as bare
    sequential statements -- if release_lock() raises, both close() calls are skipped and
    lock_conn leaks, the same failure mode this whole dedicated-connection fix targets,
    just reached through a third trigger.
    """
    import sync_orchestrator

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.closed = False

        def commit(self):
            pass

        def close(self):
            self.closed = True

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()
            self.control_db = FakeSession()
            self.lock_conn = None

        def get_db(self):
            return self.control_db

    database = FakeDatabase()
    original_connect = database.engine.connect

    def _tracking_connect():
        conn = original_connect()
        database.lock_conn = conn
        return conn

    monkeypatch.setattr(database.engine, 'connect', _tracking_connect)
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock', lambda self, db: True)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'cleanup_stale_runs', lambda self, db: None)

    def _raise_create_run(self, db, *a, **k):
        raise RuntimeError('simulated DB error creating the run row')

    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'create_run', _raise_create_run)
    monkeypatch.setattr(sync_orchestrator, 'build_log_path', lambda *a, **k: str(tmp_path / 'x.log'))

    def _raise_release_lock(self, db):
        raise RuntimeError('simulated connection drop during pg_advisory_unlock')

    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'release_lock', _raise_release_lock)

    with pytest.raises(RuntimeError, match='pg_advisory_unlock'):
        sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert database.lock_conn is not None and database.lock_conn.closed, (
        'release_lock() raised during create_run cleanup and lock_conn was never closed'
    )
    assert database.control_db.closed, (
        'release_lock() raised during create_run cleanup and control_db was never closed'
    )


def test_run_sync_job_closes_connections_when_release_lock_raises_at_normal_finish(monkeypatch, tmp_path):
    """Same gap as above, at the far more common site: the final finally block that runs
    after every sync, success or failure. lock_conn sits idle -- unused -- for the run's
    whole duration, which for `full` mode is hours (see AGENTS.md), so a network
    middlebox dropping an idle connection before release_lock() runs is a realistic way
    to hit this, not just a contrived one.
    """
    import sync_orchestrator

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.closed = False

        def commit(self):
            pass

        def close(self):
            self.closed = True

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()
            self.control_db = FakeSession()
            self.lock_conn = None

        def get_db(self):
            return self.control_db

    database = FakeDatabase()
    original_connect = database.engine.connect

    def _tracking_connect():
        conn = original_connect()
        database.lock_conn = conn
        return conn

    monkeypatch.setattr(database.engine, 'connect', _tracking_connect)
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock', lambda self, db: True)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'cleanup_stale_runs', lambda self, db: None)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'create_run',
                        lambda self, db, *a, **k: SyncRun(id=1, started_at=datetime.utcnow()))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'set_state', lambda *a, **k: None)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'finish_run', lambda *a, **k: None)
    monkeypatch.setattr(sync_orchestrator, 'execute_sync', lambda **kwargs: {
        'success': True, 'step_results': [], 'window_start': None,
        'window_end': None, 'companies_count': 0,
    })
    monkeypatch.setattr(sync_orchestrator.TelegramNotifier, 'send', lambda self, message: None)
    monkeypatch.setattr(sync_orchestrator, 'build_log_path', lambda *a, **k: str(tmp_path / 'x.log'))

    def _raise_release_lock(self, db):
        raise RuntimeError('simulated connection drop during pg_advisory_unlock')

    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'release_lock', _raise_release_lock)

    with pytest.raises(RuntimeError, match='pg_advisory_unlock'):
        sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert database.lock_conn is not None and database.lock_conn.closed, (
        'release_lock() raised at normal finish and lock_conn was never closed'
    )
    assert database.control_db.closed, 'release_lock() raised at normal finish and control_db was never closed'


def test_run_sync_job_releases_lock_even_when_notification_building_raises(monkeypatch, tmp_path):
    """The lock must come back regardless of what happens while building/sending the
    Telegram notification, and a notification failure must not turn a completed sync into
    an unhandled exception for the caller.

    Before the fix, the final `finally` built the message and sent it *before* releasing
    the lock, using `run.started_at` -- and the ORM Session expires every object it holds on
    each commit() (this function commits dozens of times via progress_callback), so that
    attribute needed a fresh SELECT by the time `finally` ran. If control_db's transaction
    was left aborted by an earlier failure (e.g. finish_run's own multi-statement commit
    sequence failing partway through), that SELECT raised too -- directly inside `finally`,
    before the release/close block, leaking the lock exactly like the windows already
    closed elsewhere in this function. This test does not need to reproduce that exact
    poisoned-session trigger (proven separately, against real PostgreSQL, in
    tests/test_postgres_integration.py and by hand in this review); it pins the observable
    behaviour that must hold regardless of *why* notification building fails: build_sync_message
    raising for any reason must not prevent the release, and must not escape run_sync_job.
    """
    import sync_orchestrator

    class FakeConnection:
        def __init__(self):
            self.closed = False

        def execution_options(self, **_kwargs):
            return self

        def close(self):
            self.closed = True

    class FakeSession:
        def __init__(self):
            self.closed = False

        def commit(self):
            pass

        def close(self):
            self.closed = True

    class FakeEngine:
        def connect(self):
            return FakeConnection()

    class FakeDatabase:
        def __init__(self):
            self.engine = FakeEngine()
            self.control_db = FakeSession()
            self.lock_conn = None

        def get_db(self):
            return self.control_db

    database = FakeDatabase()
    original_connect = database.engine.connect

    def _tracking_connect():
        conn = original_connect()
        database.lock_conn = conn
        return conn

    monkeypatch.setattr(database.engine, 'connect', _tracking_connect)
    monkeypatch.setattr(sync_orchestrator, 'init_database', lambda *a, **k: database)
    monkeypatch.setattr(sync_orchestrator, 'SYNC_LOG_DIR', str(tmp_path))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'acquire_lock', lambda self, db: True)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'cleanup_stale_runs', lambda self, db: None)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'create_run',
                        lambda self, db, *a, **k: SyncRun(id=1, started_at=datetime.utcnow()))
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'set_state', lambda *a, **k: None)
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'finish_run', lambda *a, **k: None)
    monkeypatch.setattr(sync_orchestrator, 'execute_sync', lambda **kwargs: {
        'success': True, 'step_results': [], 'window_start': None,
        'window_end': None, 'companies_count': 0,
    })
    monkeypatch.setattr(sync_orchestrator, 'build_log_path', lambda *a, **k: str(tmp_path / 'x.log'))

    release_calls = []
    monkeypatch.setattr(sync_orchestrator.SyncControlService, 'release_lock',
                        lambda self, db: release_calls.append(db))

    def _raise_build_sync_message(**_kwargs):
        raise RuntimeError('simulated: run.started_at lazy-load on a poisoned session')

    monkeypatch.setattr(sync_orchestrator, 'build_sync_message', _raise_build_sync_message)

    def _fail_if_called(self, message):
        raise AssertionError('notifier.send() must not run when build_sync_message() already raised')

    monkeypatch.setattr(sync_orchestrator.TelegramNotifier, 'send', _fail_if_called)

    # Must return normally -- a notification-building failure is not a sync failure, and
    # must not replace the already-computed, correct `result` with an unhandled exception.
    result = sync_orchestrator.run_sync_job(mode='incremental', trigger_type='manual', initiator='pytest')

    assert result['status'] == 'success'
    assert release_calls, 'build_sync_message() raised and the lock was never released'
    assert database.lock_conn is not None and database.lock_conn.closed, (
        'build_sync_message() raised and lock_conn was never closed'
    )
    assert database.control_db.closed, 'build_sync_message() raised and control_db was never closed'


def _job(job_id, status, activity_at, *, portal_account_id=1, requested_at=None):
    """A job whose last activity is `activity_at`, requested then too unless said otherwise."""
    return SyncJob(
        id=job_id,
        mode='incremental',
        initiator='auto-worker',
        status=status,
        portal_account_id=portal_account_id,
        company_ids=[],
        progress_pct=100 if status == 'success' else 0,
        current_stage=status,
        step_results=[],
        cancel_requested=False,
        requested_at=requested_at or activity_at,
        started_at=activity_at,
        finished_at=activity_at if status not in ('queued', 'running') else None,
    )


def _event(job_id, created_at):
    return SyncJobEvent(job_id=job_id, stage_key='clients', status='info', created_at=created_at)


def test_purge_old_jobs_deletes_old_finished_jobs_and_their_events():
    """sync_job_events is the high-cardinality half (one row per stage, per company within a
    stage) and has no pruning of its own, so it has to go with the job that owns it."""
    with _retention_session() as session:
        old = datetime(2026, 1, 1)
        recent = datetime(2026, 5, 25)
        session.add(_job(1, 'success', old))
        session.add(_job(2, 'success', recent))
        session.add_all([_event(1, old), _event(1, old), _event(2, recent)])
        session.commit()

        deleted = SyncJobService().purge_old_jobs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 1
        assert session.get(SyncJob, 1) is None
        assert session.query(SyncJobEvent).filter(SyncJobEvent.job_id == 1).count() == 0
        assert session.get(SyncJob, 2) is not None
        assert session.query(SyncJobEvent).filter(SyncJobEvent.job_id == 2).count() == 1


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_purge_old_jobs_never_deletes_an_active_job_regardless_of_age(status):
    """A queued job is what claim_next_job picks up next and a running one is a live sync;
    both are the queue's current state, not history. A newer job makes id=1 clearly not the
    tenant's latest, isolating this guard from the keep-the-latest safety net below."""
    with _retention_session() as session:
        session.add(_job(1, status, datetime(2020, 1, 1)))
        session.add(_job(2, 'success', datetime(2026, 5, 25)))
        session.commit()

        deleted = SyncJobService().purge_old_jobs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 0
        active = session.get(SyncJob, 1)
        assert active is not None
        assert active.status == status


def test_purge_old_jobs_keeps_the_latest_job_of_every_tenant_even_if_stale():
    """get_latest_job() is scoped by portal_account_id, so 'last_job' has to survive per
    tenant, not just globally: the tenant whose sync broke months ago is exactly the one whose
    last job the status payload needs to show. The unscoped (NULL account) job -- what the
    full-access sync path enqueues -- is its own group for the same reason.
    """
    with _retention_session() as session:
        old = datetime(2026, 1, 1)
        older = datetime(2025, 12, 1)
        session.add(_job(1, 'success', older, portal_account_id=1))
        session.add(_job(2, 'failed', old, portal_account_id=1))
        session.add(_job(3, 'failed', old, portal_account_id=2))
        session.add(_job(4, 'success', old, portal_account_id=None))
        session.commit()

        service = SyncJobService()
        deleted = service.purge_old_jobs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 1
        assert session.get(SyncJob, 1) is None
        assert service.get_latest_job(session, portal_account_id=1).id == 2
        assert service.get_latest_job(session, portal_account_id=2).id == 3
        assert service.get_latest_job(session).id == 4


def test_purge_old_jobs_dates_a_job_by_its_last_activity_not_its_request():
    """A full sync requested long ago but finished last week is recent history. Age reads
    finished_at first, the same fallback chain sync_worker._job_activity_at uses."""
    with _retention_session() as session:
        session.add(_job(1, 'success', datetime(2026, 5, 25), requested_at=datetime(2026, 1, 1)))
        session.add(_job(2, 'success', datetime(2026, 5, 26)))
        session.commit()

        deleted = SyncJobService().purge_old_jobs(session, retention_days=30, now=datetime(2026, 6, 1))

        assert deleted == 0
        assert session.get(SyncJob, 1) is not None


def test_purge_old_jobs_disabled_when_retention_days_not_positive():
    with _retention_session() as session:
        session.add(_job(1, 'success', datetime(2020, 1, 1)))
        session.add(_job(2, 'success', datetime(2020, 1, 2)))
        session.commit()

        assert SyncJobService().purge_old_jobs(session, retention_days=0) == 0
        assert session.get(SyncJob, 1) is not None


def test_purge_old_jobs_if_due_throttles_to_once_per_interval():
    with _retention_session() as session:
        session.add(_job(1, 'success', datetime(2026, 1, 1)))
        # Activity sits strictly between the first and third call's cutoffs (30 days back from
        # `now` and from `now + 25h`): not yet stale at call 1, but stale by call 3 --
        # isolating the throttle itself (call 2 must skip even though this has aged into
        # eligibility) from the retention window advancing with `now` (call 3 must catch it).
        session.add(_job(2, 'success', datetime(2026, 5, 2, 20, 0)))
        # Newest of the tenant, so it is kept by the per-tenant rule, not by the window.
        session.add(_job(3, 'success', datetime(2026, 5, 25)))
        session.commit()

        service = SyncJobService()
        now = datetime(2026, 6, 1, 8, 0, 0)

        first = service.purge_old_jobs_if_due(session, retention_days=30, interval_hours=24, now=now)
        assert first == 1
        assert session.get(SyncJob, 1) is None
        assert session.get(SyncJob, 2) is not None

        skipped = service.purge_old_jobs_if_due(
            session, retention_days=30, interval_hours=24, now=now + timedelta(hours=1),
        )
        assert skipped is None
        assert session.get(SyncJob, 2) is not None  # already stale now, but the throttle must skip it

        ran_again = service.purge_old_jobs_if_due(
            session, retention_days=30, interval_hours=24, now=now + timedelta(hours=25),
        )
        assert ran_again == 1
        assert session.get(SyncJob, 2) is None
        assert session.get(SyncJob, 3) is not None
