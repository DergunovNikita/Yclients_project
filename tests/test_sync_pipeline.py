from datetime import date, datetime, time
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import (
    Base,
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Group,
    Service,
    ServiceCatalog,
    ServiceCategoryCatalog,
    Staff,
    StaffSchedule,
    SyncState,
    SyncSourceState,
    Transaction,
)
import sync_pipeline
from sync_pipeline import (
    execute_sync,
    full_sync_start_date,
    purge_full_refresh_window,
    purge_source_window,
    sync_financial_transactions,
    sync_comments,
    sync_clients,
    sync_goods_transactions,
    sync_records,
    sync_services,
    sync_staff,
    transactional_state_key,
)
from yclients_credentials import YClientsCredentialValue


class FakeYClientsAPI:
    def __init__(self, staff):
        self._staff = staff

    def get_staff(self, company_id):
        return self._staff


class FakeServicesAPI:
    def __init__(self, services, services_by_category=None):
        self._services = services
        self._services_by_category = services_by_category or {}

    def get_services(self, company_id, staff_id=None, category_id=None):
        if category_id is not None:
            return self._services_by_category.get(category_id, [])
        return self._services


class FakeGoodsTransactionsAPI:
    def __init__(self, txns):
        self._txns = txns

    def get_goods_transactions(self, company_id, start_date=None, end_date=None):
        return self._txns


class FakeFinancialTransactionsAPI:
    def __init__(self, txns):
        self._txns = txns

    def get_financial_transactions(self, company_id, start_date=None, end_date=None):
        return self._txns


class FakeClientsAPI:
    def __init__(self, clients):
        self._clients = clients

    def get_clients(self, company_id):
        return self._clients


class FakeRecordsAPI:
    def __init__(self, records):
        self._records = records

    def get_records(self, company_id, start_date=None, end_date=None):
        return self._records


class FakeCommentsAPI:
    def __init__(self, comments):
        self._comments = comments

    def get_comments(self, company_id, start_date=None, end_date=None):
        return self._comments


class FakeSchedulesAPI:
    def __init__(self, schedules):
        self._schedules = schedules

    def get_staff_schedule(self, company_id, start_date=None, end_date=None):
        return self._schedules


class FakeSyncDatabase:
    def __init__(self, db):
        self._db = db

    def test_connection(self):
        return True

    def get_db(self):
        return self._db


class FakeSyncAPI:
    def get_groups(self):
        return [{'id': 1, 'title': 'G1', 'companies': [{'id': 10, 'title': 'Salon'}]}]


SYNC_STEP_FUNCTIONS = (
    'sync_groups_and_companies',
    'sync_service_categories',
    'sync_services',
    'sync_positions',
    'sync_staff',
    'sync_clients',
    'sync_accounts',
    'sync_storages',
    'sync_good_categories',
    'sync_goods',
    'sync_records',
    'sync_financial_transactions',
    'sync_goods_transactions',
    'sync_comments',
    'sync_staff_schedules',
    'sync_analytics_overall',
    'sync_analytics_daily_charts',
    'sync_analytics_sources_and_statuses',
    'sync_z_report',
)


def test_company_timezone_prefers_valid_source_and_defaults_to_moscow():
    assert sync_pipeline._company_timezone({'timezone': 'Europe/Rome'}, 'Europe/Moscow') == 'Europe/Rome'
    assert sync_pipeline._company_timezone({}, None) == 'Europe/Moscow'
    assert sync_pipeline._company_timezone({'timezone': 'invalid'}, 'Europe/Rome') == 'Europe/Rome'


def patch_execute_sync_dependencies(monkeypatch, db, credential, *, purge_result=True):
    monkeypatch.setattr(sync_pipeline, 'init_database', lambda *_args, **_kwargs: FakeSyncDatabase(db))
    monkeypatch.setattr(sync_pipeline, 'load_active_credentials_sync', lambda *_args, **_kwargs: [credential])
    monkeypatch.setattr(sync_pipeline, '_build_api_for_credential', lambda _credential: FakeSyncAPI())
    monkeypatch.setattr(sync_pipeline, 'mark_credential_success_sync', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sync_pipeline, 'mark_credential_failure_sync', lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sync_pipeline, 'purge_full_refresh_window', lambda *_args, **_kwargs: purge_result)
    monkeypatch.setattr(
        sync_pipeline,
        'has_complete_historical_source_coverage',
        lambda *_args, **_kwargs: True,
    )
    for name in SYNC_STEP_FUNCTIONS:
        monkeypatch.setattr(sync_pipeline, name, lambda *_args, **_kwargs: True)


@contextmanager
def sqlite_session_with_system(tables):
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=tables)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def test_full_sync_start_date_uses_history_start_when_sync_days_is_unlimited(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_DAYS', 0)
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2000, 1, 1))
    assert full_sync_start_date(date(2026, 6, 28)) == date(2000, 1, 1)


def test_historical_sync_start_date_falls_back_to_global_floor(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2000, 1, 1))
    assert sync_pipeline.historical_sync_start_date(date(2026, 8, 6)) == date(2000, 1, 1)


def test_historical_sync_start_date_uses_branch_reporting_start(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2000, 1, 1))
    assert sync_pipeline.historical_sync_start_date(
        date(2026, 8, 6), date(2022, 5, 1)
    ) == date(2022, 5, 1)


def test_full_window_starts_at_branch_reporting_start(monkeypatch):
    """A branch that reports from 2022 must not re-fetch the history it discards."""
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2000, 1, 1))
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Trimmed', group_id=1, reporting_start_date=date(2022, 5, 1)))
        db.add(Company(id=2, title='Untrimmed', group_id=1))
        db.commit()

        assert sync_pipeline.resolve_company_sync_window(
            db, date(2026, 8, 6), 'full', 1
        ) == (date(2022, 5, 1), 'full')
        assert sync_pipeline.resolve_company_sync_window(
            db, date(2026, 8, 6), 'full', 2
        ) == (date(2000, 1, 1), 'full')
    finally:
        db.close()
        engine.dispose()


TRANSACTIONAL_WINDOW_TABLES = [
    Group.__table__,
    Company.__table__,
    Appointment.__table__,
    FinancialTransaction.__table__,
    GoodTransaction.__table__,
    Comment.__table__,
    SyncState.__table__,
    SyncSourceState.__table__,
]


def test_company_window_uses_scoped_checkpoint_first(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_DAYS', 0)
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2026, 1, 1))
    monkeypatch.setattr(sync_pipeline, 'SYNC_INCREMENTAL', True)
    monkeypatch.setattr(sync_pipeline, 'SYNC_LOOKBACK_DAYS', 2)

    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(Appointment(id=1, company_id=1, date=date(2026, 6, 20)))
        db.add(SyncState(key=sync_pipeline.TRANSACTIONAL_STATE_KEY, value='2026-06-30'))
        db.add(SyncState(key=transactional_state_key(1), value='2026-06-20'))
        db.add(SyncState(key=sync_pipeline.historical_coverage_state_key(1), value='2026-06-20'))
        for source in sync_pipeline.FULL_REFRESH_COVERAGE_SOURCES:
            db.add(SyncSourceState(
                company_id=1,
                source=source,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 6, 20),
                synced_at=datetime(2026, 6, 20),
            ))
        db.commit()

        assert sync_pipeline.resolve_company_sync_window(db, date(2026, 7, 1), 'incremental', 1) == (
            date(2026, 6, 18),
            'incremental',
        )


def test_company_window_falls_back_to_global_for_existing_transactional_company(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_DAYS', 0)
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2026, 1, 1))
    monkeypatch.setattr(sync_pipeline, 'SYNC_INCREMENTAL', True)
    monkeypatch.setattr(sync_pipeline, 'SYNC_LOOKBACK_DAYS', 2)

    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(Appointment(id=1, company_id=1, date=date(2026, 6, 20)))
        db.add(SyncState(key=sync_pipeline.TRANSACTIONAL_STATE_KEY, value='2026-06-30'))
        db.add(SyncState(key=sync_pipeline.historical_coverage_state_key(1), value='2026-06-20'))
        for source in sync_pipeline.FULL_REFRESH_COVERAGE_SOURCES:
            db.add(SyncSourceState(
                company_id=1,
                source=source,
                period_start=date(2026, 1, 1),
                period_end=date(2026, 6, 20),
                synced_at=datetime(2026, 6, 20),
            ))
        db.commit()

        assert sync_pipeline.resolve_company_sync_window(db, date(2026, 7, 1), 'incremental', 1) == (
            date(2026, 6, 28),
            'incremental',
        )


def test_company_window_ignores_global_for_new_company_without_transactional_rows(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_DAYS', 0)
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2026, 1, 1))
    monkeypatch.setattr(sync_pipeline, 'SYNC_INCREMENTAL', True)
    monkeypatch.setattr(sync_pipeline, 'SYNC_LOOKBACK_DAYS', 2)

    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(SyncState(key=sync_pipeline.TRANSACTIONAL_STATE_KEY, value='2026-06-30'))
        db.commit()

        assert sync_pipeline.resolve_company_sync_window(db, date(2026, 7, 1), 'incremental', 1) == (
            date(2026, 1, 1),
            'full',
        )


@pytest.mark.parametrize(
    'row',
    [
        Appointment(id=1, company_id=1, date=date(2026, 6, 20)),
        FinancialTransaction(id=1, company_id=1, date=datetime(2026, 6, 20, 12, 0)),
        GoodTransaction(id=1, company_id=1, date=datetime(2026, 6, 20, 12, 0)),
        Comment(id=1, company_id=1, date=datetime(2026, 6, 20, 12, 0)),
    ],
)
def test_company_has_transactional_rows_detects_existing_sources(row):
    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(row)
        db.commit()

        assert sync_pipeline.company_has_transactional_rows(db, 1) is True


def test_limited_window_marker_cannot_certify_complete_history(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_DAYS', 30)
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2000, 1, 1))
    monkeypatch.setattr(sync_pipeline, 'SYNC_INCREMENTAL', True)

    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(Appointment(id=1, company_id=1, date=date(2023, 2, 10)))
        db.add(SyncState(key=transactional_state_key(1), value='2026-06-30'))
        db.add(SyncState(
            key=sync_pipeline.historical_coverage_state_key(1),
            value='2026-06-30',
        ))
        for source in sync_pipeline.FULL_REFRESH_COVERAGE_SOURCES:
            db.add(SyncSourceState(
                company_id=1,
                source=source,
                period_start=date(2026, 6, 1),
                period_end=date(2026, 6, 30),
                synced_at=datetime(2026, 6, 30),
            ))
        db.commit()

        assert sync_pipeline.has_valid_historical_coverage_marker(db, 1) is False
        assert sync_pipeline.resolve_company_sync_window(
            db,
            date(2026, 7, 1),
            'incremental',
            1,
        ) == (date(2000, 1, 1), 'full')


def test_missing_historical_source_interval_invalidates_existing_marker(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2022, 1, 1))
    monkeypatch.setattr(sync_pipeline, 'SYNC_INCREMENTAL', True)

    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(Appointment(id=1, company_id=1, date=date(2023, 2, 10)))
        db.add(SyncState(key=transactional_state_key(1), value='2026-06-30'))
        db.add(SyncState(
            key=sync_pipeline.historical_coverage_state_key(1),
            value='2026-06-30',
        ))
        for source in sync_pipeline.FULL_REFRESH_COVERAGE_SOURCES[:-1]:
            db.add(SyncSourceState(
                company_id=1,
                source=source,
                period_start=date(2022, 1, 1),
                period_end=date(2026, 6, 30),
                synced_at=datetime(2026, 6, 30),
            ))
        db.commit()

        assert sync_pipeline.resolve_company_sync_window(
            db,
            date(2026, 7, 1),
            'incremental',
            1,
        ) == (date(2022, 1, 1), 'full')


def test_execute_sync_updates_company_scoped_checkpoint(monkeypatch):
    with sqlite_session_with_system([Group.__table__, Company.__table__, SyncState.__table__]) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.commit()

        credential = YClientsCredentialValue(
            id=11,
            title='Tenant credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(1,),
            portal_account_id=7,
        )
        patch_execute_sync_dependencies(monkeypatch, db, credential)

        result = execute_sync(mode='incremental', end_date=date(2026, 6, 30), portal_account_id=7)

        assert result['success'] is True
        assert result['mode'] == 'full'
        assert result['window_start'] == sync_pipeline.full_sync_start_date(date(2026, 6, 30)).isoformat()
        assert db.get(SyncState, sync_pipeline.TRANSACTIONAL_STATE_KEY) is None
        assert db.get(SyncState, transactional_state_key(1)).value == '2026-06-30'
        assert db.get(
            SyncState, sync_pipeline.historical_coverage_state_key(1)
        ).value == '2026-06-30'


def test_execute_sync_does_not_certify_incomplete_historical_coverage(monkeypatch):
    with sqlite_session_with_system([Group.__table__, Company.__table__, SyncState.__table__]) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.commit()

        credential = YClientsCredentialValue(
            id=11,
            title='Tenant credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(1,),
            portal_account_id=7,
        )
        patch_execute_sync_dependencies(monkeypatch, db, credential)
        monkeypatch.setattr(
            sync_pipeline,
            'has_complete_historical_source_coverage',
            lambda *_args, **_kwargs: False,
        )

        result = execute_sync(
            mode='full',
            end_date=date(2026, 6, 30),
            portal_account_id=7,
        )

        assert result['success'] is True
        assert db.get(SyncState, transactional_state_key(1)).value == '2026-06-30'
        assert db.get(
            SyncState,
            sync_pipeline.historical_coverage_state_key(1),
        ) is None


def test_execute_sync_uses_global_checkpoint_for_existing_company_without_purge(monkeypatch):
    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(Appointment(id=1, company_id=1, date=date(2026, 6, 20)))
        db.add(SyncState(key=sync_pipeline.TRANSACTIONAL_STATE_KEY, value='2026-06-28'))
        db.add(SyncState(key=sync_pipeline.historical_coverage_state_key(1), value='2026-06-20'))
        db.commit()

        credential = YClientsCredentialValue(
            id=11,
            title='Tenant credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(1,),
            portal_account_id=7,
        )
        purge_calls = []
        patch_execute_sync_dependencies(monkeypatch, db, credential)
        monkeypatch.setattr(
            sync_pipeline,
            'purge_full_refresh_window',
            lambda *_args, **_kwargs: purge_calls.append(_args) or True,
        )

        result = execute_sync(mode='incremental', end_date=date(2026, 6, 30), portal_account_id=7)

        assert result['success'] is True
        assert result['mode'] == 'incremental'
        assert result['window_start'] == '2026-06-26'
        assert purge_calls == []
        assert db.get(SyncState, transactional_state_key(1)).value == '2026-06-30'


def test_execute_sync_forces_one_time_history_backfill_for_legacy_checkpoint(monkeypatch):
    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(Appointment(id=1, company_id=1, date=date(2023, 2, 10)))
        db.add(SyncState(key=sync_pipeline.TRANSACTIONAL_STATE_KEY, value='2026-06-28'))
        for source in sync_pipeline.FULL_REFRESH_COVERAGE_SOURCES:
            db.add(SyncSourceState(
                company_id=1,
                source=source,
                period_start=date(2026, 6, 26),
                period_end=date(2026, 6, 30),
                synced_at=datetime(2026, 6, 30),
            ))
        db.commit()

        credential = YClientsCredentialValue(
            id=11,
            title='Tenant credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(1,),
            portal_account_id=7,
        )
        purge_calls = []
        patch_execute_sync_dependencies(monkeypatch, db, credential)
        monkeypatch.setattr(
            sync_pipeline,
            'purge_full_refresh_window',
            lambda *_args, **_kwargs: purge_calls.append(_args) or True,
        )

        result = execute_sync(
            mode='incremental',
            end_date=date(2026, 6, 30),
            portal_account_id=7,
        )

        assert result['success'] is True
        assert result['mode'] == 'full'
        assert result['window_start'] == sync_pipeline.full_sync_start_date(
            date(2026, 6, 30)
        ).isoformat()
        assert purge_calls
        assert db.get(
            SyncState, sync_pipeline.historical_coverage_state_key(1)
        ).value == '2026-06-30'


def test_execute_sync_does_not_checkpoint_when_full_cleanup_fails(monkeypatch):
    with sqlite_session_with_system([Group.__table__, Company.__table__, SyncState.__table__]) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.commit()

        credential = YClientsCredentialValue(
            id=11,
            title='Tenant credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(1,),
            portal_account_id=7,
        )
        patch_execute_sync_dependencies(monkeypatch, db, credential, purge_result=False)
        skipped_calls = []
        monkeypatch.setattr(sync_pipeline, 'sync_service_categories', lambda *_args, **_kwargs: skipped_calls.append('company') or True)
        monkeypatch.setattr(sync_pipeline, 'sync_analytics_overall', lambda *_args, **_kwargs: skipped_calls.append('analytics') or True)

        result = execute_sync(mode='full', end_date=date(2026, 6, 30), portal_account_id=7)

        assert result['success'] is False
        assert db.get(SyncState, transactional_state_key(1)) is None
        assert db.get(
            SyncState, sync_pipeline.historical_coverage_state_key(1)
        ) is None
        assert skipped_calls == []
        cleanup = next(item for item in result['step_results'] if item['key'] == sync_pipeline.FULL_REFRESH_CLEANUP_STEP)
        assert cleanup['success'] is False


def test_execute_sync_full_refreshes_new_company_even_with_global_checkpoint(monkeypatch):
    with sqlite_session_with_system(TRANSACTIONAL_WINDOW_TABLES) as db:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1, external_id=10, portal_account_id=7))
        db.add(SyncState(key=sync_pipeline.TRANSACTIONAL_STATE_KEY, value='2026-06-28'))
        db.commit()

        credential = YClientsCredentialValue(
            id=11,
            title='Tenant credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(1,),
            portal_account_id=7,
        )
        purge_calls = []
        patch_execute_sync_dependencies(monkeypatch, db, credential)
        monkeypatch.setattr(
            sync_pipeline,
            'purge_full_refresh_window',
            lambda *_args, **_kwargs: purge_calls.append(_args) or True,
        )

        result = execute_sync(mode='incremental', end_date=date(2026, 6, 30), portal_account_id=7)

        assert result['success'] is True
        assert result['mode'] == 'full'
        assert purge_calls
        assert db.get(SyncState, transactional_state_key(1)).value == '2026-06-30'
        assert db.get(
            SyncState, sync_pipeline.historical_coverage_state_key(1)
        ).value == '2026-06-30'


def test_execute_sync_skips_credentials_without_assigned_companies(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        credential = YClientsCredentialValue(
            id=1,
            title='Orphan credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(),
            portal_account_id=1,
        )
        monkeypatch.setattr(sync_pipeline, 'init_database', lambda *_args, **_kwargs: FakeSyncDatabase(db))
        monkeypatch.setattr(sync_pipeline, 'load_active_credentials_sync', lambda *_args, **_kwargs: [credential])
        monkeypatch.setattr(sync_pipeline, '_build_api_for_credential', lambda _credential: FakeSyncAPI())
        monkeypatch.setattr(sync_pipeline, 'mark_credential_success_sync', lambda *_args, **_kwargs: None)
        monkeypatch.setattr(sync_pipeline, 'mark_credential_failure_sync', lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            sync_pipeline,
            'resolve_sync_window',
            lambda _db, _end_date, requested_mode: (date(2026, 6, 28), requested_mode),
        )

        result = execute_sync(mode='incremental', end_date=date(2026, 6, 30))

        assert result['success'] is False
        assert result['companies_count'] == 0
        company = db.query(Company).filter(
            Company.portal_account_id == 1,
            Company.source_type == 'yclients',
            Company.external_id == 10,
        ).one_or_none()
        assert company is not None
    finally:
        db.close()
        engine.dispose()


def test_checkpoint_steps_treat_empty_results_as_success():
    assert sync_records(FakeRecordsAPI([]), None, '1') is True
    assert sync_financial_transactions(FakeFinancialTransactionsAPI([]), None, '1') is True
    assert sync_goods_transactions(FakeGoodsTransactionsAPI([]), None, '1') is True
    assert sync_comments(FakeCommentsAPI([]), None, '1') is True


def test_checkpoint_steps_fail_when_source_is_unavailable():
    assert sync_records(FakeRecordsAPI(None), None, '1') is False
    assert sync_financial_transactions(FakeFinancialTransactionsAPI(None), None, '1') is False
    assert sync_goods_transactions(FakeGoodsTransactionsAPI(None), None, '1') is False
    assert sync_comments(FakeCommentsAPI(None), None, '1') is False


def test_empty_appointment_and_goods_windows_persist_source_coverage():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Appointment.__table__,
            GoodTransaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        assert sync_records(
            FakeRecordsAPI([]),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is True
        assert sync_goods_transactions(
            FakeGoodsTransactionsAPI([]),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is True

        appointment_state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': 'appointments_detail'},
        )
        goods_state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': 'goods_transactions_detail'},
        )
        assert (appointment_state.period_start, appointment_state.period_end) == (
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
        assert (goods_state.period_start, goods_state.period_end) == (
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
    finally:
        db.close()
        engine.dispose()


def test_source_coverage_does_not_bridge_disjoint_sync_windows():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[Group.__table__, Company.__table__, SyncSourceState.__table__],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        sync_pipeline.mark_sync_source_coverage(
            db, 1, 'appointments_detail', '2024-01-01', '2024-01-31'
        )
        db.commit()
        sync_pipeline.mark_sync_source_coverage(
            db, 1, 'appointments_detail', '2025-01-01', '2025-01-31'
        )
        db.commit()

        state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': 'appointments_detail'},
        )
        assert (state.period_start, state.period_end) == (
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
    finally:
        db.close()
        engine.dispose()


def test_sync_clients_scopes_external_id_by_internal_company():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__, Client.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add_all([
            Company(id=1, title='Salon 1', group_id=1),
            Company(id=2, title='Salon 2', group_id=1),
        ])
        db.commit()

        payload = [{
            'id': 42,
            'name': 'Shared external client',
            'phone': '+100',
            'visits_count': 1,
        }]
        assert sync_clients(FakeClientsAPI(payload), db, '1') is True
        assert sync_clients(FakeClientsAPI(payload), db, '2') is True

        rows = db.query(Client).order_by(Client.company_id).all()
        assert [(row.company_id, row.source_type, row.external_id) for row in rows] == [
            (1, 'yclients', 42),
            (2, 'yclients', 42),
        ]
        assert rows[0].id != rows[1].id
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_scopes_external_id_by_internal_company():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__, Staff.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add_all([
            Company(id=1, title='Salon 1', group_id=1),
            Company(id=2, title='Salon 2', group_id=1),
        ])
        db.commit()

        payload = [{'id': 7, 'name': 'Shared external staff', 'fired': 0}]
        assert sync_staff(FakeYClientsAPI(payload), db, '1') is True
        assert sync_staff(FakeYClientsAPI(payload), db, '2') is True

        rows = db.query(Staff).order_by(Staff.company_id).all()
        assert [(row.company_id, row.source_type, row.external_id) for row in rows] == [
            (1, 'yclients', 7),
            (2, 'yclients', 7),
        ]
        assert rows[0].id != rows[1].id
    finally:
        db.close()
        engine.dispose()


def test_sync_records_uses_internal_client_and_appointment_keys():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            Appointment.__table__,
            Transaction.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add_all([
            Company(id=1, title='Salon', group_id=1),
            Staff(id=100, external_id=7, source_type='yclients', name='Record staff', company_id=1),
        ])
        db.commit()

        records = [{
            'id': 500,
            'client': {'id': 42, 'name': 'Record client'},
            'staff_id': 7,
            'date': '2025-01-10',
            'datetime': '2025-01-10T10:00:00+0300',
            'services': [{'id': 10, 'title': 'Cut', 'cost': 1000.0}],
        }]

        assert sync_records(FakeRecordsAPI(records), db, '1') is True

        client = db.query(Client).filter(Client.company_id == 1, Client.external_id == 42).one()
        appointment = db.query(Appointment).filter(Appointment.company_id == 1, Appointment.external_id == 500).one()
        transaction = db.query(Transaction).one()

        assert appointment.client_id == client.id
        assert appointment.staff_id == 100
        assert appointment.id != appointment.external_id
        assert transaction.appointment_id == appointment.id
    finally:
        db.close()
        engine.dispose()


def test_sync_records_does_not_use_cross_tenant_staff_fallback():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            Appointment.__table__,
            Transaction.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add_all([
            Company(id=1, title='Salon', group_id=1),
            Company(id=2, title='Other salon', group_id=1),
            Staff(id=7, external_id=7, source_type='yclients', name='Other staff', company_id=2),
        ])
        db.commit()

        records = [{
            'id': 500,
            'client': {'id': 42, 'name': 'Record client'},
            'staff_id': 7,
            'date': '2025-01-10',
            'datetime': '2025-01-10T10:00:00+0300',
            'services': [{'id': 10, 'title': 'Cut', 'cost': 1000.0}],
        }]

        assert sync_records(FakeRecordsAPI(records), db, '1') is True

        appointment = db.query(Appointment).filter(Appointment.company_id == 1, Appointment.external_id == 500).one()
        assert appointment.staff_id is None
    finally:
        db.close()
        engine.dispose()


def test_sync_financial_transactions_scopes_external_id_by_internal_company():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            FinancialTransaction.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add_all([
            Company(id=1, title='Salon 1', group_id=1),
            Company(id=2, title='Salon 2', group_id=1),
        ])
        db.commit()

        assert sync_financial_transactions(
            FakeFinancialTransactionsAPI([{'id': 10, 'date': '2025-01-10 12:00:00', 'amount': 100.0}]),
            db,
            '101',
            db_company_id=1,
        ) is True
        assert sync_financial_transactions(
            FakeFinancialTransactionsAPI([{'id': 10, 'date': '2025-01-11 12:00:00', 'amount': 999.0}]),
            db,
            '101',
            db_company_id=2,
        ) is True

        rows = db.query(FinancialTransaction).order_by(FinancialTransaction.company_id).all()
        assert [(row.company_id, row.external_id, row.amount) for row in rows] == [
            (1, 10, 100.0),
            (2, 10, 999.0),
        ]
        assert rows[0].id != rows[1].id
    finally:
        db.close()
        engine.dispose()


def test_sync_financial_transactions_persists_expense_article_and_source_coverage():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            FinancialTransaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        api = FakeFinancialTransactionsAPI([{
            'id': 10,
            'date': '2025-01-10 12:00:00',
            'amount': 500,
            'expense': {'id': 7, 'title': 'Пополнение личного счета'},
            'account': {'id': 1},
        }])
        assert sync_financial_transactions(
            api, db, '1', start_date='2025-01-01', end_date='2025-01-31'
        ) is True

        transaction = db.get(FinancialTransaction, 10)
        assert transaction.expense_id == 7
        assert transaction.expense_title == 'Пополнение личного счета'
        state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': 'financial_transactions_detail'},
        )
        assert state.period_start == date(2025, 1, 1)
        assert state.period_end == date(2025, 1, 31)
    finally:
        db.close()
        engine.dispose()


def test_sync_financial_transactions_coerces_blank_numeric_fields():
    """Historical YClients rows send '' for optional numeric fields.

    Postgres rejects '' for an integer column, which aborted the whole financial
    step of a full historical pass and left the branch with no financial rows at
    all after the full-refresh purge had already deleted them.
    """
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            FinancialTransaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        # Shape taken verbatim from the row that broke branch 85779 in production.
        api = FakeFinancialTransactionsAPI([{
            'id': 63998288,
            'date': '2018-11-25 19:27:44',
            'amount': -43881,
            'document_id': '',
            'expense': {'id': '', 'title': ''},
            'account': {'id': 134378},
            'record_id': '',
            'visit_id': '',
            'sold_item_id': '',
        }])
        assert sync_financial_transactions(
            api, db, '1', start_date='2018-01-01', end_date='2018-12-31'
        ) is True

        transaction = db.get(FinancialTransaction, 63998288)
        for field in ('expense_id', 'document_id', 'record_id', 'visit_id', 'sold_item_id'):
            assert getattr(transaction, field) is None, field
        assert transaction.account_id == 134378
    finally:
        db.close()
        engine.dispose()


def test_sync_financial_transactions_empty_window_persists_source_coverage():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            FinancialTransaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        assert sync_financial_transactions(
            FakeFinancialTransactionsAPI([]),
            db,
            '1',
            start_date='2025-02-01',
            end_date='2025-02-28',
        ) is True

        state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': 'financial_transactions_detail'},
        )
        assert state.period_start == date(2025, 2, 1)
        assert state.period_end == date(2025, 2, 28)
        assert db.query(FinancialTransaction).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_purge_source_window_keeps_goods_transactions_outside_requested_window():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Appointment.__table__,
            Transaction.__table__,
            FinancialTransaction.__table__,
            GoodTransaction.__table__,
            Comment.__table__,
            StaffSchedule.__table__,
            SyncState.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add_all([
            GoodTransaction(id=1, company_id=1, date=datetime(2025, 1, 1, 12, 0, 0), cost=10),
            GoodTransaction(id=2, company_id=1, date=datetime(2025, 1, 10, 12, 0, 0), cost=20),
        ])
        db.commit()

        assert purge_source_window(db, GoodTransaction, 1, '2025-01-10', '2025-01-10') == 1
        db.commit()

        remaining = db.query(GoodTransaction).order_by(GoodTransaction.id).all()
        assert [(row.id, row.cost) for row in remaining] == [(1, 10)]
    finally:
        db.close()
        engine.dispose()


def test_full_refresh_purge_leaves_fact_rows_to_their_own_source():
    """Coverage is invalidated up front, but no fact row is deleted outside a reload."""
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Appointment.__table__,
            Transaction.__table__,
            FinancialTransaction.__table__,
            GoodTransaction.__table__,
            Comment.__table__,
            StaffSchedule.__table__,
            SyncState.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add_all([
            Appointment(id=1, company_id=1, date=date(2025, 1, 10), attendance=1),
            FinancialTransaction(id=1, company_id=1, date=datetime(2025, 1, 10, 12), amount=100.0),
            GoodTransaction(id=1, company_id=1, date=datetime(2025, 1, 10, 12), cost=20),
            Comment(id=1, company_id=1, date=datetime(2025, 1, 10, 12), rating=5),
        ])
        db.commit()

        assert purge_full_refresh_window(db, 1, '2025-01-01', '2025-01-31', '2025-01-31') is True

        assert db.query(Appointment).count() == 1
        assert db.query(FinancialTransaction).count() == 1
        assert db.query(GoodTransaction).count() == 1
        assert db.query(Comment).count() == 1
    finally:
        db.close()
        engine.dispose()


def test_failed_reload_rolls_back_its_own_purge(monkeypatch):
    """A source that cannot finish its reload keeps the rows it was about to replace.

    This is the regression that emptied every branch's financial history: the purge
    was committed on its own, then the reload aborted on an id past int4.
    """
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            FinancialTransaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(FinancialTransaction(
            id=500,
            external_id=500,
            company_id=1,
            date=datetime(2025, 1, 10, 12),
            amount=100.0,
        ))
        db.commit()

        def explode(*_args, **_kwargs):
            raise RuntimeError('reload failed after the window was dropped')

        monkeypatch.setattr(sync_pipeline, 'mark_sync_source_coverage', explode)

        assert sync_financial_transactions(
            FakeFinancialTransactionsAPI([
                {'id': 501, 'date': '2025-01-11 12:00:00', 'amount': 200.0},
            ]),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
            full_refresh=True,
        ) is False

        surviving = db.query(FinancialTransaction).all()
        assert [(row.id, row.amount) for row in surviving] == [(500, 100.0)]
    finally:
        db.close()
        engine.dispose()


def test_sync_financial_transactions_accepts_ids_past_int4(tmp_path):
    """document_id and friends outgrew int4 upstream; PostgreSQL rejects them as INTEGER."""
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            FinancialTransaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        assert sync_financial_transactions(
            FakeFinancialTransactionsAPI([
                {
                    'id': 1629867000,
                    'document_id': 2152046115,
                    'record_id': 1888713429,
                    'visit_id': 1646956011,
                    'sold_item_id': 18878289,
                    'date': '2026-08-05 17:30:00',
                    'amount': 2700,
                },
            ]),
            db,
            '1',
            start_date='2026-08-01',
            end_date='2026-08-31',
        ) is True

        saved = db.query(FinancialTransaction).one()
        assert saved.document_id == 2152046115
        assert saved.record_id == 1888713429
    finally:
        db.close()
        engine.dispose()


def test_sync_records_tolerates_a_record_repeated_across_pages():
    """A long paginated fetch can return the same record twice as upstream rows shift."""
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Client.__table__,
            Staff.__table__,
            Appointment.__table__,
            Transaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        duplicated = {'id': 108295673, 'date': '2019-05-18', 'attendance': 1, 'services': []}
        assert sync_records(
            FakeRecordsAPI([duplicated, dict(duplicated)]),
            db,
            '1',
            start_date='2019-01-01',
            end_date='2019-12-31',
            full_refresh=True,
        ) is True

        saved = db.query(Appointment).all()
        assert [row.external_id for row in saved] == [108295673]
    finally:
        db.close()
        engine.dispose()


def test_sync_comments_tolerates_a_comment_repeated_across_pages():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Staff.__table__,
            Comment.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        duplicated = {'id': 50317470, 'date': '2026-07-26 11:13:28', 'rating': 5}
        assert sync_comments(
            FakeCommentsAPI([duplicated, dict(duplicated)]),
            db,
            '1',
            start_date='2026-07-01',
            end_date='2026-07-31',
            full_refresh=True,
        ) is True

        saved = db.query(Comment).all()
        assert [row.external_id for row in saved] == [50317470]
    finally:
        db.close()
        engine.dispose()


def test_bigint_columns_are_bigint_on_postgres():
    """SQLite keeps INTEGER primary keys for rowid autoincrement; PostgreSQL must not."""
    from sqlalchemy.dialects import postgresql, sqlite

    pg = postgresql.dialect()
    assert 'BIGINT' in FinancialTransaction.__table__.c.id.type.compile(pg)
    assert 'BIGINT' in FinancialTransaction.__table__.c.document_id.type.compile(pg)
    assert 'BIGINT' in GoodTransaction.__table__.c.document_id.type.compile(pg)
    assert 'BIGINT' in Appointment.__table__.c.id.type.compile(pg)
    assert 'BIGINT' in Appointment.__table__.c.external_id.type.compile(pg)
    assert 'BIGINT' in Comment.__table__.c.record_id.type.compile(pg)
    assert 'BIGINT' in Transaction.__table__.c.appointment_id.type.compile(pg)

    lite = sqlite.dialect()
    assert Appointment.__table__.c.id.type.compile(lite) == 'INTEGER'


def test_full_refresh_purge_invalidates_coverage_until_each_source_succeeds():
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Appointment.__table__,
            Transaction.__table__,
            FinancialTransaction.__table__,
            GoodTransaction.__table__,
            Comment.__table__,
            StaffSchedule.__table__,
            SyncState.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add_all([
            Appointment(id=1, company_id=1, date=date(2025, 1, 10), attendance=1),
            FinancialTransaction(
                id=1,
                company_id=1,
                date=datetime(2025, 1, 10, 12),
                amount=100.0,
            ),
            SyncSourceState(
                company_id=1,
                source=sync_pipeline.APPOINTMENTS_SOURCE,
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                synced_at=datetime(2025, 2, 1),
            ),
            SyncSourceState(
                company_id=1,
                source=sync_pipeline.PERSONAL_ACCOUNT_SOURCE,
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                synced_at=datetime(2025, 2, 1),
            ),
            SyncSourceState(
                company_id=1,
                source=sync_pipeline.GOODS_TRANSACTIONS_SOURCE,
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                synced_at=datetime(2025, 2, 1),
            ),
            SyncState(
                key=transactional_state_key(1),
                value='2025-01-31',
            ),
            SyncState(
                key=sync_pipeline.historical_coverage_state_key(1),
                value='2025-01-31',
            ),
        ])
        db.commit()

        assert purge_full_refresh_window(
            db,
            1,
            '2025-01-01',
            '2025-01-31',
            '2025-01-31',
        ) is True
        assert db.query(SyncSourceState).count() == 0
        assert db.get(
            SyncState, sync_pipeline.historical_coverage_state_key(1)
        ) is None

        assert sync_records(
            FakeRecordsAPI([]),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is True
        assert sync_financial_transactions(
            FakeFinancialTransactionsAPI(None),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is False

        appointment_state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': sync_pipeline.APPOINTMENTS_SOURCE},
        )
        financial_state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': sync_pipeline.PERSONAL_ACCOUNT_SOURCE},
        )
        assert (appointment_state.period_start, appointment_state.period_end) == (
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
        assert financial_state is None
        assert sync_pipeline.resolve_company_sync_window(
            db,
            date(2025, 2, 1),
            'incremental',
            1,
        )[1] == 'full'
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_schedules_uses_internal_staff_id_for_tenant_scoped_staff():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Staff.__table__,
            StaffSchedule.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(Staff(id=100, external_id=7, source_type='yclients', name='Master', company_id=1))
        db.commit()

        api = FakeSchedulesAPI([
            {'staff_id': 7, 'date': '2025-01-10', 'slots': [{'from': '09:00', 'to': '10:00'}]},
        ])

        assert sync_pipeline.sync_staff_schedules(
            api,
            db,
            '1',
            start_date='2025-01-10',
            end_date='2025-01-10',
        ) is True

        schedule = db.query(StaffSchedule).one()
        assert schedule.staff_id == 100
        coverage = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': sync_pipeline.STAFF_SCHEDULE_SOURCE},
        )
        assert (coverage.period_start, coverage.period_end) == (
            date(2025, 1, 10),
            date(2025, 1, 10),
        )
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_schedules_deduplicates_slots_and_clears_empty_window():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Staff.__table__,
            StaffSchedule.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(Staff(id=100, external_id=7, source_type='yclients', name='Admin', company_id=1))
        db.commit()

        duplicated = {
            'staff_id': 7,
            'date': '2025-01-10',
            'slots': [
                {'from': '09:00', 'to': '18:00'},
                {'from': '09:00', 'to': '18:00'},
            ],
        }
        assert sync_pipeline.sync_staff_schedules(
            FakeSchedulesAPI([duplicated, dict(duplicated)]),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is True
        assert db.query(StaffSchedule).count() == 1

        assert sync_pipeline.sync_staff_schedules(
            FakeSchedulesAPI([]),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is True
        assert db.query(StaffSchedule).count() == 0
        coverage = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': sync_pipeline.STAFF_SCHEDULE_SOURCE},
        )
        assert (coverage.period_start, coverage.period_end) == (
            date(2025, 1, 1),
            date(2025, 1, 31),
        )
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_schedules_preserves_data_when_source_is_unavailable():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Staff.__table__,
            StaffSchedule.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(Staff(id=100, external_id=7, source_type='yclients', name='Admin', company_id=1))
        db.add(
            StaffSchedule(
                staff_id=100,
                date=date(2025, 1, 10),
                slot_from=time(9),
                slot_to=time(18),
                company_id=1,
            )
        )
        db.commit()

        assert sync_pipeline.sync_staff_schedules(
            FakeSchedulesAPI(None),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is False
        assert db.query(StaffSchedule).count() == 1
        assert db.query(SyncSourceState).count() == 0
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    'payload',
    [
        [{'staff_id': 999, 'date': '2025-01-10', 'slots': [{'from': '09:00', 'to': '18:00'}]}],
        [{'staff_id': 7, 'date': '2025-01-10', 'slots': [{'from': 'bad', 'to': '18:00'}]}],
    ],
)
def test_sync_staff_schedules_rejects_unusable_snapshot_before_replacement(payload):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Staff.__table__,
            StaffSchedule.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(Staff(id=100, external_id=7, source_type='yclients', name='Admin', company_id=1))
        db.add(
            StaffSchedule(
                staff_id=100,
                date=date(2025, 1, 10),
                slot_from=time(10),
                slot_to=time(22),
                company_id=1,
            )
        )
        db.commit()

        assert sync_pipeline.sync_staff_schedules(
            FakeSchedulesAPI(payload),
            db,
            '1',
            start_date='2025-01-01',
            end_date='2025-01-31',
        ) is False

        schedule = db.query(StaffSchedule).one()
        assert (schedule.staff_id, schedule.slot_from, schedule.slot_to) == (
            100,
            time(10),
            time(22),
        )
        assert db.query(SyncSourceState).count() == 0
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_marks_missing_staff_as_fired():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__, Staff.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(Staff(id=1, name='Existing', company_id=1, fired=0))
        db.add(Staff(id=2, name='Stale', company_id=1, fired=0))
        # Portal-created row: absent from YClients by design, fired flag owned by the portal.
        db.add(Staff(id=900, name='Portal Manager', company_id=1, fired=0, portal_user_id=42))
        db.commit()

        api = FakeYClientsAPI([
            {
                'id': 1,
                'name': 'Existing',
                'email': 'Existing.Worker@Example.COM',
                'fired': 0,
                'position': {'title': 'Барбер'},
            },
        ])

        assert sync_staff(api, db, '1') is True

        active = db.get(Staff, 1)
        stale = db.get(Staff, 2)
        assert active.email == 'existing.worker@example.com'
        assert active.fired == 0
        assert stale.fired == 1
        assert db.get(Staff, 900).fired == 0
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_ignores_invalid_staff_email():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__, Staff.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        api = FakeYClientsAPI([
            {
                'id': 1,
                'name': 'Worker',
                'email': 'worker.1@portal.local',
                'fired': 0,
            },
        ])

        assert sync_staff(api, db, '1') is True
        assert db.get(Staff, 1).email is None
    finally:
        db.close()
        engine.dispose()


def test_sync_services_writes_shared_ids_to_branch_scoped_catalog():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[Group.__table__, Company.__table__, Service.__table__, ServiceCatalog.__table__],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon 1', group_id=1))
        db.add(Company(id=2, title='Salon 2', group_id=1))
        db.commit()

        service_payload = [{
            'id': 10,
            'title': 'Воск',
            'price_min': 500.0,
            'duration': 900,
            'category': {'id': 100, 'title': 'Уход'},
        }]

        assert sync_services(FakeServicesAPI(service_payload), db, '1') is True
        assert sync_services(FakeServicesAPI(service_payload), db, '2') is True

        assert db.query(Service).count() == 1
        rows = (
            db.query(ServiceCatalog)
            .filter(ServiceCatalog.service_id == 10)
            .order_by(ServiceCatalog.company_id)
            .all()
        )
        assert [(row.company_id, row.service_id, row.title) for row in rows] == [
            (1, 10, 'Воск'),
            (2, 10, 'Воск'),
        ]
    finally:
        db.close()
        engine.dispose()


def test_sync_services_fills_category_from_category_filtered_services():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Service.__table__,
            ServiceCatalog.__table__,
            ServiceCategoryCatalog.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon 1', group_id=1))
        db.add(ServiceCategoryCatalog(company_id=1, category_id=100, title='Уход', updated_at=datetime(2025, 1, 1, 0, 0, 0)))
        db.commit()

        api = FakeServicesAPI(
            [{'id': 10, 'title': 'Воск', 'price_min': 500.0, 'duration': 900}],
            services_by_category={100: [{'id': 10, 'title': 'Воск'}]},
        )
        assert sync_services(api, db, '1') is True

        row = db.get(ServiceCatalog, {'company_id': 1, 'service_id': 10})
        assert row.category_id == 100
        assert row.category_title == 'Уход'
    finally:
        db.close()
        engine.dispose()


def test_sync_services_preserves_catalog_when_snapshot_is_incomplete():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Service.__table__,
            ServiceCatalog.__table__,
            ServiceCategoryCatalog.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(Service(id=10, title='Care', company_id=1))
        db.add(ServiceCategoryCatalog(
            company_id=1,
            category_id=100,
            title='Care category',
            updated_at=datetime(2025, 1, 1),
        ))
        db.add(ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Care',
            category_id=100,
            category_title='Care category',
            is_active=True,
            updated_at=datetime(2025, 1, 1),
        ))
        db.commit()

        assert sync_services(
            FakeServicesAPI(
                [{'id': 10, 'title': 'Care'}],
                services_by_category={100: None},
            ),
            db,
            '1',
        ) is False
        assert sync_services(FakeServicesAPI([{'title': 'Missing id'}]), db, '1') is False

        catalog = db.get(ServiceCatalog, {'company_id': 1, 'service_id': 10})
        assert catalog.is_active is True
        assert (catalog.category_id, catalog.category_title) == (100, 'Care category')
    finally:
        db.close()
        engine.dispose()


def test_sync_services_marks_only_current_yclients_catalog_rows_active():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[Group.__table__, Company.__table__, Service.__table__, ServiceCatalog.__table__],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon 1', group_id=1))
        db.add_all([
            Service(id=10, title='Old title', company_id=1),
            Service(id=20, title='Archived', company_id=1),
            ServiceCatalog(
                company_id=1,
                service_id=10,
                title='Old title',
                is_active=True,
                updated_at=datetime(2025, 1, 1),
            ),
            ServiceCatalog(
                company_id=1,
                service_id=20,
                title='Archived',
                is_active=True,
                updated_at=datetime(2025, 1, 1),
            ),
        ])
        db.commit()

        assert sync_services(
            FakeServicesAPI([{
                'id': 10,
                'title': 'Current title',
                'category': {'id': 1, 'title': 'Current'},
            }]),
            db,
            '1',
        ) is True

        current = db.get(ServiceCatalog, {'company_id': 1, 'service_id': 10})
        archived = db.get(ServiceCatalog, {'company_id': 1, 'service_id': 20})
        assert current.title == 'Current title'
        assert current.is_active is True
        assert archived.is_active is False

        assert sync_services(FakeServicesAPI(None), db, '1') is False
        db.refresh(current)
        assert current.is_active is True

        assert sync_services(FakeServicesAPI([]), db, '1') is True
        db.refresh(current)
        assert current.is_active is False
        assert archived.is_active is False
    finally:
        db.close()
        engine.dispose()


def test_sync_goods_transactions_preserves_embedded_titles():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[Group.__table__, Company.__table__, Client.__table__, Staff.__table__, GoodTransaction.__table__],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon 1', group_id=1))
        db.commit()

        txns = [{
            'id': 100,
            'document_id': 10,
            'type_id': 1,
            'good': {'id': 200, 'title': 'Archived pomade'},
            'storage': {'id': 300, 'title': 'Archive shelf'},
            'amount': -1,
            'cost': 1200.0,
            'create_date': '2026-01-02T10:00:00+0300',
        }]

        assert sync_goods_transactions(FakeGoodsTransactionsAPI(txns), db, '1') is True

        row = db.get(GoodTransaction, 100)
        assert row.good_id == 200
        assert row.good_title == 'Archived pomade'
        assert row.storage_id == 300
        assert row.storage_title == 'Archive shelf'
    finally:
        db.close()
        engine.dispose()
