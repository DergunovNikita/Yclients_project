"""Tests for sync_worker.py's own orchestration loop (job claiming, engine lifecycle)."""
import types
from contextlib import contextmanager
from datetime import datetime

import pytest

import sync_worker
from helpers import sqlite_session_with_system as _sqlite_session
from models import SyncJob, SyncRun
from sync_control import SyncControlService
from sync_jobs import SyncJobService


class _FakeSession:
    def close(self):
        pass


class _FakeDatabase:
    """Stands in for database.Database and counts how many times one gets built."""

    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1

    def get_db(self):
        return _FakeSession()


class _FakeJobs:
    # Sessions the two retention sweeps were handed, so the test can assert they run on the
    # shared, reused connection rather than opening one of their own.
    purge_sessions: list = []

    def claim_next_job(self, db):
        return None  # nothing queued -> every tick is an idle tick

    def has_running_job(self, db):
        return False

    def reap_stale_jobs(self, db, max_running_minutes=None):
        return 0

    def purge_old_jobs_if_due(self, db):
        _FakeJobs.purge_sessions.append(db)
        return None


class _FakeControl:
    def get_running_run(self, db):
        return None

    def get_state_values(self, db, keys):
        return {}

    def purge_old_runs_if_due(self, db):
        return None

    def purge_old_report_usage_if_due(self, db):
        return None


class _StopWorker(Exception):
    """Sentinel used to break main()'s `while True:` after a fixed number of ticks."""


def test_main_builds_database_once_across_several_idle_ticks(monkeypatch):
    """main() must build exactly one Database for the life of the process.

    It used to call init_database() on every poll tick -- twice when idle, once in
    process_next_job() and once in the idle branch -- building a new engine and pool each
    time. Measured cost (see the comment in sync_worker.main): not connection exhaustion,
    which the obvious reading suggests and 200 back-to-back engines did not reproduce, but a
    standing tail of idle backends reclaimed on the cycle collector's schedule plus ~34k
    pointless TCP+auth handshakes a day.
    """
    _FakeDatabase.instances = 0
    _FakeJobs.purge_sessions = []
    monkeypatch.setattr(sync_worker, 'init_database', lambda *a, **k: _FakeDatabase())
    monkeypatch.setattr(sync_worker, 'SyncJobService', _FakeJobs)
    monkeypatch.setattr(sync_worker, 'SyncControlService', _FakeControl)
    monkeypatch.setattr(sync_worker, 'enqueue_auto_sync_jobs_if_due', lambda db, *a, **k: {'enqueued': 0})
    monkeypatch.setattr(sync_worker, 'parse_args', lambda: types.SimpleNamespace(once=False))

    ticks = {'n': 0}

    def fake_sleep(_seconds):
        ticks['n'] += 1
        if ticks['n'] >= 5:
            raise _StopWorker()

    monkeypatch.setattr(sync_worker.time, 'sleep', fake_sleep)

    try:
        sync_worker.main()
    except _StopWorker:
        pass

    assert ticks['n'] == 5
    assert _FakeDatabase.instances == 1
    # The job-retention sweep runs on the idle branch's session, not one of its own: if it
    # ever opened its own Database the instance count above would have caught it, and this
    # pins that it actually ran on every idle tick rather than being skipped entirely.
    assert len(_FakeJobs.purge_sessions) == 5


class _ExplodingSession(_FakeSession):
    """A session whose rollback() fails too — the dead-connection case.

    A bulk DELETE most plausibly fails because the connection died, and that is exactly the
    state in which Session.rollback() raises as well. An unguarded rollback inside the
    handler would escape it, pass the finally, leave main() (which has no except of its own)
    and kill the worker.
    """

    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1
        raise RuntimeError('connection is closed')


class _RollbackRecordingSession(_FakeSession):
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


class _ExplodingDatabase(_FakeDatabase):
    """Hands out one shared session so the test can inspect it after the loop."""

    session_class = _RollbackRecordingSession

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        type(self).session = type(self).session_class()

    def get_db(self):
        return type(self).session


class _FailingJobs(_FakeJobs):
    sweeps = 0

    def purge_old_jobs_if_due(self, db):
        type(self).sweeps += 1
        raise RuntimeError('bulk delete failed')


class _FailingControl(_FakeControl):
    sweeps = 0

    def purge_old_runs_if_due(self, db):
        type(self).sweeps += 1
        raise RuntimeError('bulk delete failed')


def _run_worker_ticks(monkeypatch, database_class, jobs_class, control_class, ticks=3):
    database_class.instances = 0
    enqueued = {'n': 0}

    def counting_enqueue(db, *a, **k):
        enqueued['n'] += 1
        return {'enqueued': 0}

    monkeypatch.setattr(sync_worker, 'init_database', lambda *a, **k: database_class())
    monkeypatch.setattr(sync_worker, 'SyncJobService', jobs_class)
    monkeypatch.setattr(sync_worker, 'SyncControlService', control_class)
    monkeypatch.setattr(sync_worker, 'enqueue_auto_sync_jobs_if_due', counting_enqueue)
    monkeypatch.setattr(sync_worker, 'parse_args', lambda: types.SimpleNamespace(once=False))

    seen = {'n': 0}

    def fake_sleep(_seconds):
        seen['n'] += 1
        if seen['n'] >= ticks:
            raise _StopWorker()

    monkeypatch.setattr(sync_worker.time, 'sleep', fake_sleep)
    try:
        sync_worker.main()
    except _StopWorker:
        pass
    return seen['n'], enqueued['n']


@pytest.mark.parametrize('session_class', [_RollbackRecordingSession, _ExplodingSession])
def test_a_failing_retention_sweep_does_not_stop_the_worker_or_its_auto_enqueue(monkeypatch, session_class):
    """Both sweeps raising (and, for a dead connection, the rollback raising too) must not kill main().

    main() has no except of its own, so an escaping sweep exception ends the process. The
    container restarts it, lands in the same idle branch, and fails again — the sync cadence
    stops for good while the worker looks like it is merely restarting.
    """
    _FailingJobs.sweeps = 0
    _FailingControl.sweeps = 0
    monkeypatch.setattr(_ExplodingDatabase, 'session_class', session_class)

    ticks, enqueued = _run_worker_ticks(monkeypatch, _ExplodingDatabase, _FailingJobs, _FailingControl)

    assert ticks == 3
    assert enqueued == 3
    assert _FailingControl.sweeps == 3
    assert _FailingJobs.sweeps == 3
    # Every failed sweep is rolled back, or the next statement on the shared session would fail too.
    assert _ExplodingDatabase.session.rollbacks == _FailingJobs.sweeps + _FailingControl.sweeps


class _ProbeControl(SyncControlService):
    """Real control service whose lock is replaced: pg_try_advisory_lock does not exist on SQLite."""

    def __init__(self, lock_free):
        super().__init__()
        self.lock_free = lock_free
        self.probed = 0

    @contextmanager
    def probe_idle_lock(self, engine):
        self.probed += 1
        yield self.lock_free


def _orphans(session):
    session.add(SyncRun(mode='full', trigger_type='queued', status='running', initiator='x',
                        started_at=datetime(2026, 1, 1)))
    session.add(SyncJob(mode='full', initiator='x', status='running', portal_account_id=7, company_ids=[],
                        progress_pct=10, current_stage='running', step_results=[],
                        requested_at=datetime(2026, 1, 1), started_at=datetime(2026, 1, 1)))
    session.commit()


def _statuses(session):
    return session.query(SyncRun).one().status, session.query(SyncJob).one().status


def _reap(session, control):
    sync_worker.reap_orphans_if_no_sync_running(
        types.SimpleNamespace(engine=object()), session, control, SyncJobService(),
    )


def test_orphans_are_closed_when_the_lock_is_free():
    with _sqlite_session([SyncRun.__table__, SyncJob.__table__]) as session:
        _orphans(session)

        _reap(session, _ProbeControl(lock_free=True))

        assert _statuses(session) == ('abandoned', 'failed')


def test_nothing_is_closed_while_a_sync_holds_the_lock():
    """A held lock means the 'running' run and job belong to a live sync, however old they are."""
    with _sqlite_session([SyncRun.__table__, SyncJob.__table__]) as session:
        _orphans(session)
        control = _ProbeControl(lock_free=False)

        _reap(session, control)

        assert control.probed == 1
        assert _statuses(session) == ('running', 'running')


def test_the_lock_is_not_probed_when_nothing_is_running():
    """An idle tick must not take the lock needlessly: a CLI sync starting then would bounce."""
    with _sqlite_session([SyncRun.__table__, SyncJob.__table__]) as session:
        control = _ProbeControl(lock_free=True)

        _reap(session, control)

        assert control.probed == 0


def test_a_running_job_alone_is_enough_to_probe():
    with _sqlite_session([SyncRun.__table__, SyncJob.__table__]) as session:
        session.add(SyncJob(mode='full', initiator='x', status='running', portal_account_id=7, company_ids=[],
                            progress_pct=10, current_stage='running', step_results=[],
                            requested_at=datetime(2026, 1, 1), started_at=datetime(2026, 1, 1)))
        session.commit()

        _reap(session, _ProbeControl(lock_free=True))

        assert session.query(SyncJob).one().status == 'failed'


def test_process_next_job_hands_its_database_to_run_sync_job(monkeypatch):
    """The worker's engine serves the sync's lock and run tracking; no second engine per job."""
    database = _FakeDatabase()
    seen = {}

    class ClaimingJobs(_FakeJobs):
        def claim_next_job(self, db):
            return types.SimpleNamespace(
                id=1, mode='incremental', initiator='w', portal_account_id=7, credential_id=None, company_ids=[]
            )

        def finish_job(self, db, job, result):
            seen['finished'] = result

    def fake_run_sync_job(**kwargs):
        seen['database'] = kwargs['database']
        return {'status': 'success'}

    monkeypatch.setattr(sync_worker, 'SyncJobService', ClaimingJobs)
    monkeypatch.setattr(sync_worker, 'SyncControlService', _FakeControl)
    monkeypatch.setattr(sync_worker, 'run_sync_job', fake_run_sync_job)

    assert sync_worker.process_next_job(database) is True
    assert seen['database'] is database


def test_process_next_job_leaves_the_queue_alone_while_a_sync_is_running(monkeypatch):
    with _sqlite_session([SyncRun.__table__, SyncJob.__table__]) as session:
        session.add(SyncRun(mode='full', trigger_type='cli', status='running', initiator='x',
                            started_at=datetime(2026, 5, 1)))
        session.add(SyncJob(mode='incremental', initiator='x', status='queued', portal_account_id=7,
                            company_ids=[], progress_pct=0, current_stage='queued', step_results=[],
                            requested_at=datetime(2026, 5, 1)))
        session.commit()

        class Database:
            def get_db(self):
                return session

        monkeypatch.setattr(sync_worker, 'run_sync_job', lambda **kwargs: pytest.fail('a job was started'))

        assert sync_worker.process_next_job(Database()) is False
        assert session.query(SyncJob).one().status == 'queued'
