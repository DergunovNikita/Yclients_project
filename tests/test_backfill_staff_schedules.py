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


class FakeDatabase:
    def __init__(self, session):
        self.session = session

    def test_connection(self):
        return True

    def get_db(self):
        return self.session


class FakeControl:
    def __init__(self, acquired):
        self.acquired = acquired
        self.released = False

    def acquire_lock(self, _db):
        return self.acquired

    def release_lock(self, _db):
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
    control = FakeControl(acquired=False)
    monkeypatch.setattr(backfill_staff_schedules, 'parse_args', _args)
    monkeypatch.setattr(backfill_staff_schedules, 'init_database', lambda *_args: FakeDatabase(session))
    monkeypatch.setattr(backfill_staff_schedules, 'SyncControlService', lambda: control)

    assert backfill_staff_schedules.main() == 1
    assert control.released is False
    assert session.closed is True


def test_backfill_releases_sync_lock_on_early_exit(monkeypatch):
    session = FakeSession()
    control = FakeControl(acquired=True)
    monkeypatch.setattr(backfill_staff_schedules, 'parse_args', _args)
    monkeypatch.setattr(backfill_staff_schedules, 'init_database', lambda *_args: FakeDatabase(session))
    monkeypatch.setattr(backfill_staff_schedules, 'SyncControlService', lambda: control)

    assert backfill_staff_schedules.main() == 1
    assert control.released is True
    assert session.closed is True
