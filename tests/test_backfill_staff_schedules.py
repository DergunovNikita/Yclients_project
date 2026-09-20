from argparse import Namespace
from datetime import date

from scripts import backfill_staff_schedules


class FakeQuery:
    def filter(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def all(self):
        return []


class FakeSession:
    def __init__(self):
        self.closed = False

    def query(self, *_args):
        return FakeQuery()

    def close(self):
        self.closed = True


class FakeConnection:
    """Stands in for the dedicated connection the advisory lock lives on."""

    def __init__(self):
        self.closed = False

    def execution_options(self, **_kwargs):
        return self

    def close(self):
        self.closed = True


class FakeEngine:
    def __init__(self):
        self.connections = []

    def connect(self):
        connection = FakeConnection()
        self.connections.append(connection)
        return connection


class FakeDatabase:
    def __init__(self, session):
        self.session = session
        self.engine = FakeEngine()

    def test_connection(self):
        return True

    def get_db(self):
        return self.session


class FakeControl:
    def __init__(self, acquired):
        self.acquired = acquired
        self.released = False
        self.acquired_on = None
        self.released_on = None

    def acquire_lock(self, db):
        self.acquired_on = db
        return self.acquired

    def release_lock(self, db):
        self.released_on = db
        self.released = True


def _args():
    return Namespace(start=None, end=date(2025, 1, 31), company_ids=None, chunk_days=31)


def test_backfill_windows_run_newest_first_without_gaps():
    assert list(
        backfill_staff_schedules._windows(
            date(2025, 1, 1),
            date(2025, 3, 10),
            31,
        )
    ) == [
        (date(2025, 2, 8), date(2025, 3, 10)),
        (date(2025, 1, 8), date(2025, 2, 7)),
        (date(2025, 1, 1), date(2025, 1, 7)),
    ]


def test_company_start_includes_previous_day_for_overnight_shift():
    company = type('CompanyStub', (), {'reporting_start_date': date(2025, 1, 1)})()

    assert backfill_staff_schedules._company_start(company, None, None) == date(2024, 12, 31)
    assert backfill_staff_schedules._company_start(
        company,
        None,
        date(2025, 2, 1),
    ) == date(2025, 1, 31)


def test_backfill_refuses_to_run_while_sync_lock_is_held(monkeypatch):
    session = FakeSession()
    database = FakeDatabase(session)
    control = FakeControl(acquired=False)
    monkeypatch.setattr(backfill_staff_schedules, 'parse_args', _args)
    monkeypatch.setattr(backfill_staff_schedules, 'init_database', lambda *_args: database)
    monkeypatch.setattr(backfill_staff_schedules, 'SyncControlService', lambda: control)

    assert backfill_staff_schedules.main() == 1
    assert control.released is False
    assert session.closed is True
    # Even when the lock was never taken, the connection opened for it must go back.
    assert database.engine.connections[0].closed is True


def test_backfill_releases_sync_lock_on_early_exit(monkeypatch):
    session = FakeSession()
    database = FakeDatabase(session)
    control = FakeControl(acquired=True)
    monkeypatch.setattr(backfill_staff_schedules, 'parse_args', _args)
    monkeypatch.setattr(backfill_staff_schedules, 'init_database', lambda *_args: database)
    monkeypatch.setattr(backfill_staff_schedules, 'SyncControlService', lambda: control)

    assert backfill_staff_schedules.main() == 1
    assert control.released is True
    assert session.closed is True
    assert database.engine.connections[0].closed is True


def test_backfill_locks_on_a_connection_no_pooled_session_can_take(monkeypatch):
    """The lock must not live on the ORM Session this script commits through.

    pg_advisory_lock belongs to a physical connection, and a Session hands its connection
    back to the pool on every commit(). This script commits between acquiring and releasing
    (load_credentials_for_companies_sync, and each sync_staff_schedules call), so a lock taken
    on the Session could be released on a different connection: pg_advisory_unlock() answers
    false and the lock stays held on an abandoned pooled connection, leaving every later sync
    reporting 'already_running'. Same invariant as run_sync_job's, pinned the same way.
    """
    session = FakeSession()
    database = FakeDatabase(session)
    control = FakeControl(acquired=True)
    monkeypatch.setattr(backfill_staff_schedules, 'parse_args', _args)
    monkeypatch.setattr(backfill_staff_schedules, 'init_database', lambda *_args: database)
    monkeypatch.setattr(backfill_staff_schedules, 'SyncControlService', lambda: control)

    backfill_staff_schedules.main()

    lock_connection = database.engine.connections[0]
    assert control.acquired_on is lock_connection
    assert control.released_on is lock_connection
    assert control.acquired_on is not session, (
        'the lock lives on the pooled ORM Session: a commit hands that connection back to '
        'the pool and the unlock lands on a different connection'
    )
