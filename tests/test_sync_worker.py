"""Tests for sync_worker.py's own orchestration loop (job claiming, engine lifecycle)."""
import types

import sync_worker


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


def test_process_next_job_builds_its_own_database_when_called_standalone(monkeypatch):
    """process_next_job() keeps working with no arguments (tests, one-off invocations) by
    falling back to init_database() when main() hasn't handed it a shared instance.
    """
    _FakeDatabase.instances = 0
    monkeypatch.setattr(sync_worker, 'init_database', lambda *a, **k: _FakeDatabase())
    monkeypatch.setattr(sync_worker, 'SyncJobService', _FakeJobs)

    processed = sync_worker.process_next_job()

    assert processed is False
    assert _FakeDatabase.instances == 1


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
    monkeypatch.setattr(sync_worker, 'init_database', lambda *a, **k: database_class())
    monkeypatch.setattr(sync_worker, 'SyncJobService', jobs_class)
    monkeypatch.setattr(sync_worker, 'SyncControlService', control_class)
    monkeypatch.setattr(sync_worker, 'enqueue_auto_sync_jobs_if_due', lambda db, *a, **k: {'enqueued': 0})
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
    return seen['n']


def test_a_failing_retention_sweep_does_not_stop_the_worker(monkeypatch):
    """Both sweeps raising must not kill main() nor skip a tick.

    main() has no except of its own, so an escaping sweep exception ends the process. The
    container restarts it, lands in the same idle branch, and fails again — the sync cadence
    stops for good while the worker looks like it is merely restarting.
    """
    _FailingJobs.sweeps = 0
    _FailingControl.sweeps = 0

    ticks = _run_worker_ticks(monkeypatch, _ExplodingDatabase, _FailingJobs, _FailingControl)

    assert ticks == 3
    # Every tick attempted both sweeps and survived both.
    assert _FailingControl.sweeps == 3
    assert _FailingJobs.sweeps == 3
    # And each failure was rolled back — once per failing sweep, per tick.
    assert _ExplodingDatabase.session.rollbacks == 6


def test_a_failing_rollback_after_a_failing_sweep_does_not_stop_the_worker(monkeypatch):
    """The dead-connection case: the sweep raises AND the rollback raises."""
    _FailingJobs.sweeps = 0
    _FailingControl.sweeps = 0
    monkeypatch.setattr(_ExplodingDatabase, 'session_class', _ExplodingSession)

    ticks = _run_worker_ticks(monkeypatch, _ExplodingDatabase, _FailingJobs, _FailingControl)

    assert ticks == 3
    assert _FailingJobs.sweeps == 3
    assert _ExplodingDatabase.session.rollbacks == 6


def test_auto_enqueue_still_runs_when_a_retention_sweep_fails(monkeypatch):
    """A failing sweep must not cost the tick its auto-enqueue.

    Note what this does NOT pin: because each sweep is individually wrapped, moving them
    back above enqueue_auto_sync_jobs_if_due would still leave this green. The ordering
    itself is only enforced by reading the code; what the sibling above pins is that the
    loop survives at all. Kept because the guarantee users actually care about is that the
    sync cadence continues, and that is what this asserts directly.
    """
    _FailingJobs.sweeps = 0
    _FailingControl.sweeps = 0
    monkeypatch.setattr(_ExplodingDatabase, 'session_class', _RollbackRecordingSession)
    enqueued = {'n': 0}

    def counting_enqueue(db, *a, **k):
        enqueued['n'] += 1
        return {'enqueued': 0}

    _ExplodingDatabase.instances = 0
    monkeypatch.setattr(sync_worker, 'init_database', lambda *a, **k: _ExplodingDatabase())
    monkeypatch.setattr(sync_worker, 'SyncJobService', _FailingJobs)
    monkeypatch.setattr(sync_worker, 'SyncControlService', _FailingControl)
    monkeypatch.setattr(sync_worker, 'enqueue_auto_sync_jobs_if_due', counting_enqueue)
    monkeypatch.setattr(sync_worker, 'parse_args', lambda: types.SimpleNamespace(once=False))

    seen = {'n': 0}

    def fake_sleep(_seconds):
        seen['n'] += 1
        if seen['n'] >= 3:
            raise _StopWorker()

    monkeypatch.setattr(sync_worker.time, 'sleep', fake_sleep)
    try:
        sync_worker.main()
    except _StopWorker:
        pass

    assert enqueued['n'] == 3
