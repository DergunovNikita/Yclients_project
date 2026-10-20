"""Report comparison: baseline window, who gets one, and what the block carries."""

import logging
from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError, ProgrammingError

import api
import dashboard_reports
import dashboard_routes
import reports_team
from api import app
from auth_scope import AccessContext
from dashboard_reports import (
    _comparison_payload,
    _comparison_rows,
    _previous_window,
    fetch_report_data,
)
from models import Appointment, Client, Company, Group, Staff
from report_payload import ReportCalculationError, ReportInputError


def _payload(**overrides):
    base = {
        'period': {'start': '2026-09-01', 'end': '2026-09-30', 'granularity': 'day'},
        'source_status': 'ready',
        'cards': [],
        'charts': [],
        'tables': [],
    }
    return {**base, **overrides}


def test_previous_window_follows_the_overview_baseline():
    assert _previous_window(date(2026, 9, 1), date(2026, 9, 30), 'month') == (date(2026, 8, 1), date(2026, 8, 31))
    # Without a preset the plain window of equal length right before the period.
    assert _previous_window(date(2026, 9, 1), date(2026, 9, 30), None) == (date(2026, 8, 2), date(2026, 8, 31))
    assert _previous_window(date(2026, 9, 7), date(2026, 9, 13), 'week') == (date(2026, 8, 31), date(2026, 9, 6))


def test_a_period_at_the_start_of_the_calendar_has_no_baseline_and_is_a_client_error():
    for preset in (None, 'week', 'month'):
        with pytest.raises(ReportInputError, match='previous period'):
            _previous_window(date(1, 1, 1), date(1, 1, 31), preset)


def test_a_zero_base_has_no_percent_change():
    rows = _comparison_rows(
        [{'label': 'Новые', 'value': 4, 'format': 'number'}, {'label': 'Выручка', 'value': 150, 'format': 'money'}],
        [{'label': 'Новые', 'value': 0, 'format': 'number'}, {'label': 'Выручка', 'value': 100, 'format': 'money'}],
    )
    assert rows[0]['delta'] == 4
    assert rows[0]['delta_pct'] is None
    assert rows[1]['delta_pct'] == 50.0


def test_percent_change_keeps_precision_for_the_one_rounding_the_page_does():
    # 151.7536 vs 152.5893 is -0.5477 %: the page shows -0.5, as the table cell computed from the same values does.
    # Rounded to two decimals first (-0.55) it would be shown as -0.6 and the two would disagree on one screen.
    (row,) = _comparison_rows(
        [{'label': 'Средний чек', 'value': 151.7535876840698, 'format': 'money'}],
        [{'label': 'Средний чек', 'value': 152.5892980295566, 'format': 'money'}],
    )
    assert row['delta_pct'] == pytest.approx(-0.5477, abs=1e-4)
    assert round(row['delta_pct'], 1) == -0.5


def test_comparison_carries_charts_and_tables_only_for_ids_the_current_payload_has():
    current = _payload(
        charts=[{'id': 'a'}], tables=[{'id': 't1', 'row_key': 'k', 'rows': [{'k': 1}]}, {'id': 'events'}]
    )
    previous = _payload(
        charts=[
            {'id': 'a', 'labels': ['x'], 'datasets': [{'label': 'L', 'data': [1]}], 'title': 'dropped'},
            {'id': 'gone', 'labels': [], 'datasets': []},
        ],
        tables=[
            {'id': 't1', 'row_key': 'k', 'rows': [{'k': 1}], 'columns': []},
            {'id': 'events', 'rows': [{'comment': 'previous window text'}]},
            {'id': 'gone', 'row_key': 'k', 'rows': []},
        ],
    )
    comparison = _comparison_payload(current, previous, None)
    assert comparison['charts'] == [{'id': 'a', 'labels': ['x'], 'datasets': [{'label': 'L', 'data': [1]}]}]
    # `events` has no row_key, so the page cannot pair its rows and the block does not ship them.
    assert comparison['tables'] == [{'id': 't1', 'rows': [{'k': 1}]}]
    assert 'raw' not in comparison
    assert comparison['source_status'] == 'ready'


def test_comparison_table_carries_only_rows_the_current_table_can_pair_with():
    # The comparison run of `service_combos` returns every pair; the page reads only those of the current top.
    current = _payload(tables=[
        {'id': 'combos', 'row_key': 'pair', 'rows': [{'pair': 'A + B'}, {'pair': 'C + D'}]},
        {'id': 'periods', 'row_key': 'period', 'x_kind': 'time', 'rows': [{'period': '2026-09-01'}]},
    ])
    previous = _payload(
        tables=[
            {'id': 'combos', 'row_key': 'pair', 'rows': [{'pair': 'A + B'}, {'pair': 'X + Y'}, {'pair': 'C + D'}]},
            {
                'id': 'periods',
                'row_key': 'period',
                'x_kind': 'time',
                'rows': [{'period': '2026-08-01'}, {'period': '2026-08-02'}],
            },
        ]
    )
    comparison = _comparison_payload(current, previous, None)
    assert comparison['tables'] == [
        {'id': 'combos', 'rows': [{'pair': 'A + B'}, {'pair': 'C + D'}]},
        # Consecutive periods are paired by position, so they are carried whole.
        {'id': 'periods', 'rows': [{'period': '2026-08-01'}, {'period': '2026-08-02'}]},
    ]


@pytest.mark.asyncio
async def test_comparison_is_built_even_when_the_primary_payload_is_partial(monkeypatch):
    async def fake_payload(db, report_id, start, end, *args, **kwargs):
        status = 'partial' if start == date(2026, 9, 1) else 'ready'
        return _payload(
            source_status=status,
            period={'start': start.isoformat(), 'end': end.isoformat(), 'granularity': 'day'},
            cards=[{'label': 'Новые', 'value': 3, 'format': 'number'}],
        )

    monkeypatch.setattr(dashboard_reports, '_fetch_report_payload', fake_payload)
    data = await fetch_report_data(
        None, 'new_vs_returning_cross', date(2026, 9, 1), date(2026, 9, 30),
        compare_previous=True, period_preset='month',
    )
    assert data['source_status'] == 'partial'
    assert data['comparison']['period']['start'] == '2026-08-01'
    assert data['comparison']['source_status'] == 'ready'


@pytest.mark.asyncio
async def test_only_an_explicit_comparison_window_drops_the_preset(monkeypatch):
    primary_presets = []

    async def fake_payload(db, report_id, start, end, *args, for_comparison=False, **kwargs):
        if not for_comparison:
            primary_presets.append(args[-1] if args else kwargs.get('period_preset'))
        return _payload(period={'start': start.isoformat(), 'end': end.isoformat()})

    monkeypatch.setattr(dashboard_reports, '_fetch_report_payload', fake_payload)
    window = (date(2026, 9, 1), date(2026, 9, 30))
    # Another employee on the same window leaves the preset alone: the deltas inside the tables keep the
    # Overview's base.
    await fetch_report_data(None, 'new_vs_returning_cross', *window, compare_staff_id=7, period_preset='month')
    await fetch_report_data(
        None, 'new_vs_returning_cross', *window, compare_staff_id=7, compare_previous=True, period_preset='month',
    )
    # A window typed by hand governs the whole page.
    await fetch_report_data(
        None, 'new_vs_returning_cross', *window,
        compare_start=date(2026, 7, 1), compare_end=date(2026, 7, 31), period_preset='month',
    )
    assert primary_presets == ['month', 'month', None]


@pytest.mark.asyncio
async def test_a_failed_primary_still_reports_the_comparison_status(monkeypatch):
    async def fake_payload(db, report_id, start, end, *args, **kwargs):
        status = 'ready' if start == date(2026, 9, 1) else 'partial'
        return _payload(source_status=status, period={'start': start.isoformat(), 'end': end.isoformat()})

    monkeypatch.setattr(dashboard_reports, '_fetch_report_payload', fake_payload)
    data = await fetch_report_data(
        None, 'new_vs_returning_cross', date(2026, 9, 1), date(2026, 9, 30), compare_previous=True,
    )
    assert data['comparison']['source_status'] == 'partial'


@pytest.mark.asyncio
async def test_a_report_that_cannot_compare_gets_no_comparison(monkeypatch):
    async def fake_payload(db, report_id, start, end, *args, **kwargs):
        return _payload()

    monkeypatch.setattr(dashboard_reports, '_fetch_report_payload', fake_payload)
    data = await fetch_report_data(
        None, 'year_over_year', date(2026, 9, 1), date(2026, 9, 30), compare_previous=True,
    )
    assert 'comparison' not in data


@pytest.mark.asyncio
async def test_a_report_without_comparison_does_not_fail_on_a_window_it_never_uses(monkeypatch):
    async def fake_payload(db, report_id, start, end, *args, **kwargs):
        return _payload()

    monkeypatch.setattr(dashboard_reports, '_fetch_report_payload', fake_payload)
    # The calendar's first days have no previous period; a report that ignores the comparison must still open.
    data = await fetch_report_data(
        None, 'year_over_year', date(1, 1, 2), date(1, 1, 31), compare_previous=True,
    )
    assert 'comparison' not in data
    with pytest.raises(ReportInputError, match='previous period'):
        await fetch_report_data(
            None, 'new_vs_returning_cross', date(1, 1, 2), date(1, 1, 31), compare_previous=True,
        )


@pytest.mark.asyncio
async def test_deliberate_refusals_are_report_input_errors():
    with pytest.raises(ReportInputError):
        await fetch_report_data(None, 'no_such_report', date(2026, 9, 1), date(2026, 9, 30))
    with pytest.raises(ReportInputError):
        await fetch_report_data(None, 'new_vs_returning_cross', date(2026, 9, 1), date(2026, 9, 30), granularity='hour')
    with pytest.raises(ReportInputError):
        await fetch_report_data(None, 'new_vs_returning_cross', date(2026, 9, 30), date(2026, 9, 1))


@pytest.mark.asyncio
async def test_leaderboard_retries_only_database_failures(async_session, monkeypatch):
    messages = []

    class Collect(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    # The app stops `yclients.*` records at its own handler, so caplog (a root handler) never sees them.
    logger = logging.getLogger('yclients.reports')
    handler = Collect(level=logging.INFO)
    logger.addHandler(handler)
    monkeypatch.setattr(logger, 'level', logging.INFO)
    monkeypatch.setattr(logger, 'disabled', False)

    def failing_with(error):
        async def fetch_plan_fact(*args, **kwargs):
            raise error

        return fetch_plan_fact

    window = (date(2026, 9, 1), date(2026, 9, 30))
    monkeypatch.setattr(
        reports_team, 'fetch_plan_fact', failing_with(OperationalError('SELECT 1', {}, OSError('gone')))
    )
    try:
        with pytest.raises(ReportCalculationError) as raised:
            await fetch_report_data(async_session, 'staff_leaderboard', *window, company_id=1, staff_id=7)
    finally:
        logger.removeHandler(handler)
    assert raised.value.stage == 'plan_fact'
    assert any(message.startswith('staff_leaderboard failed') for message in messages)
    # A person id stays out of the log line.
    assert not any('staff_id' in message for message in messages)

    # A defect in the calculation is not retryable: it must surface as itself (a 500), not as a 503.
    monkeypatch.setattr(reports_team, 'fetch_plan_fact', failing_with(ValueError('broken row')))
    with pytest.raises(ValueError, match='broken row'):
        await fetch_report_data(async_session, 'staff_leaderboard', *window, company_id=1)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('error', 'retryable'),
    [
        (DBAPIError('SELECT 1', {}, Exception('connection is closed'), connection_invalidated=True), True),
        (InterfaceError('SELECT 1', {}, Exception('connection lost')), True),
        (OSError('network unreachable'), True),
        # asyncpg's QueryCanceledError (statement_timeout) arrives as a plain DBAPIError: it would
        # time out again on the same window, and a programming error is a defect.
        (DBAPIError('SELECT 1', {}, Exception('canceling statement due to statement timeout')), False),
        (ProgrammingError('SELECT 1', {}, Exception('relation does not exist')), False),
    ],
)
async def test_leaderboard_retries_only_connection_level_failures(async_session, monkeypatch, error, retryable):
    async def fetch_plan_fact(*args, **kwargs):
        raise error

    monkeypatch.setattr(reports_team, 'fetch_plan_fact', fetch_plan_fact)
    expected = ReportCalculationError if retryable else type(error)

    with pytest.raises(expected) as raised:
        await fetch_report_data(async_session, 'staff_leaderboard', date(2026, 9, 1), date(2026, 9, 30), company_id=1)

    assert isinstance(raised.value, ReportCalculationError) is retryable


@pytest.mark.asyncio
async def test_the_preset_survives_compare_previous_but_not_an_explicit_window(monkeypatch):
    seen = []

    async def fake_payload(
        db, report_id, start, end, company_id, staff_id, granularity, allowed, factual_at, preset, for_comparison=False,
        staff_position_ids=None,
    ):
        seen.append((start, end, preset))
        return _payload()

    monkeypatch.setattr(dashboard_reports, '_fetch_report_payload', fake_payload)
    await fetch_report_data(
        None, 'new_vs_returning_cross', date(2026, 9, 1), date(2026, 9, 30),
        compare_previous=True, period_preset='month',
    )
    # explicit dates win over compare_previous and null the preset, as before
    await fetch_report_data(
        None, 'new_vs_returning_cross', date(2026, 9, 1), date(2026, 9, 30),
        compare_start=date(2026, 7, 1), compare_end=date(2026, 7, 31), compare_previous=True, period_preset='month',
    )
    assert seen == [
        (date(2026, 9, 1), date(2026, 9, 30), 'month'),
        (date(2026, 8, 1), date(2026, 8, 31), None),
        (date(2026, 9, 1), date(2026, 9, 30), None),
        (date(2026, 7, 1), date(2026, 7, 31), None),
    ]


async def _seed_personal_role(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=2, name='Own', position='Барбер', company_id=1, fired=0, portal_user_id=2),
        Staff(id=3, name='Colleague', position='Барбер', company_id=1, fired=0),
        Client(id=1, name='A', company_id=1),
        Client(id=2, name='B', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=2, client_id=1, date=date(2026, 9, 3), attendance=1),
        Appointment(id=2, company_id=1, staff_id=2, client_id=2, date=date(2026, 8, 5), attendance=1),
    ])
    await async_session.commit()


@pytest.mark.asyncio
async def test_a_personal_role_gets_no_self_comparison_unless_it_asks(async_session):
    await _seed_personal_role(async_session)

    async def override_db():
        yield async_session

    async def override_access():
        return AccessContext.from_user(
            user_id=2, role='barber', portal_account_id=1, company_ids=[1], staff_id=2, staff_keys=((1, 2),),
        )

    app.dependency_overrides[api.get_async_db] = override_db
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = override_access
    params = {
        'report_id': 'new_vs_returning_cross', 'start_date': '2026-09-01', 'end_date': '2026-09-30',
        'period_preset': 'month',
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        plain = await client.get('/dashboard/reports/data', params=params)
        asked = await client.get('/dashboard/reports/data', params={**params, 'compare_previous': 'true'})
        other_staff = await client.get(
            '/dashboard/reports/data', params={**params, 'compare_staff_id': 3},
        )
    app.dependency_overrides.clear()

    assert plain.status_code == 200
    assert 'comparison' not in plain.json()['data']
    assert asked.status_code == 200
    comparison = asked.json()['data']['comparison']
    # Own row, previous *month* — the Overview baseline of a preset month, not the preceding 30 days.
    assert comparison['staff_id'] == 2
    assert comparison['period']['start'] == '2026-08-01'
    assert comparison['period']['end'] == '2026-08-31'
    # A colleague stays out of reach, as everywhere else.
    assert other_staff.status_code == 403


@pytest.mark.asyncio
async def test_compare_previous_without_a_preset_uses_the_preceding_window(async_session):
    await _seed_personal_role(async_session)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    params = {
        'report_id': 'new_vs_returning_cross', 'start_date': '2026-09-01', 'end_date': '2026-09-30',
        'compare_previous': 'true',
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.get('/dashboard/reports/data', params=params)
        explicit = await client.get('/dashboard/reports/data', params={
            **params, 'compare_start_date': '2026-07-01', 'compare_end_date': '2026-07-31',
        })
    app.dependency_overrides.clear()

    assert response.json()['data']['comparison']['period']['start'] == '2026-08-02'
    assert explicit.json()['data']['comparison']['period']['start'] == '2026-07-01'
