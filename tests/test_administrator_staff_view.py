"""The portal administrator reads every colleague of their branches; money and manual-fact entry stay narrow."""
import pytest

import dashboard_service
from api import app
from auth_scope import AccessContext
from dashboard_routes import _hide_staff_leaderboard_financials
from test_multiselect_filters import PERIOD, PLAN_FACT, REPORT, STAFF, BUNDLE, _data, _get, _seed
from test_staff_levels import _client

ADMIN_STAFF_ID = 3  # the administrator of Alpha in `_seed`; staff 2 is a colleague barber


@pytest.fixture(autouse=True)
def no_upstream(monkeypatch):
    async def fail(*args, **kwargs):
        raise dashboard_service.yclients_analytics.YClientsAnalyticsError('offline')

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fail)


def _admin():
    return AccessContext.from_user(
        user_id=20, role='admin', portal_account_id=1, company_ids=[1, 2], staff_id=ADMIN_STAFF_ID,
        staff_keys=((1, ADMIN_STAFF_ID),),
    )


def _barber():
    return AccessContext.from_user(
        user_id=21, role='barber', portal_account_id=1, company_ids=[1, 2], staff_id=2, staff_keys=((1, 2),),
    )


def _codes(node):
    """Every metric `code` anywhere in a payload."""
    if isinstance(node, dict):
        if 'code' in node:
            yield node['code']
        for value in node.values():
            yield from _codes(value)
    elif isinstance(node, list):
        for item in node:
            yield from _codes(item)


@pytest.mark.asyncio
async def test_an_administrator_lists_the_whole_branch_and_a_barber_only_themselves(async_session):
    await _seed(async_session)

    admin_staff = await _data(async_session, STAFF, _admin())
    barber_staff = await _data(async_session, STAFF, _barber())

    assert {row['id'] for row in admin_staff} >= {1, 2, ADMIN_STAFF_ID, 4}
    assert [row['id'] for row in barber_staff] == [2]


@pytest.mark.asyncio
async def test_an_administrator_may_ask_for_a_colleague_and_a_barber_may_not(async_session):
    await _seed(async_session)

    admin = await _get(async_session, BUNDLE, _admin(), **PERIOD, staff_id=1)
    barber = await _get(async_session, BUNDLE, _barber(), **PERIOD, staff_id=1)
    barber_group = await _get(async_session, BUNDLE, _barber(), **PERIOD, staff_ids='1,2')

    assert admin.status_code == 200
    assert barber.status_code == 403 and barber.json()['detail'] == 'Staff member not allowed'
    assert barber_group.status_code == 403
    # The branch scope still applies to the administrator: a branch outside it is refused.
    assert (await _get(async_session, BUNDLE, _admin(), **PERIOD, company_id=3)).status_code == 403


@pytest.mark.asyncio
async def test_an_administrators_plan_fact_has_colleagues_and_the_branch_row_without_revenue(async_session):
    await _seed(async_session)

    data = await _data(async_session, PLAN_FACT, _admin(), **PERIOD, company_id=1)

    assert {group['staff_id'] for group in data['groups']} >= {1, 2, ADMIN_STAFF_ID}
    assert data['parent_group']['scope'] == 'branch'
    codes = set(_codes(data))
    assert 'clients' in codes and 'cosmo_sum' in codes
    assert not codes & {'revenue', 'avg_check_total'}
    assert data['financials_hidden'] is True


@pytest.mark.asyncio
async def test_a_barbers_plan_fact_stays_their_own_row_without_branch_totals(async_session):
    await _seed(async_session)

    data = await _data(async_session, PLAN_FACT, _barber(), **PERIOD, company_id=1)

    assert [group['staff_id'] for group in data['groups']] == [2]
    assert 'parent_group' not in data


@pytest.mark.asyncio
async def test_an_administrator_still_enters_manual_facts_for_themselves_only(async_session):
    await _seed(async_session)

    async def save(company_id, staff_id):
        client = await _client(async_session, _admin())
        try:
            async with client:
                return await client.post('/dashboard/plan/reviews_fact', json={
                    'month': '2025-03', 'company_id': company_id,
                    'items': [{'company_id': company_id, 'staff_id': staff_id, 'value': 4.0, 'previous_value': 5.0}],
                })
        finally:
            app.dependency_overrides.clear()

    # A colleague of the SAME branch: the refusal must come from the self-only rule, not from the branch scope.
    colleague = await save(1, 1)
    own = await save(1, ADMIN_STAFF_ID)
    read = await _get(
        async_session, '/dashboard/plan/reviews_fact', _admin(), month='2025-03', company_id=1, staff_id=1
    )

    assert colleague.status_code == 403 and colleague.json()['detail'] == 'Staff member not allowed'
    assert own.status_code == 200, own.text
    assert read.status_code == 403 and read.json()['detail'] == 'Staff member not allowed'


@pytest.mark.asyncio
async def test_the_administrator_overview_keeps_the_count_cards_and_drops_money(async_session):
    await _seed(async_session)

    bundle = await _data(async_session, BUNDLE, _admin(), **PERIOD)

    revenue = bundle['summary']['revenue']
    assert revenue['appointments'] > 0
    assert not {'total', 'service_revenue', 'goods_revenue', 'topup_revenue', 'extra_service_revenue'} & set(revenue)
    assert not {key for key in revenue if key.endswith('revenue_change_pct') or key == 'change_pct'}
    assert 'average_check' not in bundle['summary']


@pytest.mark.asyncio
async def test_the_ratings_of_an_administrator_expose_neither_revenue_nor_its_ratio(async_session):
    await _seed(async_session)
    async_session.info.clear()

    data = await _data(async_session, REPORT, _admin(), **PERIOD, report_id='staff_leaderboard')

    tables = {table['id']: table for table in data['tables']}
    assert not tables.keys() & {'revenue_barber', 'avg_check_barber', 'avg_check_plan_branch', 'avg_check_plan_staff'}
    cosmo = tables.get('cosmo_barber')
    if cosmo is not None:
        assert 'pct' not in {column['key'] for column in cosmo['columns']}
        assert 'pct' not in {option['key'] for option in cosmo['ranking']['options']}
        assert 'pct' not in cosmo['ranking']['rows_by_metric']
        assert all('pct' not in row for rows in cosmo['ranking']['rows_by_metric'].values() for row in rows)


def test_hiding_revenue_strips_the_cosmetics_share_but_keeps_the_cosmetics_money():
    row = {'staff': 'A', 'qty': 2.0, 'sum': 900.0, 'avg': 450.0, 'pct': 12.5, 'share_pct': 100.0}
    report = {
        'cards': [], 'charts': [],
        'tables': [{
            'id': 'cosmo_barber',
            'columns': [{'key': key} for key in row],
            'rows': [row],
            'ranking': {
                'default_metric': 'sum',
                'options': [{'key': 'sum'}, {'key': 'pct'}],
                'rows_by_metric': {'sum': [row], 'pct': [row]},
            },
        }],
    }

    shown = _hide_staff_leaderboard_financials(report, frozenset({'revenue', 'avg_check'}))

    table = shown['tables'][0]
    assert 'pct' not in {column['key'] for column in table['columns']}
    assert table['rows'] == [{key: value for key, value in row.items() if key != 'pct'}]
    assert list(table['ranking']['rows_by_metric']) == ['sum']
    assert [option['key'] for option in table['ranking']['options']] == ['sum']
    assert table['rows'][0]['sum'] == 900.0
