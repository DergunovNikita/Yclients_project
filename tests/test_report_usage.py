"""Report usage analytics: capture around /dashboard/reports/data and the platform-admin report."""

import asyncio
import logging
import time
from datetime import date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import sessionmaker

import api
import dashboard_reports
import dashboard_routes
import dashboard_service
import report_usage
from api import app
from auth_scope import AccessContext
from models import Company, Group, ReportUsageEvent, Staff, SyncState
from sync_control import SyncControlService

PARAMS = {'start_date': '2026-03-01', 'end_date': '2026-03-31'}
REPORT = 'bookings_dynamics'


def _ctx(role='owner', user_id=1, account=1, staff_id=None, **kwargs):
    return AccessContext.from_user(
        user_id=user_id, role=role, portal_account_id=account, company_ids=[1], staff_id=staff_id, **kwargs
    )


@pytest.fixture
def usage_log():
    """Messages of the usage logger. The app stops `yclients.*` records at its own stdout handler, so
    caplog (a root-logger handler) never sees them."""
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Collect(level=logging.WARNING)
    logger = logging.getLogger('yclients.report_usage')
    logger.addHandler(handler)
    yield records
    logger.removeHandler(handler)


@pytest.fixture
def usage_app(async_session, monkeypatch):
    """Route wiring: test DB for the request and for the separate writer session; `access` picks the principal."""
    state = {'ctx': _ctx(), 'demo': False}

    async def override_db():
        yield async_session

    monkeypatch.setattr(
        report_usage, 'get_async_session_factory', lambda: async_sessionmaker(async_session.bind, expire_on_commit=False)
    )
    app.dependency_overrides[api.get_async_db] = override_db
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = lambda: state['ctx']
    app.dependency_overrides[dashboard_routes.is_demo_request] = lambda: state['demo']
    yield state
    app.dependency_overrides.clear()


async def _seed(session):
    session.add(Group(id=1, title='G'))
    session.add(Company(id=1, title='Salon', group_id=1))
    session.add(Staff(id=5, name='Master', position='Барбер', company_id=1))
    await session.commit()


async def _get(path='/dashboard/reports/data', *, raise_app_exceptions=True, **params):
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        return await client.get(path, params=params or None)


async def _events(session):
    await report_usage.drain()
    session.expire_all()
    return (await session.execute(select(ReportUsageEvent).order_by(ReportUsageEvent.id))).scalars().all()


async def _data(report_id=REPORT, **extra):
    return await _get(report_id=report_id, **PARAMS, **extra)


@pytest.mark.asyncio
async def test_opening_a_report_records_one_event(async_session, usage_app):
    await _seed(async_session)
    usage_app['ctx'] = _ctx('owner', user_id=7)

    response = await _data(
        'cancellation_analysis', company_id=1, granularity='week', compare_previous='true', period_preset='month'
    )

    assert response.status_code == 200
    (row,) = await _events(async_session)
    assert (row.portal_account_id, row.user_id, row.role) == (1, 7, 'owner')
    assert (row.report_id, row.requested_report_id) == ('bookings_dynamics', 'cancellation_analysis')
    assert row.status_code == 200 and row.source_status == response.json()['data']['source_status']
    assert row.compare_used is True and row.staff_filter is False and row.company_filter is True
    assert (row.granularity, row.period_days, row.period_preset) == ('week', 31, 'month')
    assert row.duration_ms >= 0
    assert row.usage_day == dashboard_service.factual_branch_date(row.created_at)


@pytest.mark.asyncio
async def test_canonical_request_stores_no_requested_id_and_flags_default_off(async_session, usage_app):
    await _seed(async_session)

    assert (await _data()).status_code == 200

    (row,) = await _events(async_session)
    assert row.requested_report_id is None
    assert (row.compare_used, row.staff_filter, row.company_filter) == (False, False, False)


@pytest.mark.asyncio
async def test_staff_filter_is_the_requested_value_not_the_clamped_one(async_session, usage_app):
    await _seed(async_session)
    usage_app['ctx'] = _ctx('barber', user_id=9, staff_id=5)

    await _data()  # the personal role is clamped to its own row without asking
    await _data(staff_id=5)

    first, second = await _events(async_session)
    assert (first.staff_filter, second.staff_filter) == (False, True)


@pytest.mark.asyncio
async def test_compare_flag_covers_every_way_to_ask_for_a_comparison(async_session, usage_app):
    await _seed(async_session)

    await _data(compare_start_date='2026-02-01', compare_end_date='2026-02-28')
    await _data(compare_staff_id=5)
    await _data(compare_previous='true')

    assert [row.compare_used for row in await _events(async_session)] == [True, True, True]


@pytest.mark.asyncio
async def test_a_report_that_cannot_compare_is_not_logged_as_compared(async_session, usage_app):
    await _seed(async_session)

    assert (await _data('retention_3_6_12', compare_previous='true')).status_code == 200

    (row,) = await _events(async_session)
    assert row.compare_used is False


@pytest.mark.asyncio
async def test_failures_are_recorded_with_their_status(async_session, usage_app, monkeypatch):
    await _seed(async_session)

    def failing(error):
        async def fetch(*args, **kwargs):
            raise error

        return fetch

    monkeypatch.setattr(dashboard_routes, 'fetch_report_data', failing(ValueError('bad window')))
    bad = await _data()
    monkeypatch.setattr(dashboard_routes, 'fetch_report_data', failing(dashboard_reports.ReportCalculationError('x')))
    unavailable = await _data()
    monkeypatch.setattr(dashboard_routes, 'fetch_report_data', failing(RuntimeError('boom')))
    crashed = await _get(raise_app_exceptions=False, report_id=REPORT, **PARAMS)

    assert (bad.status_code, unavailable.status_code, crashed.status_code) == (400, 503, 500)
    rows = await _events(async_session)
    assert [row.status_code for row in rows] == [400, 503, 500]
    assert all(row.source_status is None for row in rows)


@pytest.mark.asyncio
async def test_refused_requests_are_not_usage(async_session, usage_app):
    await _seed(async_session)
    usage_app['ctx'] = _ctx('manager', money_metrics=frozenset())

    money_gate = await _data('financial_overview')
    unknown = await _data('no_such_report')
    usage_app['ctx'] = _ctx('barber', staff_id=5)
    other_staff = await _data(staff_id=6)
    usage_app['ctx'] = _ctx('barber', staff_id=5)
    other_branch = await _data(company_id=2)
    usage_app['ctx'] = _ctx('manager')
    usage_gate = await _data('report_usage')

    assert [r.status_code for r in (money_gate, unknown, other_staff, other_branch, usage_gate)] == [
        403, 400, 403, 403, 403
    ]
    assert await _events(async_session) == []


@pytest.mark.asyncio
async def test_demo_api_key_and_tenantless_principals_are_not_recorded(async_session, usage_app):
    await _seed(async_session)

    usage_app['demo'] = True
    assert (await _data()).status_code == 200
    usage_app['demo'] = False
    usage_app['ctx'] = AccessContext.api_key()
    assert (await _data()).status_code == 200
    usage_app['ctx'] = _ctx('platform_admin', account=None)
    assert (await _data()).status_code == 200

    assert await _events(async_session) == []


@pytest.mark.asyncio
async def test_platform_admin_with_a_tenant_is_recorded_under_their_role(async_session, usage_app):
    await _seed(async_session)
    usage_app['ctx'] = _ctx('platform_admin', user_id=3)

    assert (await _data()).status_code == 200

    (row,) = await _events(async_session)
    assert (row.role, row.user_id) == ('platform_admin', 3)


@pytest.mark.asyncio
async def test_a_failing_writer_does_not_touch_the_response(async_session, usage_app, monkeypatch, usage_log):
    await _seed(async_session)
    healthy = await _data()
    assert len(await _events(async_session)) == 1

    def broken():
        raise RuntimeError('password=hunter2')

    monkeypatch.setattr(report_usage, 'get_async_session_factory', broken)
    response = await _data()
    await report_usage.drain()

    assert response.status_code == 200 and response.json() == healthy.json()
    assert len(await _events(async_session)) == 1  # nothing was added
    assert usage_log == ['report usage event not recorded: RuntimeError']


@pytest.mark.asyncio
async def test_a_hanging_writer_neither_delays_the_response_nor_outlives_the_timeout(
    async_session, usage_app, monkeypatch, usage_log
):
    await _seed(async_session)

    async def hang(fields):
        await asyncio.sleep(60)

    monkeypatch.setattr(report_usage, '_insert', hang)
    monkeypatch.setattr(report_usage, 'WRITE_TIMEOUT_SECONDS', 0.2)
    began = time.perf_counter()
    response = await _data()
    answered_in = time.perf_counter() - began
    await report_usage.drain()

    assert response.status_code == 200
    assert answered_in < 0.2  # the write was still pending when the response went out
    assert usage_log == ['report usage event not recorded: TimeoutError']


@pytest.mark.asyncio
async def test_writes_pile_up_only_to_the_pending_cap(async_session, usage_app, monkeypatch, usage_log):
    await _seed(async_session)
    release = asyncio.Event()

    async def blocked(fields):
        await release.wait()

    monkeypatch.setattr(report_usage, '_insert', blocked)
    monkeypatch.setattr(report_usage, 'MAX_PENDING_WRITES', 2)
    for _ in range(4):
        assert (await _data()).status_code == 200

    assert len(report_usage._pending) == 2
    assert usage_log == ['report usage event dropped: 2 writes already pending'] * 2
    release.set()
    await report_usage.drain()
    assert not report_usage._pending



# --- Read side ---


def _event(**overrides):
    values = dict(
        portal_account_id=1, user_id=1, role='owner', report_id='bookings_dynamics', requested_report_id=None,
        created_at=datetime(2026, 3, 10, 9, 0), usage_day=date(2026, 3, 10), duration_ms=100, status_code=200,
        source_status='ready', compare_used=False, staff_filter=False, company_filter=False, granularity=None,
        period_days=31, period_preset=None,
    )
    values.update(overrides)
    return ReportUsageEvent(**values)


async def _usage_report(account=1, **params):
    """Read the report as the platform operator looking at `account`; the report is theirs alone."""
    previous = app.dependency_overrides[dashboard_routes.get_dashboard_access]
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = lambda: _ctx('platform_admin', account=account)
    try:
        response = await _get(report_id='report_usage', **{**PARAMS, **params})
    finally:
        app.dependency_overrides[dashboard_routes.get_dashboard_access] = previous
    assert response.status_code == 200, response.text
    data = response.json()['data']
    tables = {t['id']: t for t in data['tables']}
    rows = {row['title']: row for row in tables['report_usage']['rows']}
    return data, rows, tables


@pytest.mark.asyncio
async def test_report_counts_calls_users_and_user_days(async_session, usage_app):
    async_session.add_all([
        _event(user_id=1, created_at=datetime(2026, 3, 10, 9)),
        _event(user_id=1, created_at=datetime(2026, 3, 10, 15), compare_used=True),
        _event(user_id=1, usage_day=date(2026, 3, 11), created_at=datetime(2026, 3, 11, 9)),
        _event(user_id=2, created_at=datetime(2026, 3, 10, 11)),
        _event(user_id=2, status_code=503, created_at=datetime(2026, 3, 10, 12)),
        _event(user_id=2, report_id='peak_load'),
    ])
    await async_session.commit()

    data, rows, tables = await _usage_report()

    row = rows['Записи и неявки']
    assert (row['calls'], row['users'], row['user_days'], row['errors']) == (5, 2, 3, 1)
    assert row['compare_share'] == 20.0
    assert row['last_used'] == '2026-03-11'
    assert rows['Загрузка по дням недели и часам']['calls'] == 1
    cards = {c['label']: c['value'] for c in data['cards']}
    assert cards == {'Отчётов открывали': 2, 'Пользователей': 2, 'Вызовов': 6}
    # Every catalog report is listed, opened or not, and the report itself is not one of its own rows.
    catalog = [d for d in dashboard_reports.REPORT_REGISTRY.values() if d.id != 'report_usage']
    assert len(rows) == len(catalog)
    assert rows['Финансовый обзор']['calls'] == 0 and rows['Финансовый обзор']['median_ms'] is None


@pytest.mark.asyncio
async def test_median_response_time_uses_successful_calls_only(async_session, usage_app):
    odd = [100, 900, 300]
    even = [100, 200, 300, 400]
    async_session.add_all(
        [_event(duration_ms=ms) for ms in odd]
        + [_event(duration_ms=60_000, status_code=500)]
        + [_event(report_id='peak_load', duration_ms=ms) for ms in even]
    )
    await async_session.commit()

    _, rows, _ = await _usage_report()

    assert rows['Записи и неявки']['median_ms'] == 300
    assert rows['Загрузка по дням недели и часам']['median_ms'] == 250


@pytest.mark.asyncio
async def test_report_is_limited_to_the_tenant_period_and_real_users(async_session, usage_app):
    async_session.add_all([
        _event(),
        _event(portal_account_id=2, user_id=50),
        _event(role='platform_admin', user_id=99),
        _event(usage_day=date(2026, 4, 2), created_at=datetime(2026, 4, 2, 9)),
        _event(usage_day=date(2026, 2, 27), created_at=datetime(2026, 2, 27, 9)),
    ])
    await async_session.commit()

    data, rows, _ = await _usage_report()

    assert rows['Записи и неявки']['calls'] == 1
    assert {c['label']: c['value'] for c in data['cards']}['Пользователей'] == 1
    # Last launch is read up to the period end, so April does not leak into March.
    assert rows['Записи и неявки']['last_used'] == '2026-03-10'


@pytest.mark.asyncio
async def test_unused_list_looks_back_sixty_days_from_the_period_end(async_session, usage_app):
    async_session.add_all([
        _event(report_id='bookings_dynamics', usage_day=date(2026, 3, 31)),
        _event(report_id='peak_load', usage_day=date(2026, 1, 31)),  # 59 days before 31.03: still inside
        _event(report_id='booking_channels', usage_day=date(2026, 1, 30)),  # 60 days back: outside
        _event(report_id='service_combos', portal_account_id=2),
    ])
    await async_session.commit()

    _, _, tables = await _usage_report()

    unused = {row['title']: row['last_used'] for row in tables['report_usage_unused']['rows']}
    assert 'Записи и неявки' not in unused and 'Загрузка по дням недели и часам' not in unused
    assert unused['Каналы записи: онлайн и администратор'] == '2026-01-30'
    assert unused['Комбинации услуг'] is None


@pytest.mark.asyncio
async def test_tracking_since_note_warns_when_the_period_starts_earlier(async_session, usage_app):
    async_session.add(_event(usage_day=date(2026, 3, 10)))
    await async_session.commit()

    data, _, _ = await _usage_report()
    later, _, _ = await _usage_report(start_date='2026-03-10')

    assert data['raw']['tracking_since'] == '2026-03-10'
    assert [n['kind'] for n in data['notes']] == ['info', 'warning']
    # A period that starts at the first record still has a «not opened for 60 days» list reaching before it.
    assert [n['kind'] for n in later['notes']] == ['info', 'warning']


@pytest.mark.asyncio
async def test_no_tracking_note_once_the_log_covers_the_period_and_the_unused_window(async_session, usage_app):
    async_session.add(_event(usage_day=date(2025, 12, 1), created_at=datetime(2025, 12, 1, 9)))
    await async_session.commit()

    data, _, _ = await _usage_report(start_date='2026-03-10')

    assert [n['kind'] for n in data['notes']] == ['info']


@pytest.mark.asyncio
async def test_empty_log_says_so(async_session, usage_app):
    data, rows, _ = await _usage_report()

    assert data['raw']['tracking_since'] is None
    assert any(n['kind'] == 'warning' for n in data['notes'])
    assert all(row['calls'] == 0 for row in rows.values())


@pytest.mark.asyncio
async def test_report_usage_is_gated_to_platform_admin(async_session, usage_app):
    await _seed(async_session)
    seen = {}
    for label, ctx in {
        'owner': _ctx('owner'),
        'platform_admin': _ctx('platform_admin'),
        'branch_admin': _ctx('branch_admin'),
        'manager': _ctx('manager'),
        'barber': _ctx('barber', staff_id=5),
        'api_key': AccessContext.api_key(),
    }.items():
        usage_app['ctx'] = ctx
        catalog = await _get('/dashboard/reports')
        data = await _get(report_id='report_usage', **PARAMS)
        seen[label] = ('report_usage' in {r['id'] for r in catalog.json()['data']}, data.status_code)

    assert seen == {
        'owner': (False, 403),
        'platform_admin': (True, 200),
        'branch_admin': (False, 403),
        'manager': (False, 403),
        'barber': (False, 403),
        'api_key': (False, 403),
    }


@pytest.mark.asyncio
async def test_owner_is_refused_the_usage_report_even_with_data_of_their_tenant(async_session, usage_app):
    await _seed(async_session)
    async_session.add(_event(portal_account_id=1))
    await async_session.commit()
    usage_app['ctx'] = _ctx('owner')

    response = await _get(report_id='report_usage', **PARAMS)

    assert response.status_code == 403
    assert 'usage' in response.json()['detail'].lower()
    assert len(await _events(async_session)) == 1  # the seeded row only: the refusal records nothing


@pytest.mark.asyncio
async def test_platform_admin_reads_the_selected_tenant_only(async_session, usage_app):
    await _seed(async_session)
    async_session.add_all([
        _event(portal_account_id=1, user_id=11, role='owner'),
        _event(portal_account_id=2, user_id=22, role='owner'),
        _event(portal_account_id=2, user_id=23, role='manager'),
    ])
    await async_session.commit()
    rows = {}
    for account in (1, 2):
        _, rows[account], _ = await _usage_report(account=account)

    assert rows[1]['Записи и неявки']['calls'] == 1
    assert rows[2]['Записи и неявки']['calls'] == 2


@pytest.mark.asyncio
async def test_platform_admin_without_a_tenant_must_pick_one(async_session, usage_app):
    await _seed(async_session)
    usage_app['ctx'] = _ctx('platform_admin', account=None)

    catalog = await _get('/dashboard/reports')
    response = await _get(report_id='report_usage', **PARAMS)

    assert 'report_usage' in {r['id'] for r in catalog.json()['data']}
    assert response.status_code == 400
    assert response.json()['detail'] == 'X-Portal-Account-Id is required'


@pytest.mark.asyncio
async def test_report_usage_is_hidden_from_demo(async_session, usage_app):
    usage_app['demo'] = True

    catalog = await _get('/dashboard/reports')
    data = await _get(report_id='report_usage', **PARAMS)

    assert 'report_usage' not in {r['id'] for r in catalog.json()['data']}
    assert data.status_code == 404


@pytest.mark.asyncio
async def test_report_usage_validates_granularity_like_every_other_report(async_session, usage_app):
    await _seed(async_session)

    response = await _get(report_id='report_usage', granularity='quarter', **PARAMS)

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_opening_the_usage_report_is_not_itself_usage(async_session, usage_app):
    await _usage_report()

    assert await _events(async_session) == []


@pytest.mark.asyncio
async def test_registry_builder_cannot_run_without_a_tenant(async_session):
    with pytest.raises(RuntimeError):
        await dashboard_reports.fetch_report_data(
            async_session, 'report_usage', date(2026, 3, 1), date(2026, 3, 31)
        )


# --- Retention ---


@pytest.fixture
def sync_db():
    engine = create_engine('sqlite://')
    event.listen(engine, 'connect', lambda conn, _: conn.execute("ATTACH DATABASE ':memory:' AS system"))
    ReportUsageEvent.__table__.create(engine)
    SyncState.__table__.create(engine)
    with sessionmaker(bind=engine)() as session:
        yield session


def test_retention_sweep_deletes_only_rows_past_the_window(sync_db):
    now = datetime(2026, 10, 9, 12)
    sync_db.add_all([
        _event(created_at=now - timedelta(days=401)),
        _event(created_at=now - timedelta(days=399)),
    ])
    sync_db.commit()
    control = SyncControlService()

    assert control.purge_old_report_usage(sync_db, 400, now=now) == 1
    assert sync_db.query(ReportUsageEvent).count() == 1
    assert control.purge_old_report_usage(sync_db, 0, now=now) == 0  # 0 switches the sweep off


def test_retention_sweep_runs_once_per_interval(sync_db):
    now = datetime(2026, 10, 9, 12)
    sync_db.add(_event(created_at=now - timedelta(days=500)))
    sync_db.commit()
    control = SyncControlService()

    assert control.purge_old_report_usage_if_due(sync_db, retention_days=400, now=now) == 1
    sync_db.add(_event(created_at=now - timedelta(days=500)))
    sync_db.commit()
    assert control.purge_old_report_usage_if_due(sync_db, retention_days=400, now=now + timedelta(hours=1)) is None
    assert control.purge_old_report_usage_if_due(sync_db, retention_days=400, now=now + timedelta(hours=25)) == 1
