"""The Overview of a selected administrator shows his own Plan/fact row, his OPZ by day, and the money gates hold."""
import logging
from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

import api
import auth_deps
import dashboard_service
from api import app
from clock import utcnow
from conftest import issue_access_token
from helpers import cached_password_hash
from models import (
    Appointment,
    Company,
    Group,
    ManualFactMetric,
    PortalAccount,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    ServiceLabel,
    Staff,
    Transaction,
)
from plan_config import ADMIN_METRIC_CODES

NOW = datetime(2025, 12, 31, 12, 0)
MARCH = (date(2025, 3, 1), date(2025, 3, 31))
ADMIN_ID = 3


def _visit(id, client_id, day, *, staff_id=1, created=None, attendance=1):
    return Appointment(
        id=id, company_id=1, client_id=client_id, staff_id=staff_id, date=day,
        datetime=datetime.combine(day, datetime.min.time()).replace(hour=12),
        attendance=attendance, create_date=created or datetime(2024, 1, 1, 9),
    )


def _manual(month, value, code='opz_qty'):
    first = date(2025, month, 1)
    last = date(2025, month, 28 if month == 2 else 31)
    return ManualFactMetric(
        period_start=first, period_end=last, company_id=1, staff_id=ADMIN_ID,
        metric_code=code, value=value, updated_at=datetime(2025, month, 28, 10),
    )


async def _seed(async_session, *, manual):
    async_session.add_all([
        Group(id=1, title='Group'),
        Company(id=1, title='Salon', timezone='UTC', group_id=1),
        Staff(id=1, name='Barber one', position='Барбер', company_id=1),
        Staff(id=2, name='Barber two', position='Барбер', company_id=1),
        Staff(id=ADMIN_ID, name='Admin', position='Администратор', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        # February: a repeat visit of client 2 (first seen in January), rebooked; the base of the deltas.
        _visit(102, 2, date(2025, 2, 10)),
        _visit(103, 2, date(2025, 3, 25), attendance=0, created=datetime(2025, 2, 10, 13)),
        # March: client 1 comes on the 3rd and again on the 20th, rebooked after each; clients 2 and 3 once.
        _visit(1, 1, date(2025, 3, 3)),
        _visit(2, 1, date(2025, 3, 20), staff_id=2, created=datetime(2025, 3, 3, 13)),
        _visit(3, 1, date(2025, 4, 10), attendance=0, created=datetime(2025, 3, 20, 13)),
        _visit(4, 2, date(2025, 1, 10)),
        _visit(5, 2, date(2025, 3, 5)),
        _visit(6, 3, date(2025, 3, 7)),
        *manual,
    ])
    await async_session.commit()


def _plan_fact_row(plan_fact):
    group = next(group for group in plan_fact['groups'] if group.get('staff_id') == ADMIN_ID)
    return {metric['code']: metric['fact'] for metric in group['metrics']}


async def _summary(async_session, **kwargs):
    async_session.info.clear()
    return await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, staff_id=ADMIN_ID, factual_at=NOW, period_preset='month', **kwargs,
    )


@pytest.mark.asyncio
async def test_the_block_is_the_administrators_plan_fact_row_manual_top_up_included(async_session):
    await _seed(async_session, manual=[_manual(3, 3.0), _manual(3, 2.0, 'reviews_qty')])

    summary = await _summary(async_session)

    assert summary['service_attribution']['mode'] == 'administrator_schedule'
    async_session.info.clear()
    row = _plan_fact_row(
        await dashboard_service.fetch_plan_fact(async_session, *MARCH, company_id=1, staff_id=ADMIN_ID, factual_at=NOW),
    )
    metrics = summary['administrator_personal']['metrics']
    assert set(metrics) == set(ADMIN_METRIC_CODES)
    assert {code: metric['value'] for code, metric in metrics.items()} == row
    # Two calculated events and three added by hand, over the four visits credited to him.
    assert (row['opz_qty'], row['clients'], row['reviews_qty']) == (5.0, 4.0, 2.0)
    assert summary['administrator_personal']['opz_manual_qty'] == 3.0
    # The OPZ cards and the block are one number.
    assert summary['visit_metrics']['opz_qty'] == metrics['opz_qty']['value']
    assert summary['visit_metrics']['opz_pct'] == pytest.approx(metrics['opz_pct']['value'], abs=0.01)
    calculated = summary['visit_metrics']['opz_first_visit_qty'] + summary['visit_metrics']['opz_repeat_visit_qty']
    assert calculated + summary['administrator_personal']['opz_manual_qty'] == metrics['opz_qty']['value']


@pytest.mark.asyncio
async def test_the_block_carries_the_extra_services_of_the_visits_credited_to_him(async_session):
    await _seed(async_session, manual=[])
    async_session.add(ServiceLabel(
        service_id=10, company_id=1, is_extra=True, source='dashboard', updated_at=datetime(2025, 1, 1),
    ))
    await async_session.flush()
    # March's four visits are all credited to the only administrator, whoever the master was.
    async_session.add_all([
        Transaction(id=1, appointment_id=1, company_id=1, service_id=10, service_title='Extra', amount=2),
        Transaction(id=2, appointment_id=2, company_id=1, service_id=10, service_title='Extra', amount=4),
        Transaction(id=3, appointment_id=6, company_id=1, service_id=10, service_title='Extra', amount=1),
    ])
    await async_session.commit()

    summary = await _summary(async_session)

    metrics = summary['administrator_personal']['metrics']
    assert metrics['extra_services_qty']['value'] == 7.0
    assert metrics['extra_services_pct']['value'] == 175.0
    async_session.info.clear()
    plan_fact = await dashboard_service.fetch_plan_fact(async_session, *MARCH, company_id=1, factual_at=NOW)
    branch = {metric['code']: metric['fact'] for metric in plan_fact['parent_group']['metrics']}
    assert branch['extra_services_qty'] == metrics['extra_services_qty']['value']


@pytest.mark.asyncio
async def test_the_block_compares_with_the_previous_window_and_its_own_manual_top_up(async_session):
    await _seed(async_session, manual=[_manual(3, 3.0), _manual(2, 2.0)])

    summary = await _summary(async_session)

    metrics = summary['administrator_personal']['metrics']
    # February: one repeat event and the manual 2 over one visit credited to him.
    assert metrics['opz_qty']['previous_value'] == 3.0
    assert metrics['opz_qty']['change_pct'] == pytest.approx(66.67, abs=0.01)
    assert metrics['clients']['previous_value'] == 1.0
    assert metrics['clients']['change_pct'] == 300.0
    assert metrics['opz_pct']['previous_value'] == 300.0
    assert summary['visit_metrics']['opz_qty_change_pct'] == pytest.approx(66.67, abs=0.01)
    # The cards cannot tell "no such visits last month" from 0 % without the previous values: the summary carries them.
    for part in ('first', 'repeat'):
        assert f'opz_{part}_visit_pct_previous' in summary['visit_metrics']


@pytest.mark.asyncio
async def test_a_barber_overview_has_no_administrator_block(async_session):
    await _seed(async_session, manual=[])

    async_session.info.clear()
    summary = await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, staff_id=1, factual_at=NOW, period_preset='month',
    )

    assert summary['service_attribution']['mode'] == 'master'
    assert 'administrator_personal' not in summary


@pytest.mark.asyncio
async def test_the_daily_opz_series_sums_to_the_calculated_cards(async_session):
    await _seed(async_session, manual=[_manual(3, 3.0)])

    summary = await _summary(async_session)
    async_session.info.clear()
    daily = await dashboard_service.fetch_revenue_daily(
        async_session, *MARCH, company_id=1, staff_id=ADMIN_ID, factual_at=NOW, administrator_attribution=True,
    )
    async_session.info.clear()
    as_master = await dashboard_service.fetch_revenue_daily(
        async_session, *MARCH, company_id=1, staff_id=ADMIN_ID, factual_at=NOW,
    )

    visit_metrics = summary['visit_metrics']
    calculated = visit_metrics['opz_first_visit_qty'] + visit_metrics['opz_repeat_visit_qty']
    # The manual top-up is a month's number and cannot be spread over days.
    assert sum(row['opz_qty'] for row in daily) == calculated == 2
    assert sum(row['appointments'] for row in daily) == visit_metrics['first_visits'] + visit_metrics['repeat_visits']
    for row in daily:
        if row['appointments']:
            assert row['opz_pct'] == pytest.approx(100.0 * row['opz_qty'] / row['appointments'])
    # Without the Overview's flag the series stays the master's own: an administrator has no such visits.
    assert sum(row['opz_qty'] for row in as_master) == 0
    assert sum(row['appointments'] for row in as_master) == 0


async def _bundle_as(async_session, monkeypatch, role, *, user_id, linked_staff_id=None):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(PortalUser(
        id=user_id, portal_account_id=1, email=f'{role}@example.com', password_hash=cached_password_hash('Passw0rd!x'),
        role=role, is_active=True, email_verified_at=utcnow(), created_at=utcnow(),
    ))
    await async_session.flush()
    async_session.add(PortalUserBranch(user_id=user_id, company_id=1))
    if linked_staff_id is not None:
        staff = await async_session.get(Staff, linked_staff_id)
        staff.portal_user_id = user_id
    await async_session.commit()
    token = await issue_access_token(async_session, user_id, role)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    params = {'start_date': '2025-03-01', 'end_date': '2025-03-31', 'company_id': 1, 'period_preset': 'month'}
    if linked_staff_id is None:
        params['staff_id'] = ADMIN_ID
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(
                '/dashboard/bundle', params=params, headers={'Authorization': f'Bearer {token}'}
            )
    finally:
        app.dependency_overrides.clear()
        monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)
    assert response.status_code == 200, response.text
    return response.json()['data']


MONEY_CODES = {'revenue', 'avg_check_total', 'cosmo_sum'}


@pytest.mark.asyncio
async def test_an_owner_sees_every_metric_of_the_block(async_session, monkeypatch):
    await _seed(async_session, manual=[_manual(3, 3.0)])

    data = await _bundle_as(async_session, monkeypatch, 'owner', user_id=500)

    assert set(data['summary']['administrator_personal']['metrics']) == set(ADMIN_METRIC_CODES)


@pytest.mark.asyncio
async def test_a_manager_loses_the_money_the_default_hides_and_keeps_the_rest(async_session, monkeypatch):
    await _seed(async_session, manual=[_manual(3, 3.0)])

    data = await _bundle_as(async_session, monkeypatch, 'manager', user_id=501)

    # The manager default opens the average check only.
    assert set(data['summary']['administrator_personal']['metrics']) == set(ADMIN_METRIC_CODES) - {
        'revenue', 'cosmo_sum',
    }


@pytest.mark.asyncio
async def test_the_administrator_himself_sees_no_money_by_default(async_session, monkeypatch):
    await _seed(async_session, manual=[_manual(3, 3.0)])

    data = await _bundle_as(async_session, monkeypatch, 'admin', user_id=502, linked_staff_id=ADMIN_ID)

    metrics = data['summary']['administrator_personal']['metrics']
    assert not MONEY_CODES & set(metrics)
    assert metrics['opz_qty']['value'] == 5.0
    assert 'revenue' not in data['summary']
    assert all('revenue' not in row for row in data['revenue_daily'])
    assert sum(row['opz_qty'] for row in data['revenue_daily']) == 2


@pytest.mark.asyncio
async def test_a_plan_fact_below_the_overview_calculation_is_clamped_and_logged(async_session):
    await _seed(async_session, manual=[_manual(3, 3.0)])
    async_session.info.clear()
    current_dr = dashboard_service.DateRange(*MARCH)
    previous_dr = dashboard_service.DateRange(date(2025, 2, 1), date(2025, 2, 28))

    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    # The app logger does not propagate to the root, where caplog listens.
    logger = logging.getLogger('yclients.dashboard')
    handler = Collect(level=logging.DEBUG)
    logger.addHandler(handler)
    try:
        # The truthful calculation (2 and 1 events) leaves the top-up as is and logs nothing.
        block, current_manual, _ = await dashboard_service._administrator_personal_block(
            async_session, current_dr, previous_dr, 1, ADMIN_ID, NOW, (2.0, 1.0),
        )
        assert (current_manual, block['opz_manual_qty']) == (3.0, 3.0)
        assert not records

        async_session.info.clear()
        # An overview that counted more OPZ than Plan/fact: no negative top-up on screen, but a warning.
        block, current_manual, previous_manual = await dashboard_service._administrator_personal_block(
            async_session, current_dr, previous_dr, 1, ADMIN_ID, NOW, (99.0, 99.0),
        )
    finally:
        logger.removeHandler(handler)

    assert (current_manual, previous_manual, block['opz_manual_qty']) == (0.0, 0.0, 0.0)
    assert [(record.levelno, record.name) for record in records] == [(logging.WARNING, 'yclients.dashboard')]
    assert f'staff={ADMIN_ID}' in records[0].getMessage()


@pytest.mark.asyncio
async def test_the_administrator_attribution_is_computed_once_per_request_and_keyed_by_every_argument(async_session):
    await _seed(async_session, manual=[])
    async_session.info.clear()
    plan_start, plan_end = await dashboard_service._resolve_plan_period(async_session, *MARCH, [1])

    first = await dashboard_service._staff_opz_segments(
        async_session, *MARCH, plan_start, plan_end, 1, ADMIN_ID, NOW,
    )
    again = await dashboard_service._staff_opz_segments(
        async_session, *MARCH, plan_start, plan_end, 1, ADMIN_ID, NOW,
    )
    other_window = await dashboard_service._staff_opz_segments(
        async_session, date(2025, 2, 1), date(2025, 2, 28), plan_start, plan_end, 1, ADMIN_ID, NOW,
    )

    assert again is first
    assert other_window is not first
    assert isinstance(first, tuple) and all(isinstance(segment.events, tuple) for segment in first)

    users = {1: None, 2: None, ADMIN_ID: None}
    kwargs = {'factual_at': NOW, 'with_first_visits': True}
    records = await dashboard_service._admin_assigned_records(
        async_session, *MARCH, 1, [ADMIN_ID], users, [1, 2], **kwargs,
    )
    assert await dashboard_service._admin_assigned_records(
        async_session, *MARCH, 1, [ADMIN_ID], users, [2, 1], **kwargs,
    ) is records
    # A cached answer that carries the first-visit flag serves a caller that does not need it, not the reverse.
    assert await dashboard_service._admin_assigned_records(
        async_session, *MARCH, 1, [ADMIN_ID], users, [1, 2], factual_at=NOW,
    ) is records
    narrower = await dashboard_service._admin_assigned_records(
        async_session, *MARCH, 1, [ADMIN_ID], users, [1], **kwargs,
    )
    assert narrower is not records
    assert len(narrower[ADMIN_ID]) < len(records[ADMIN_ID])
    assert all(hasattr(row, 'is_first_visit') for row in records[ADMIN_ID])
