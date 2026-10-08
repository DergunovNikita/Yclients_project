"""Payment-method report and the manual Yandex Pay editor."""

import math
from datetime import date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select

import api
import auth_deps
import dashboard_reports
import dashboard_service
import payment_methods
from api import app
from auth_scope import AccessContext, can_view_branch_payments
from auth_service import create_access_token, hash_password
from models import (
    AccountCatalog,
    Appointment,
    Company,
    FinancialTransaction,
    Group,
    ManualPaymentAmount,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalMetricVisibility,
    PortalUser,
    PortalUserBranch,
    Staff,
)

FROZEN_NOW = datetime(2026, 9, 15, 12, 0)
AUGUST = (date(2026, 8, 1), date(2026, 8, 31))
# Owner, branch admin (branches 1 and 2 only), manager, admin, barber, demo owner.
USER_IDS = {'owner': 1, 'branch_admin': 2, 'manager': 3, 'admin': 4, 'barber': 5, 'demo': 6}
EDITOR = '/dashboard/payments/yandex_pay'


@pytest.fixture(autouse=True)
def frozen_login(monkeypatch):
    monkeypatch.setattr(dashboard_service, 'factual_now', lambda: FROZEN_NOW)
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)


def _fact(fact_id, company_id, day, amount, account_id, record_id=None, item_type='service'):
    return FinancialTransaction(
        id=fact_id,
        company_id=company_id,
        date=datetime(2026, 8, day, 12) if isinstance(day, int) else day,
        amount=amount,
        account_id=account_id,
        record_id=record_id,
        sold_item_type=item_type,
        master_id=company_id,
    )


def _visit(visit_id, company_id, day, attendance=1):
    return Appointment(
        id=visit_id,
        external_id=visit_id,
        company_id=company_id,
        staff_id=company_id,
        date=day if isinstance(day, date) else date(2026, 8, day),
        attendance=attendance,
    )


PASSWORD_HASH = hash_password('Passw0rd12345!')  # bcrypt is slow: hash once, not per seeded user


def _user(user_id, role, email, **extra):
    return PortalUser(
        id=user_id,
        portal_account_id=1,
        email=email,
        password_hash=PASSWORD_HASH,
        full_name=f'{role} name',
        role=role,
        is_active=True,
        email_verified_at=datetime(2026, 1, 1),
        created_at=datetime(2026, 1, 1),
        **extra,
    )


@pytest_asyncio.fixture
async def seeded(async_session):
    """Branch 1: every account kind; 2: opened mid-month; 3: left mid-month; 4: not opened yet; 6: left."""
    async_session.add_all([
        Group(id=1, title='G1'),
        PortalAccount(id=1, label='Tenant', created_at=datetime(2026, 1, 1)),
        PortalAccount(id=2, label='Other tenant', created_at=datetime(2026, 1, 1)),
        Company(id=1, title='Alpha', group_id=1),
        Company(id=2, title='Beta', group_id=1, reporting_start_date=date(2026, 8, 10)),
        Company(id=3, title='Gamma', group_id=1, reporting_end_date=date(2026, 8, 20)),
        Company(id=4, title='Delta', group_id=1, reporting_start_date=date(2026, 10, 1)),
        Company(id=6, title='Left', group_id=1, reporting_end_date=date(2026, 7, 31)),
        Company(id=9, title='Foreign', group_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        *[PortalBranch(portal_account_id=1, company_id=company) for company in (1, 2, 3, 4, 6)],
        PortalBranch(portal_account_id=2, company_id=9),
        _user(1, 'owner', 'owner@example.com'),
        _user(2, 'branch_admin', 'branch-admin@example.com'),
        _user(3, 'manager', 'manager@example.com'),
        _user(4, 'admin', 'admin@example.com'),
        _user(5, 'barber', 'barber@example.com'),
        _user(6, 'owner', 'demo@example.com', is_demo=True),
        *[PortalUserBranch(user_id=user, company_id=company) for user in (2, 3, 4, 5) for company in (1, 2)],
        *[Staff(id=company, name=f'Staff {company}', position='Барбер', company_id=company) for company in (1, 2, 3)],
        Staff(id=14, name='Admin staff', position='Администратор', company_id=1, portal_user_id=4),
        Staff(id=15, name='Barber staff', position='Барбер', company_id=1, portal_user_id=5),
        AccountCatalog(company_id=1, account_id=10, title='Касса', type=0, updated_at=FROZEN_NOW),
        AccountCatalog(company_id=1, account_id=11, title='Эквайринг', type=1, updated_at=FROZEN_NOW),
        AccountCatalog(company_id=1, account_id=13, title='Бонусы лояльности', type=1, updated_at=FROZEN_NOW),
        AccountCatalog(company_id=1, account_id=14, title='Прочее', type=5, updated_at=FROZEN_NOW),
        AccountCatalog(company_id=2, account_id=20, title='Касса', type=0, updated_at=FROZEN_NOW),
        AccountCatalog(company_id=2, account_id=21, title='Карта', type=1, updated_at=FROZEN_NOW),
    ])
    await async_session.flush()
    async_session.add_all([
        _visit(101, 1, 5),
        _visit(102, 1, 6),
        _visit(103, 1, 7),
        _visit(104, 1, 8, attendance=0),
        _visit(105, 1, date(2026, 7, 31)),
        _visit(201, 2, 5),
        _visit(202, 2, 12),
    ])
    await async_session.flush()
    async_session.add_all([
        _fact(1, 1, 5, 1000.0, 10, 101),  # cash
        _fact(2, 1, 5, 2000.0, 11, 101),  # cashless
        _fact(3, 1, 6, 300.0, 12, 102),  # account missing from the catalog
        _fact(4, 1, 6, 777.0, 13, 102),  # bonus account: not money
        _fact(5, 1, 7, 50.0, 14, 103),  # account of an unknown type
        _fact(6, 1, 8, 999.0, 10, 104),  # visit that did not happen
        _fact(7, 1, 9, 400.0, 11, item_type='goods_transaction'),
        _fact(8, 1, 10, 250.0, 11, item_type='account_replenishment'),
        _fact(9, 1, datetime(2026, 7, 31, 12), 12345.0, 10, 105),  # outside the period
        _fact(10, 2, 5, 800.0, 20, 201),  # before branch 2 opened
        _fact(11, 2, 12, 500.0, 20, 202),
        _fact(12, 2, 12, 700.0, 21, 202),
    ])
    await async_session.commit()
    return async_session


EXPECTED = {
    1: {'revenue': 4000.0, 'cash': 1000.0, 'cashless_yclients': 2650.0, 'other': 350.0},
    2: {'revenue': 1200.0, 'cash': 500.0, 'cashless_yclients': 700.0, 'other': 0.0},
}


@pytest_asyncio.fixture
async def client(seeded):
    async def override_db():
        yield seeded

    app.dependency_overrides[api.get_async_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        yield http
    app.dependency_overrides.clear()


def _auth(role):
    return {'Authorization': f'Bearer {create_access_token(USER_IDS[role], "owner" if role == "demo" else role)}'}


async def _report(client, role='owner', start=AUGUST[0], end=AUGUST[1], **params):
    return await client.get(
        '/dashboard/reports/data',
        headers=_auth(role),
        params={'report_id': 'payment_methods', 'start_date': start, 'end_date': end, **params},
    )


def _month_bounds(month):
    return date(2026, month, 1), date(2026, month, 28 if month == 2 else 30 if month in (4, 6, 9, 11) else 31)


async def _store(session, company_id, amount, month=8, user_id=None, through=None):
    """A stored total; unless `through` says otherwise it covers its whole month."""
    month_start, month_end = _month_bounds(month)
    session.add(ManualPaymentAmount(
        period_start=month_start,
        period_end=month_end,
        company_id=company_id,
        method_code='yandex_pay',
        amount=amount,
        data_through=through or month_end,
        source='dashboard',
        updated_at=datetime(2026, 9, 1, 10),
        updated_by_user_id=user_id,
    ))
    await session.commit()


def _table(payload, table_id):
    return next(table for table in payload['tables'] if table['id'] == table_id)


def _by_company(payload):
    return {branch['company_id']: branch for branch in payload['raw']['branches']}


# --- revenue reconciliation -------------------------------------------------------------


@pytest.mark.asyncio
async def test_buckets_add_up_to_the_overview_revenue(seeded):
    breakdown = await dashboard_service.fetch_revenue_by_account_type(
        seeded, dashboard_service.DateRange(*AUGUST), [1, 2, 3, 4, 6], FROZEN_NOW
    )
    network = 0.0
    for company_id, expected in EXPECTED.items():
        buckets = breakdown[company_id]
        summary = await dashboard_service.fetch_summary(
            seeded, *AUGUST, company_id=company_id, include_appointments_breakdown=False, factual_at=FROZEN_NOW
        )
        assert sum(buckets.values()) == pytest.approx(summary['revenue']['total'])
        assert summary['revenue']['total'] == pytest.approx(expected['revenue'])
        assert buckets == pytest.approx({
            'cash': expected['cash'], 'cashless': expected['cashless_yclients'], 'other': expected['other']
        })
        network += sum(buckets.values())
    network_summary = await dashboard_service.fetch_summary(
        seeded,
        *AUGUST,
        allowed_company_ids=[1, 2, 3, 4, 6],
        include_appointments_breakdown=False,
        factual_at=FROZEN_NOW,
    )
    assert network == pytest.approx(network_summary['revenue']['total'])
    assert set(breakdown) == {1, 2}


@pytest.mark.asyncio
async def test_report_matches_overview_per_branch_and_for_the_network(client, seeded):
    response = await _report(client)
    assert response.status_code == 200, response.text
    payload = response.json()['data']
    branches = _by_company(payload)
    assert set(branches) == {1, 2, 3}  # Delta not opened yet, Left already gone
    for company_id, expected in EXPECTED.items():
        for key, value in expected.items():
            assert branches[company_id][key] == pytest.approx(value)
        summary = await dashboard_service.fetch_summary(
            seeded, *AUGUST, company_id=company_id, include_appointments_breakdown=False, factual_at=FROZEN_NOW
        )
        assert branches[company_id]['revenue'] == pytest.approx(summary['revenue']['total'])
    assert branches[3]['revenue'] == 0.0

    network = await dashboard_service.fetch_summary(
        seeded,
        *AUGUST,
        allowed_company_ids=[1, 2, 3, 4, 6],
        include_appointments_breakdown=False,
        factual_at=FROZEN_NOW,
    )
    assert payload['cards'][0] == {'label': 'Выручка', 'value': pytest.approx(network['revenue']['total']), 'format': 'money'}
    amounts = _table(payload, 'payment_amounts')
    assert [column['key'] for column in amounts['columns']] == ['metric', 'total', 'company_1', 'company_2', 'company_3']
    revenue_row = amounts['rows'][0]
    assert revenue_row['total'] == pytest.approx(network['revenue']['total'])
    # Every form row adds up to the revenue row in every column.
    for column in ('total', 'company_1', 'company_2', 'company_3'):
        assert sum(row[column] or 0 for row in amounts['rows'][1:]) == pytest.approx(revenue_row[column])


# --- Yandex Pay in the report -----------------------------------------------------------


@pytest.mark.asyncio
async def test_yandex_pay_is_carved_out_of_cashless_for_whole_months(client, seeded):
    await _store(seeded, 1, 600.5)
    payload = (await _report(client)).json()['data']
    alpha, beta = _by_company(payload)[1], _by_company(payload)[2]
    assert alpha['yandex_pay'] == 600.5
    assert alpha['cashless'] == pytest.approx(2650.0 - 600.5)
    assert alpha['cashless_yclients'] == 2650.0
    assert alpha['revenue'] == pytest.approx(alpha['cash'] + alpha['cashless'] + alpha['yandex_pay'] + alpha['other'])
    assert beta['yandex_pay'] is None and beta['cashless'] == 700.0
    assert beta['yandex_pay_missing_months'] == ['2026-08']
    assert alpha['yandex_pay_missing_months'] == []
    assert payload['raw']['yandex_pay_applied'] is True
    assert payload['raw']['countable_months'] == ['2026-08']
    warnings = [note['title'] for note in payload['notes'] if note['kind'] == 'warning']
    assert warnings == ['Яндекс Пэй введён не за все месяцы']
    shares = {row['metric']: row for row in _table(payload, 'payment_shares')['rows']}
    assert shares['Яндекс Пэй']['company_1'] == pytest.approx(600.5 / 4000.0 * 100, abs=0.01)
    assert shares['Яндекс Пэй']['company_2'] is None
    assert {card['label']: card['value'] for card in payload['cards']}['Доля Яндекс Пэй'] == pytest.approx(
        600.5 / 5200.0 * 100, abs=0.01
    )


@pytest.mark.asyncio
async def test_partial_period_leaves_yandex_pay_unknown(client, seeded):
    await _store(seeded, 1, 600.0)
    payload = (await _report(client, end=date(2026, 8, 15))).json()['data']
    alpha = _by_company(payload)[1]
    assert payload['raw']['yandex_pay_applied'] is False
    assert alpha['yandex_pay'] is None
    assert alpha['cashless'] == alpha['cashless_yclients']
    assert alpha['yandex_pay_missing_months'] == []
    assert alpha['yandex_pay_applied'] is False
    assert alpha['yandex_pay_unapplied_reason'] == 'value_beyond_period'
    assert alpha['yandex_pay_unapplied_data_through'] == '2026-08-31'
    # Beta has no value: nothing blocks it, it is simply not entered.
    assert _by_company(payload)[2]['yandex_pay_applied'] is True
    titles = [note['title'] for note in payload['notes'] if note['kind'] == 'warning']
    assert titles == ['Яндекс Пэй не учтён', 'Яндекс Пэй введён не за все месяцы']
    text = next(note['text'] for note in payload['notes'] if note['title'] == 'Яндекс Пэй не учтён')
    assert 'после конца периода' in text and 'Alpha (данные по 31.08)' in text
    assert {card['label']: card['value'] for card in payload['cards']}['Доля Яндекс Пэй'] is None
    yandex_row = next(row for row in _table(payload, 'payment_amounts')['rows'] if row['metric'] == 'Яндекс Пэй')
    assert yandex_row['total'] is None and yandex_row['company_1'] is None


@pytest.mark.asyncio
async def test_month_to_date_counts_the_current_month(client, seeded):
    await _store(seeded, 1, 90.0, month=9, through=date(2026, 9, 14))
    payload = (await _report(client, start=date(2026, 9, 1), end=date(2026, 9, 15), company_id=1)).json()['data']
    assert payload['raw']['yandex_pay_applied'] is True
    assert _by_company(payload)[1]['yandex_pay'] == 90.0
    assert _by_company(payload)[1]['yandex_pay_data_through'] == {'2026-09': '2026-09-14'}
    note = next(note for note in payload['notes'] if note['title'] == 'Яндекс Пэй внесён не за весь месяц')
    assert note['kind'] == 'info'
    assert note['text'].startswith('сентябрь 2026: данные по 14.09 — Alpha.')
    assert 'по 15.09' in note['text'] and 'занижена' in note['text']
    # No revenue yet, so every share is unknown rather than a division by zero.
    assert {card['label']: card['value'] for card in payload['cards']}['Доля Яндекс Пэй'] is None


@pytest.mark.asyncio
async def test_period_spanning_a_missing_month_lists_it(client, seeded):
    await _store(seeded, 1, 100.0, month=8)
    payload = (await _report(client, start=date(2026, 7, 1), end=date(2026, 8, 31))).json()['data']
    alpha = _by_company(payload)[1]
    assert alpha['yandex_pay'] == 100.0
    assert alpha['yandex_pay_missing_months'] == ['2026-07']


@pytest.mark.asyncio
async def test_months_that_have_not_started_are_not_listed_as_missing(client, seeded):
    await _store(seeded, 1, 100.0, month=8)
    payload = (await _report(client, start=date(2026, 8, 1), end=date(2026, 12, 31), company_id=1)).json()['data']
    # Today is 15.09: September can be entered, October to December cannot yet.
    assert _by_company(payload)[1]['yandex_pay_missing_months'] == ['2026-09']
    note = next(note for note in payload['notes'] if note['title'] == 'Яндекс Пэй введён не за все месяцы')
    assert 'сентябрь 2026' in note['text'] and 'октябрь' not in note['text'] and 'декабрь' not in note['text']


@pytest.mark.asyncio
async def test_yandex_pay_above_cashless_is_shown_as_is_with_a_warning(client, seeded):
    await _store(seeded, 2, 5000.0)
    payload = (await _report(client)).json()['data']
    beta = _by_company(payload)[2]
    assert beta['cashless'] == pytest.approx(700.0 - 5000.0)
    excess = [note for note in payload['notes'] if note['title'] == 'Яндекс Пэй больше безналичных YClients']
    assert len(excess) == 1 and 'Beta' in excess[0]['text']


@pytest.mark.asyncio
async def test_stored_value_outside_the_reporting_window_is_not_applied(client, seeded):
    # Gamma left on 20.08: its August row is anchored on 31.08, outside the window.
    await _store(seeded, 3, 700.0)
    payload = (await _report(client)).json()['data']
    gamma = _by_company(payload)[3]
    assert gamma['yandex_pay'] is None
    assert gamma['yandex_pay_missing_months'] == []


@pytest.mark.asyncio
async def test_report_shape_for_one_branch_and_the_stacked_chart(client, seeded):
    payload = (await _report(client, company_id=1)).json()['data']
    amounts = _table(payload, 'payment_amounts')
    assert [column['key'] for column in amounts['columns']] == ['metric', 'company_1']
    assert [row['metric'] for row in amounts['rows']] == [
        'Выручка', 'Наличные', 'Безналичные', 'Яндекс Пэй', 'Прочие кассы'
    ]
    stacked, doughnut = payload['charts']
    assert stacked['id'] == 'payment_shares_by_branch' and stacked['stacked'] is True and stacked['type'] == 'bar'
    assert stacked['labels'] == ['Alpha']
    assert doughnut['id'] == 'payment_shares_network' and doughnut['type'] == 'doughnut'
    assert 'stacked' not in doughnut

    network = (await _report(client)).json()['data']
    assert network['charts'][0]['labels'] == ['Alpha', 'Beta', 'Gamma', 'Все филиалы']
    assert [dataset['label'] for dataset in network['charts'][0]['datasets']] == [
        'Наличные', 'Безналичные', 'Яндекс Пэй', 'Прочие кассы'
    ]
    # Shares lead: they are what the report is read for. Both tables wrap their long branch headers.
    assert [table['id'] for table in network['tables']] == ['payment_shares', 'payment_amounts']
    assert all(table['wrap_headers'] is True for table in network['tables'])
    assert [row['metric'] for row in _table(network, 'payment_shares')['rows']][:3] == [
        'Наличные', 'Безналичные', 'Яндекс Пэй'
    ]

    # Branch 2 has nothing in "other": the row disappears there, as it would for any branch set.
    beta_only = (await _report(client, company_id=2)).json()['data']
    assert 'Прочие кассы' not in [row['metric'] for row in _table(beta_only, 'payment_amounts')['rows']]


@pytest.mark.asyncio
async def test_report_ignores_the_staff_filter(client):
    plain = (await _report(client)).json()['data']
    filtered = (await _report(client, staff_id=1)).json()['data']
    assert filtered['raw'] == plain['raw']
    assert filtered['cards'] == plain['cards']


# --- registry ---------------------------------------------------------------------------


def test_report_registry_flags():
    definition = dashboard_reports.REPORT_REGISTRY['payment_methods']
    assert definition.title == 'Формы оплаты'
    assert definition.status == 'ready' and definition.group == 'finance'
    assert definition.filters['staff'] is False
    assert definition.filters['compare'] is False and definition.filters['granularity'] is False
    assert dashboard_reports.report_requires_financials('payment_methods') is True
    assert 'payment_methods' in dashboard_reports.MONEY_REPORTS


# --- access -----------------------------------------------------------------------------


def _ctx(role, money):
    return AccessContext.from_user(1, role, 1, [1], money_metrics=frozenset(money))


def test_access_predicate():
    assert can_view_branch_payments(AccessContext.api_key())
    for role in ('platform_admin', 'owner', 'branch_admin'):
        assert can_view_branch_payments(_ctx(role, {'revenue'})) is True
    assert can_view_branch_payments(_ctx('manager', {'avg_check'})) is False
    assert can_view_branch_payments(_ctx('manager', {'revenue'})) is True
    # Revenue opened for a clamped role must not reveal branch totals.
    assert can_view_branch_payments(_ctx('admin', {'revenue'})) is False
    assert can_view_branch_payments(_ctx('barber', {'revenue'})) is False


async def _catalog_ids(client, role):
    response = await client.get('/dashboard/reports', headers=_auth(role))
    assert response.status_code == 200
    return {item['id'] for item in response.json()['data']}


@pytest.mark.asyncio
async def test_default_roles_see_or_do_not_see_the_report(client):
    for role in ('owner', 'branch_admin'):
        assert 'payment_methods' in await _catalog_ids(client, role)
        assert (await _report(client, role)).status_code == 200
        assert (await client.get(EDITOR, headers=_auth(role), params={'month': '2026-08'})).status_code == 200
    for role in ('manager', 'admin', 'barber'):
        assert 'payment_methods' not in await _catalog_ids(client, role)
        assert (await _report(client, role)).status_code == 403
        assert (await client.get(EDITOR, headers=_auth(role), params={'month': '2026-08'})).status_code == 403
        post = await client.post(EDITOR, headers=_auth(role), json={'month': '2026-08', 'items': []})
        assert post.status_code == 403


@pytest.mark.asyncio
async def test_opened_revenue_admits_a_manager_but_never_admin_or_barber(client, seeded):
    seeded.add_all([
        PortalMetricVisibility(portal_account_id=1, role=role, visible_codes=['revenue'], updated_at=FROZEN_NOW)
        for role in ('manager', 'admin', 'barber')
    ])
    await seeded.commit()

    assert 'payment_methods' in await _catalog_ids(client, 'manager')
    assert (await _report(client, 'manager')).status_code == 200
    assert (await client.get(EDITOR, headers=_auth('manager'), params={'month': '2026-08'})).status_code == 200
    for role in ('admin', 'barber'):
        assert 'payment_methods' not in await _catalog_ids(client, role)
        assert (await _report(client, role)).status_code == 403
        assert (await client.get(EDITOR, headers=_auth(role), params={'month': '2026-08'})).status_code == 403
        post = await client.post(
            EDITOR, headers=_auth(role), json={'month': '2026-08', 'items': [{'company_id': 1, 'value': 1}]}
        )
        assert post.status_code == 403
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 0


@pytest.mark.asyncio
async def test_me_reports_the_same_predicate(client, seeded):
    seeded.add(PortalMetricVisibility(portal_account_id=1, role='admin', visible_codes=['revenue'], updated_at=FROZEN_NOW))
    await seeded.commit()
    flags = {}
    for role in ('owner', 'branch_admin', 'manager', 'admin', 'barber'):
        me = await client.get('/auth/me', headers=_auth(role))
        assert me.status_code == 200, me.text
        flags[role] = me.json()['data']['branch_payments_access']
    assert flags == {'owner': True, 'branch_admin': True, 'manager': False, 'admin': False, 'barber': False}


@pytest.mark.asyncio
async def test_branch_outside_the_scope_is_refused(client):
    assert (await client.get(EDITOR, headers=_auth('branch_admin'), params={'month': '2026-08', 'company_id': 3})).status_code == 403
    assert (await client.get(EDITOR, headers=_auth('owner'), params={'month': '2026-08', 'company_id': 9})).status_code == 403
    assert (await _report(client, 'branch_admin', company_id=3)).status_code == 403
    post = await client.post(
        EDITOR, headers=_auth('branch_admin'), json={'month': '2026-08', 'items': [{'company_id': 3, 'value': 1}]}
    )
    assert post.status_code == 403
    post = await client.post(
        EDITOR,
        headers=_auth('owner'),
        json={'month': '2026-08', 'items': [{'company_id': 9, 'value': 1}]},
    )
    assert post.status_code == 403
    # The branch admin's report covers only the branches they hold.
    scoped = (await _report(client, 'branch_admin')).json()['data']
    assert set(_by_company(scoped)) == {1, 2}


@pytest.mark.asyncio
async def test_demo_reads_but_cannot_write(client, seeded):
    assert (await client.get(EDITOR, headers=_auth('demo'), params={'month': '2026-08'})).status_code == 200
    post = await client.post(
        EDITOR, headers=_auth('demo'), json={'month': '2026-08', 'items': [{'company_id': 1, 'value': 5}]}
    )
    assert post.status_code == 403
    assert post.json()['detail'] == 'Demo account is read-only'
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 0


# --- editor -----------------------------------------------------------------------------


async def _editor(client, role='owner', month='2026-08', **params):
    response = await client.get(EDITOR, headers=_auth(role), params={'month': month, **params})
    assert response.status_code == 200, response.text
    return response.json()['data']


async def _save(client, items, role='owner', month='2026-08', company_id=None):
    return await client.post(
        EDITOR, headers=_auth(role), json={'month': month, 'company_id': company_id, 'items': items}
    )


def _rows(data):
    return {row['company_id']: row for row in data['rows']}


async def _audit_events(session):
    return (
        await session.execute(select(PortalAuditEvent).where(PortalAuditEvent.action == 'manual_payment.updated'))
    ).scalars().all()


@pytest.mark.asyncio
async def test_editor_lists_open_branches_with_the_cashless_hint(client):
    data = await _editor(client)
    assert data['month'] == '2026-08'
    assert data['period'] == {'start': '2026-08-01', 'end': '2026-08-31'}
    assert data['editable'] is True and data['total_value'] == 0
    rows = _rows(data)
    # Branch 6 left before the month began and is gone; Delta has not opened yet but stays enterable.
    assert set(rows) == {1, 2, 3, 4}
    assert rows[1]['cashless_yclients'] == 2650.0 and rows[2]['cashless_yclients'] == 700.0
    assert [rows[company]['counted'] for company in (1, 2, 3, 4)] == [True, True, False, False]
    for row in rows.values():
        assert row['value'] is None
        assert row['updated_at'] is None and row['updated_by'] is None and row['updated_by_name'] is None

    assert (await _editor(client, month='2026-10'))['editable'] is False
    assert set(_rows(await _editor(client, 'branch_admin'))) == {1, 2}
    assert set(_rows(await _editor(client, company_id=2))) == {2}


@pytest.mark.asyncio
async def test_save_quantizes_signs_the_row_and_audits(client, seeded):
    response = await _save(client, [{'company_id': 1, 'value': 600.455, 'previous_value': None}])
    assert response.status_code == 200, response.text
    row = _rows(response.json()['data'])[1]
    assert row['value'] == 600.46  # half-up on kopecks
    assert row['updated_by'] == 1 and row['updated_by_name'] == 'owner name'
    assert row['updated_at'] == FROZEN_NOW.isoformat()
    assert response.json()['data']['total_value'] == 600.46
    stored = (await seeded.execute(select(ManualPaymentAmount))).scalars().one()
    assert (stored.period_start, stored.period_end) == (date(2026, 8, 1), date(2026, 8, 31))
    assert stored.method_code == 'yandex_pay' and float(stored.amount) == 600.46
    (event,) = await _audit_events(seeded)
    assert event.actor_user_id == 1 and event.portal_account_id == 1
    assert event.target_type == 'manual_payment' and event.target_id == '2026-08'
    assert event.metadata_json == {
        'method_code': 'yandex_pay',
        'rows': [{
            'company_id': 1, 'old': None, 'new': 600.46, 'old_data_through': None, 'new_data_through': '2026-08-31'
        }],
    }
    assert row['data_through'] == '2026-08-31' and stored.data_through == date(2026, 8, 31)


@pytest.mark.asyncio
async def test_uncounted_rows_stay_visible_but_stay_out_of_the_total(client):
    await _save(client, [{'company_id': 3, 'value': 40, 'previous_value': None}])
    data = await _editor(client)
    assert _rows(data)[3]['value'] == 40.0 and _rows(data)[3]['counted'] is False
    assert data['total_value'] == 0


@pytest.mark.asyncio
async def test_unchanged_rows_keep_their_author_and_write_no_audit(client, seeded):
    await _save(client, [{'company_id': 1, 'value': 600, 'previous_value': None}], role='owner')
    first = _rows(await _editor(client))[1]

    # The editor posts every rendered row; a branch admin only touches branch 2.
    saved = await _save(
        client,
        [
            {'company_id': 1, 'value': 600, 'previous_value': 600,
             'data_through': '2026-08-31', 'previous_data_through': '2026-08-31'},
            {'company_id': 2, 'value': 75.5, 'previous_value': None},
        ],
        role='branch_admin',
    )
    assert saved.status_code == 200, saved.text
    rows = _rows(saved.json()['data'])
    assert rows[1]['updated_by'] == 1 and rows[1]['updated_at'] == first['updated_at']
    assert rows[2]['updated_by'] == 2 and rows[2]['updated_by_name'] == 'branch_admin name'
    events = await _audit_events(seeded)
    assert len(events) == 2
    assert events[-1].metadata_json['rows'] == [{
        'company_id': 2, 'old': None, 'new': 75.5, 'old_data_through': None, 'new_data_through': '2026-08-31'
    }]
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 2


@pytest.mark.asyncio
async def test_posting_back_the_get_payload_changes_nothing(client, seeded):
    """Empty cells arrive as null and go back as null; only that keeps a blind save a no-op."""
    await _save(client, [{'company_id': 2, 'value': 0, 'previous_value': None}])
    data = await _editor(client)
    assert _rows(data)[2]['value'] == 0.0 and _rows(data)[1]['value'] is None
    events_before = len(await _audit_events(seeded))

    items = [
        {
            'company_id': row['company_id'],
            'value': row['value'],
            'previous_value': row['value'],
            'data_through': row['data_through'],
            'previous_data_through': row['data_through'],
        }
        for row in data['rows']
    ]
    response = await _save(client, items)
    assert response.status_code == 200
    assert len(await _audit_events(seeded)) == events_before
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 1
    assert _rows(response.json()['data'])[1]['value'] is None


@pytest.mark.asyncio
async def test_zero_is_a_value_and_null_clears_it(client, seeded):
    await _save(client, [{'company_id': 1, 'value': 0, 'previous_value': None}])
    assert float((await seeded.execute(select(ManualPaymentAmount.amount))).scalar_one()) == 0.0
    cleared = await _save(
        client, [{'company_id': 1, 'value': None, 'previous_value': 0, 'previous_data_through': '2026-08-31'}]
    )
    assert cleared.status_code == 200
    assert _rows(cleared.json()['data'])[1]['value'] is None
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 0
    events = await _audit_events(seeded)
    assert events[-1].metadata_json['rows'] == [{
        'company_id': 1, 'old': 0.0, 'new': None, 'old_data_through': '2026-08-31', 'new_data_through': None
    }]


@pytest.mark.asyncio
async def test_stale_previous_value_conflicts_and_writes_nothing(client, seeded):
    await _store(seeded, 1, 500.0, user_id=2)

    stale = await _save(
        client,
        [
            {'company_id': 2, 'value': 10, 'previous_value': None},  # valid on its own
            {'company_id': 1, 'value': 700, 'previous_value': 400},
        ],
    )
    assert stale.status_code == 409
    assert stale.json()['detail'] == 'Value was changed by someone else; reload and try again'
    assert [(row.company_id, float(row.amount)) for row in (await seeded.execute(select(ManualPaymentAmount))).scalars()] == [(1, 500.0)]
    assert await _audit_events(seeded) == []

    # Forgetting the lock value is stale too, whether the row exists...
    assert (await _save(client, [{'company_id': 1, 'value': 700}])).status_code == 409
    # ...or the editor still shows a value that somebody has already cleared.
    assert (await _save(client, [{'company_id': 2, 'value': 5, 'previous_value': 9}])).status_code == 409
    clear_stale = await _save(client, [{'company_id': 1, 'value': None, 'previous_value': 400}])
    assert clear_stale.status_code == 409
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 1


@pytest.mark.asyncio
async def test_lost_insert_race_is_a_conflict(client, seeded, monkeypatch):
    """Both editors saw an empty cell; the second INSERT hits the unique index."""
    real_stored = payment_methods._stored_amounts

    async def blind_read(db, month_start, company_ids):
        # Reads as if nobody had written yet, though the row is already there.
        await real_stored(db, month_start, company_ids)
        return {}

    await _store(seeded, 1, 500.0, user_id=2)
    monkeypatch.setattr(payment_methods, '_stored_amounts', blind_read)
    response = await _save(client, [{'company_id': 1, 'value': 700, 'previous_value': None}])
    assert response.status_code == 409
    monkeypatch.undo()
    assert [float(amount) for amount in (await seeded.execute(select(ManualPaymentAmount.amount))).scalars()] == [500.0]
    assert await _audit_events(seeded) == []


@pytest.mark.asyncio
async def test_lost_update_race_is_a_conflict(client, seeded, monkeypatch):
    """The guarded UPDATE finds the row already changed and refuses instead of overwriting."""
    await _store(seeded, 1, 500.0, user_id=2)
    real_stored = payment_methods._stored_amounts

    async def outdated_read(db, month_start, company_ids):
        rows = await real_stored(db, month_start, company_ids)
        # The pre-check sees the value the editor showed; the row has moved on since.
        await db.execute(
            ManualPaymentAmount.__table__.update().values(amount=550)
        )
        return rows

    monkeypatch.setattr(payment_methods, '_stored_amounts', outdated_read)
    response = await _save(
        client, [{'company_id': 1, 'value': 700, 'previous_value': 500, 'previous_data_through': '2026-08-31'}]
    )
    assert response.status_code == 409
    assert await _audit_events(seeded) == []


@pytest.mark.asyncio
async def test_audit_is_written_before_the_commit(client, seeded, monkeypatch):
    order = []
    real_log, real_commit = payment_methods.log_portal_audit, seeded.commit

    async def log(*args, **kwargs):
        order.append('audit')
        await real_log(*args, **kwargs)

    async def commit():
        order.append('commit')
        await real_commit()

    monkeypatch.setattr(payment_methods, 'log_portal_audit', log)
    monkeypatch.setattr(seeded, 'commit', commit)
    assert (await _save(client, [{'company_id': 1, 'value': 5, 'previous_value': None}])).status_code == 200
    assert order == ['audit', 'commit']


@pytest.mark.parametrize(
    'month, items',
    [
        ('August', [{'company_id': 1, 'value': 5}]),
        ('2026-10', [{'company_id': 1, 'value': 5}]),  # a month that has not started
        ('2026-08', [{'company_id': 1, 'value': -1}]),
        ('2026-08', [{'company_id': 1, 'value': 1000000000.01}]),
        # Too wide for Decimal quantization: must be an ordinary 400, not an InvalidOperation 500.
        ('2026-08', [{'company_id': 1, 'value': 1e30}]),
        ('2026-08', [{'company_id': 1, 'value': 5, 'previous_value': 1e300}]),
        ('2026-08', [{'company_id': 1, 'value': 5, 'previous_value': -3}]),
        ('2026-08', [{'company_id': 1, 'value': 5}, {'company_id': 1, 'value': 6}]),
        ('2026-08', [{'company_id': 6, 'value': 5}]),  # left the tenant before the month
    ],
)
@pytest.mark.asyncio
async def test_invalid_saves_are_400_and_write_nothing(client, seeded, month, items):
    response = await _save(client, items, month=month)
    assert response.status_code == 400, response.text
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 0
    assert await _audit_events(seeded) == []


@pytest.mark.asyncio
async def test_an_untouched_empty_row_that_closed_since_loading_does_not_refuse_the_batch(client, seeded):
    # Branch 6 left the tenant after the editor loaded: its blank row is simply not written.
    saved = await _save(
        client,
        [
            {'company_id': 6, 'value': None, 'previous_value': None},
            {'company_id': 1, 'value': 5, 'previous_value': None},
        ],
    )
    assert saved.status_code == 200, saved.text
    assert [(row.company_id, float(row.amount)) for row in (await seeded.execute(select(ManualPaymentAmount))).scalars()] == [(1, 5.0)]
    (event,) = await _audit_events(seeded)
    assert [row['company_id'] for row in event.metadata_json['rows']] == [1]
    # Nothing but the closed blank row: a no-op, not an error.
    assert (await _save(client, [{'company_id': 6, 'value': None, 'previous_value': None}])).status_code == 200


@pytest.mark.asyncio
async def test_a_closed_row_with_a_shown_value_is_a_conflict_and_a_typed_value_is_refused(client, seeded):
    await _store(seeded, 6, 40.0, month=8, user_id=2)
    await seeded.commit()
    stale = await _save(
        client,
        [
            {'company_id': 1, 'value': 5, 'previous_value': None},
            {'company_id': 6, 'value': 40, 'previous_value': 40, 'previous_data_through': '2026-08-31'},
        ],
    )
    assert stale.status_code == 409
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 1
    typed = await _save(client, [{'company_id': 6, 'value': 7, 'previous_value': None}])
    assert typed.status_code == 400
    assert typed.json()['detail'] == 'Payment row is not open for entry'


@pytest.mark.asyncio
async def test_non_finite_numbers_are_rejected(client):
    for literal in ('NaN', 'Infinity', '-Infinity'):
        response = await client.post(
            EDITOR,
            headers={**_auth('owner'), 'Content-Type': 'application/json'},
            content=f'{{"month": "2026-08", "items": [{{"company_id": 1, "value": {literal}}}]}}',
        )
        assert response.status_code in (400, 422), (literal, response.text)


@pytest.mark.asyncio
async def test_the_largest_allowed_value_is_accepted(client):
    response = await _save(client, [{'company_id': 1, 'value': 1000000000, 'previous_value': None}])
    assert response.status_code == 200
    assert _rows(response.json()['data'])[1]['value'] == 1000000000.0


@pytest.mark.asyncio
async def test_bad_month_on_read_is_400_and_selected_company_is_enforced(client):
    response = await client.get(EDITOR, headers=_auth('owner'), params={'month': 'August'})
    assert response.status_code == 400
    mismatch = await _save(client, [{'company_id': 2, 'value': 5}], company_id=1)
    assert mismatch.status_code == 400


@pytest.mark.asyncio
async def test_editor_row_follows_the_stored_value_into_the_report(client, seeded):
    await _save(client, [{'company_id': 1, 'value': 1000, 'previous_value': None}])
    payload = (await _report(client)).json()['data']
    assert _by_company(payload)[1]['yandex_pay'] == 1000.0
    assert _by_company(payload)[1]['cashless'] == pytest.approx(1650.0)


# --- review regressions -----------------------------------------------------------------


async def _post_raw(client, body):
    return await client.post(EDITOR, headers={**_auth('owner'), 'Content-Type': 'application/json'}, content=body)


@pytest.mark.asyncio
async def test_a_boolean_is_not_an_amount(client, seeded):
    for literal in ('true', 'false'):
        response = await _post_raw(
            client, f'{{"month": "2026-08", "items": [{{"company_id": 1, "value": {literal}}}]}}'
        )
        assert response.status_code == 422, (literal, response.text)
    response = await _post_raw(
        client, '{"month": "2026-08", "items": [{"company_id": 1, "value": 5, "previous_value": false}]}'
    )
    assert response.status_code == 422
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 0


@pytest.mark.asyncio
async def test_negative_zero_is_a_plain_zero(client, seeded):
    response = await _post_raw(
        client, '{"month": "2026-08", "items": [{"company_id": 1, "value": -0.0, "previous_value": null}]}'
    )
    assert response.status_code == 200, response.text
    (event,) = await _audit_events(seeded)
    assert str(event.metadata_json['rows'][0]['new']) == '0.0'
    assert str(_rows(response.json()['data'])[1]['value']) == '0.0'


@pytest.mark.asyncio
async def test_a_race_lost_on_a_later_row_undoes_the_rows_already_written(client, seeded, monkeypatch):
    """Row 1 is updated first; row 2 turns out to have moved on — row 1 must not stay changed."""
    await _store(seeded, 1, 100.0)
    await _store(seeded, 2, 200.0)
    real_stored = payment_methods._stored_amounts

    async def outdated_read(db, month_start, company_ids):
        rows = await real_stored(db, month_start, company_ids)
        # A competing save commits between our read and our write — for the second row only.
        await db.execute(ManualPaymentAmount.__table__.update().where(
            ManualPaymentAmount.company_id == 2).values(amount=250))
        await db.commit()
        return rows

    monkeypatch.setattr(payment_methods, '_stored_amounts', outdated_read)
    response = await _save(client, [
        {'company_id': 1, 'value': 111, 'previous_value': 100, 'previous_data_through': '2026-08-31'},
        {'company_id': 2, 'value': 222, 'previous_value': 200, 'previous_data_through': '2026-08-31'},
    ])
    assert response.status_code == 409
    monkeypatch.undo()
    stored = {row.company_id: float(row.amount) for row in (await seeded.execute(select(ManualPaymentAmount))).scalars()}
    assert stored == {1: 100.0, 2: 250.0}
    assert await _audit_events(seeded) == []


@pytest.mark.asyncio
async def test_a_lost_insert_race_after_an_update_undoes_the_update(client, seeded, monkeypatch):
    await _store(seeded, 1, 100.0)
    real_stored = payment_methods._stored_amounts

    async def blind_to_branch_two(db, month_start, company_ids):
        rows = await real_stored(db, month_start, company_ids)
        # Branch 2 gets a value from someone else after our read: our INSERT hits the unique index.
        db.add(ManualPaymentAmount(
            period_start=date(2026, 8, 1), period_end=date(2026, 8, 31), company_id=2, method_code='yandex_pay',
            amount=300, data_through=date(2026, 8, 31), source='dashboard', updated_at=datetime(2026, 9, 1),
        ))
        await db.commit()
        return rows

    monkeypatch.setattr(payment_methods, '_stored_amounts', blind_to_branch_two)
    response = await _save(client, [
        {'company_id': 1, 'value': 111, 'previous_value': 100, 'previous_data_through': '2026-08-31'},
        {'company_id': 2, 'value': 222, 'previous_value': None},
    ])
    assert response.status_code == 409
    monkeypatch.undo()
    stored = {row.company_id: float(row.amount) for row in (await seeded.execute(select(ManualPaymentAmount))).scalars()}
    assert stored == {1: 100.0, 2: 300.0}
    assert await _audit_events(seeded) == []


@pytest.mark.asyncio
async def test_a_branch_outside_the_period_gets_an_empty_state_not_empty_tables(client):
    # Gamma left on 20.08; September has nothing of it. No column, no zero-width charts.
    payload = (await _report(client, start=date(2026, 9, 1), end=date(2026, 9, 30), company_id=3)).json()['data']
    assert payload['raw']['branches'] == []
    assert payload['cards'] == [] and payload['charts'] == [] and payload['tables'] == []
    warnings = [note for note in payload['notes'] if note['kind'] == 'warning']
    assert len(warnings) == 1 and 'нет данных' in warnings[0]['text'].lower()


@pytest.mark.asyncio
async def test_months_in_the_warnings_read_like_a_calendar(client, seeded):
    await _store(seeded, 1, 100.0, month=8)
    payload = (await _report(client, start=date(2026, 7, 1), end=date(2026, 8, 31))).json()['data']
    missing = next(note for note in payload['notes'] if note['title'] == 'Яндекс Пэй введён не за все месяцы')
    assert 'июль 2026' in missing['text'] and '2026-07' not in missing['text']
    # The machine-readable part keeps the ISO month.
    assert _by_company(payload)[1]['yandex_pay_missing_months'] == ['2026-07']


def test_describe_months_squeezes_runs_of_three_or_more():
    assert payment_methods.describe_months(['2026-07']) == 'июль 2026'
    assert payment_methods.describe_months(['2026-08', '2026-07']) == 'июль 2026, август 2026'
    assert payment_methods.describe_months(['2026-01', '2026-02', '2026-03']) == 'январь 2026 – март 2026'
    # Across a year boundary, with a gap in the middle.
    assert payment_methods.describe_months(
        ['2025-11', '2025-12', '2026-01', '2026-02', '2026-04']
    ) == 'ноябрь 2025 – февраль 2026, апрель 2026'


@pytest.mark.asyncio
async def test_a_long_period_names_untouched_branches_once_and_squeezes_gaps(client, seeded):
    await _store(seeded, 1, 100.0, month=8)
    payload = (await _report(client, start=date(2026, 1, 1), end=date(2026, 8, 31))).json()['data']
    note = next(note for note in payload['notes'] if note['title'] == 'Яндекс Пэй введён не за все месяцы')
    # Alpha entered only August: its gap is one run. Beta entered nothing: one name, no months.
    assert note['text'].startswith('Alpha: январь 2026 – июль 2026')
    assert 'Не введён ни за один месяц периода: Beta' in note['text']
    assert note['text'].count('2026') == 2
    # The machine-readable list is not squeezed.
    assert len(_by_company(payload)[1]['yandex_pay_missing_months']) == 7


@pytest.mark.asyncio
async def test_a_negative_cashless_share_is_not_drawn_as_a_doughnut_segment(client, seeded):
    await _store(seeded, 2, 5000.0)
    payload = (await _report(client, company_id=2)).json()['data']
    doughnut = payload['charts'][1]
    assert doughnut['labels'] == ['Наличные', 'Безналичные', 'Яндекс Пэй']
    cash, cashless, yandex = doughnut['datasets'][0]['data']
    # Chart.js draws |value| for an arc, so a negative share would show up as a positive slice.
    assert cashless is None and cash > 0 and yandex > 0
    # The tables keep the number as it is.
    cashless_row = next(row for row in _table(payload, 'payment_shares')['rows'] if row['metric'] == 'Безналичные')
    assert cashless_row['company_2'] < 0


@pytest.mark.asyncio
async def test_a_period_without_revenue_draws_no_empty_charts(client):
    # Branch 1 exists in September but has no facts yet: tables of zeros are honest, empty charts are not.
    payload = (await _report(client, start=date(2026, 9, 1), end=date(2026, 9, 30), company_id=1)).json()['data']
    assert payload['charts'] == []
    assert payload['tables'] and payload['raw']['branches'][0]['revenue'] == 0


@pytest.mark.asyncio
async def test_a_month_in_the_far_future_is_readable_and_not_editable(client):
    data = await _editor(client, month='9999-12')
    assert data['editable'] is False
    assert all(row['cashless_yclients'] == 0 for row in data['rows'])


@pytest.mark.asyncio
async def test_a_float_sum_a_hair_under_yandex_pay_reads_as_zero_not_minus_zero(client, seeded, monkeypatch):
    """Yandex Pay equal to the cashless sum: Postgres adds floats naively, 0.3 + 0.6 is 0.8999999999999999."""
    await _store(seeded, 1, 0.9)

    async def hair_under(db, dr, company_ids, factual_at, by_month=False):
        assert by_month
        return {(1, date(2026, 8, 1)): {'cash': 0.0, 'cashless': 0.3 + 0.6, 'other': 0.0}}

    monkeypatch.setattr(payment_methods, 'fetch_revenue_by_account_type', hair_under)
    payload = (await _report(client, company_id=1)).json()['data']
    (alpha,) = payload['raw']['branches']
    assert alpha['cashless'] == 0.0 and math.copysign(1.0, alpha['cashless']) > 0
    assert not [note for note in payload['notes'] if note['title'] == 'Яндекс Пэй больше безналичных YClients']
    cells = [row[column] for table in payload['tables'] for row in table['rows'] for column in row if column != 'metric']
    assert all(math.copysign(1.0, cell) > 0 for cell in cells if cell == 0)


# --- monthly trend ----------------------------------------------------------------------

JULY_AUGUST = (date(2026, 7, 1), date(2026, 8, 31))
JULY = date(2026, 7, 1)
AUGUST_START = date(2026, 8, 1)


async def _seed_july(session):
    """July activity of branch 1 on top of `seeded`: its 12 345 cash on 31.07 is already there."""
    session.add_all([
        # Paid in August for the visit of 31.07: belongs to the payment month, not the visit month.
        _fact(20, 1, 3, 100.0, 10, 105),
        _fact(21, 1, datetime(2026, 7, 15, 12), 60.0, 11, item_type='goods_transaction'),
        _fact(22, 1, datetime(2026, 7, 20, 12), 40.0, 11, item_type='account_replenishment'),
        _fact(23, 1, datetime(2026, 7, 21, 12), 25.0, 12, 105),  # account missing from the catalog
    ])
    await session.commit()


@pytest.mark.asyncio
async def test_monthly_buckets_add_up_to_the_period_buckets(seeded):
    await _seed_july(seeded)
    period = dashboard_service.DateRange(*JULY_AUGUST)
    ids = [1, 2, 3, 4, 6]
    whole = await dashboard_service.fetch_revenue_by_account_type(seeded, period, ids, FROZEN_NOW)
    monthly = await dashboard_service.fetch_revenue_by_account_type(seeded, period, ids, FROZEN_NOW, by_month=True)

    assert set(whole) == {1, 2}
    assert set(monthly) == {(1, JULY), (1, AUGUST_START), (2, AUGUST_START)}
    for company_id, buckets in whole.items():
        for name, amount in buckets.items():
            assert sum(
                month_buckets[name] for (cid, _), month_buckets in monthly.items() if cid == company_id
            ) == pytest.approx(amount)
    assert monthly[(1, JULY)] == pytest.approx({'cash': 12345.0, 'cashless': 100.0, 'other': 25.0})
    # The 100 paid on 03.08 for the July visit lands in August, with the August payments.
    assert monthly[(1, AUGUST_START)] == pytest.approx({'cash': 1100.0, 'cashless': 2650.0, 'other': 350.0})
    assert sum(sum(buckets.values()) for buckets in monthly.values()) == pytest.approx(
        (await dashboard_service.fetch_summary(
            seeded, *JULY_AUGUST, allowed_company_ids=ids, include_appointments_breakdown=False, factual_at=FROZEN_NOW
        ))['revenue']['total']
    )


@pytest.mark.asyncio
async def test_the_default_output_is_still_keyed_by_branch(seeded):
    breakdown = await dashboard_service.fetch_revenue_by_account_type(
        seeded, dashboard_service.DateRange(*AUGUST), [1, 2], FROZEN_NOW
    )
    assert all(isinstance(key, int) for key in breakdown)
    assert breakdown[1] == pytest.approx({'cash': 1000.0, 'cashless': 2650.0, 'other': 350.0})


@pytest.mark.asyncio
async def test_monthly_rows_sum_to_the_branch_table_and_apply_yandex_pay_per_month(seeded):
    await _seed_july(seeded)
    await _store(seeded, 1, 600.5, month=8)
    await _store(seeded, 2, 100.0, month=8)
    breakdown = await payment_methods.fetch_payment_breakdown(seeded, *JULY_AUGUST, [1, 2, 3], FROZEN_NOW)

    july, august = breakdown['monthly']
    assert (july['month'], august['month']) == ('2026-07', '2026-08')
    # Nothing entered for July: unknown, and the cashless stays whole.
    assert july['yandex_pay'] is None and july['cashless'] == july['cashless_yclients'] == 100.0
    assert august['yandex_pay'] == 700.5
    assert august['cashless'] == pytest.approx(august['cashless_yclients'] - 700.5)
    for key in ('revenue', 'cash', 'cashless_yclients', 'other'):
        assert july[key] + august[key] == pytest.approx(sum(branch[key] for branch in breakdown['branches']))
    assert july['revenue'] == pytest.approx(12470.0) and august['revenue'] == pytest.approx(5300.0)
    assert breakdown['yandex_pay_applied'] is True


@pytest.mark.asyncio
async def test_a_partial_edge_month_gets_no_yandex_pay_while_the_others_do(seeded):
    await _seed_july(seeded)
    await _store(seeded, 1, 50.0, month=7)
    await _store(seeded, 1, 600.0, month=8)
    breakdown = await payment_methods.fetch_payment_breakdown(
        seeded, date(2026, 7, 15), AUGUST[1], [1, 2, 3], FROZEN_NOW
    )
    july, august = breakdown['monthly']
    # The table is all-or-nothing, the trend decides month by month.
    assert breakdown['yandex_pay_applied'] is False
    assert all(branch['yandex_pay'] is None for branch in breakdown['branches'])
    assert july['yandex_pay'] is None and july['cashless'] == july['cashless_yclients']
    assert august['yandex_pay'] == 600.0

    month_to_date = await payment_methods.fetch_payment_breakdown(
        seeded, date(2026, 8, 1), date(2026, 9, 10), [1], FROZEN_NOW
    )
    assert [row['yandex_pay'] for row in month_to_date['monthly']] == [600.0, None]


@pytest.mark.asyncio
async def test_a_stored_value_outside_the_window_stays_out_of_the_month(seeded):
    await _seed_july(seeded)
    await _store(seeded, 3, 700.0, month=8)  # Gamma left on 20.08
    breakdown = await payment_methods.fetch_payment_breakdown(seeded, *JULY_AUGUST, [1, 2, 3], FROZEN_NOW)
    assert [row['yandex_pay'] for row in breakdown['monthly']] == [None, None]


@pytest.mark.asyncio
async def test_the_monthly_share_chart_comes_last_spans_the_grid_and_leaves_gaps(client, seeded):
    await _seed_july(seeded)
    await _store(seeded, 1, 600.0, month=8)
    payload = (await _report(client, start=JULY_AUGUST[0], end=JULY_AUGUST[1])).json()['data']
    assert [chart['id'] for chart in payload['charts']] == [
        'payment_shares_by_branch', 'payment_shares_network', 'payment_shares_by_month'
    ]
    chart = payload['charts'][-1]
    assert chart['title'] == 'Доли форм оплаты по месяцам'
    assert chart['type'] == 'line' and chart['wide'] is True and 'stacked' not in chart
    assert chart['labels'] == ['июл 2026', 'авг 2026']
    assert [dataset['label'] for dataset in chart['datasets']] == [
        'Наличные', 'Безналичные', 'Яндекс Пэй', 'Прочие кассы'
    ]
    assert all(dataset['format'] == 'percent' and dataset['fill'] is False for dataset in chart['datasets'])
    cash, cashless, yandex, other = (dataset['data'] for dataset in chart['datasets'])
    assert cash[0] == pytest.approx(12345.0 / 12470.0 * 100, abs=0.01)
    assert yandex == [None, pytest.approx(600.0 / 5300.0 * 100, abs=0.01)]
    assert other[0] == pytest.approx(25.0 / 12470.0 * 100, abs=0.01)
    # July's shares are the whole of its revenue; August's add up with Yandex Pay carved out.
    assert cash[0] + cashless[0] + other[0] == pytest.approx(100.0, abs=0.02)
    assert cash[1] + cashless[1] + yandex[1] + other[1] == pytest.approx(100.0, abs=0.03)
    # The other charts are what they were.
    assert payload['charts'][0]['type'] == 'bar' and 'wide' not in payload['charts'][0]
    assert 'wide' not in payload['charts'][1]


@pytest.mark.asyncio
async def test_the_monthly_share_chart_needs_two_months_and_some_revenue(client, seeded):
    await _seed_july(seeded)
    single = (await _report(client)).json()['data']
    assert 'payment_shares_by_month' not in [chart['id'] for chart in single['charts']]
    # Two months, no revenue in either: the empty frames are not drawn.
    quiet = (await _report(
        client, start=date(2026, 9, 1), end=date(2026, 10, 31), company_id=1
    )).json()['data']
    assert quiet['charts'] == []
    gone = (await _report(client, start=date(2026, 9, 1), end=date(2026, 10, 31), company_id=6)).json()['data']
    assert gone['charts'] == []


@pytest.mark.asyncio
async def test_the_warning_for_a_cut_period_says_the_monthly_chart_still_carves_whole_months(client, seeded):
    await _seed_july(seeded)
    await _store(seeded, 1, 600.0, month=8)
    cut = (await _report(client, start=date(2026, 7, 15), end=AUGUST[1], company_id=1)).json()['data']
    assert [chart['id'] for chart in cut['charts']][-1] == 'payment_shares_by_month'
    text = next(note['text'] for note in cut['notes'] if note['title'] == 'Яндекс Пэй не учтён')
    assert 'графике по месяцам' in text
    # Whole months, but nothing entered for any of them: the line carves nothing either.
    await seeded.execute(delete(ManualPaymentAmount))
    await seeded.commit()
    empty = (await _report(client, start=date(2026, 7, 15), end=AUGUST[1], company_id=1)).json()['data']
    assert 'графике' not in next(note['text'] for note in empty['notes'] if note['title'] == 'Яндекс Пэй не учтён')
    # Nothing is carved anywhere, so there is nothing to explain.
    none = (await _report(client, end=date(2026, 8, 15), company_id=1)).json()['data']
    assert 'Яндекс Пэй не учтён' not in [note['title'] for note in none['notes']]


@pytest.mark.asyncio
async def test_a_month_where_yandex_pay_exceeds_cashless_is_named_even_if_the_period_nets_out(client, seeded):
    await _seed_july(seeded)
    await _store(seeded, 1, 500.0, month=7)  # July's cashless is 100
    await _store(seeded, 1, 100.0, month=8)
    payload = (await _report(client, start=JULY_AUGUST[0], end=JULY_AUGUST[1], company_id=1)).json()['data']
    (alpha,) = payload['raw']['branches']
    assert alpha['cashless'] > 0
    assert not [note for note in payload['notes'] if note['title'] == 'Яндекс Пэй больше безналичных YClients']
    note = next(note for note in payload['notes'] if note['title'] == 'Яндекс Пэй больше безналичных YClients по месяцам')
    assert note['kind'] == 'warning' and 'июль 2026' in note['text'] and 'август' not in note['text']
    trend = payload['charts'][-1]
    cashless = next(dataset['data'] for dataset in trend['datasets'] if dataset['label'] == 'Безналичные')
    assert cashless[0] < 0


def test_short_month_labels():
    assert payment_methods.short_month_label('2026-01') == 'янв 2026'
    assert payment_methods.short_month_label('2025-12') == 'дек 2025'


@pytest.mark.asyncio
async def test_one_batch_can_insert_update_and_clear_rows_together(client, seeded):
    await _store(seeded, 2, 300.0, user_id=2)
    await _store(seeded, 3, 400.0, user_id=2)
    response = await _save(
        client,
        [
            {'company_id': 1, 'value': 100.5, 'previous_value': None},
            {'company_id': 2, 'value': None, 'previous_value': 300, 'previous_data_through': '2026-08-31'},
            {'company_id': 3, 'value': 450, 'previous_value': 400, 'previous_data_through': '2026-08-31'},
        ],
    )
    assert response.status_code == 200, response.text
    stored = {
        row.company_id: float(row.amount) for row in (await seeded.execute(select(ManualPaymentAmount))).scalars()
    }
    assert stored == {1: 100.5, 3: 450.0}
    assert (await _audit_events(seeded))[-1].metadata_json['rows'] == [
        {'company_id': 1, 'old': None, 'new': 100.5, 'old_data_through': None, 'new_data_through': '2026-08-31'},
        {'company_id': 2, 'old': 300.0, 'new': None, 'old_data_through': '2026-08-31', 'new_data_through': None},
        {'company_id': 3, 'old': 400.0, 'new': 450.0,
         'old_data_through': '2026-08-31', 'new_data_through': '2026-08-31'},
    ]



# --- "data through": how far a typed total reaches -------------------------------------


def _at(monkeypatch, moment):
    monkeypatch.setattr(dashboard_service, 'factual_now', lambda: moment)


@pytest.mark.parametrize(
    'moment, month, expected',
    [
        (datetime(2026, 9, 15, 12), '2026-09', ('2026-09-14', '2026-09-15')),  # an ordinary day: yesterday
        (datetime(2026, 9, 15, 12), '2026-08', ('2026-08-31', '2026-08-31')),  # a finished month: its last day
        (datetime(2026, 9, 1, 12), '2026-09', ('2026-09-01', '2026-09-01')),  # the 1st has no yesterday in the month
        (datetime(2026, 9, 30, 12), '2026-09', ('2026-09-29', '2026-09-30')),  # the last day is not finished yet
        (datetime(2026, 9, 15, 22), '2026-09', ('2026-09-15', '2026-09-16')),  # 01:00 MSK of the 16th is the 16th
        (datetime(2026, 9, 15, 12), '2026-10', (None, None)),  # not started: nothing to enter
    ],
)
@pytest.mark.asyncio
async def test_editor_offers_a_default_and_a_latest_data_through(client, monkeypatch, moment, month, expected):
    _at(monkeypatch, moment)
    data = await _editor(client, month=month)
    assert (data['default_data_through'], data['max_data_through']) == expected
    for row in data['rows']:
        assert (row['default_data_through'], row['max_data_through']) == expected
        assert row['data_through'] is None


@pytest.mark.asyncio
async def test_a_value_without_a_date_gets_the_default_and_a_date_is_kept(client, seeded):
    saved = await _save(
        client,
        [
            {'company_id': 1, 'value': 90, 'previous_value': None},
            {'company_id': 2, 'value': 40, 'previous_value': None, 'data_through': '2026-09-10'},
        ],
        month='2026-09',
    )
    assert saved.status_code == 200, saved.text
    rows = _rows(saved.json()['data'])
    assert rows[1]['data_through'] == '2026-09-14' and rows[2]['data_through'] == '2026-09-10'
    # No amount, no date: the key is not a sign of a value.
    assert rows[4]['value'] is None and rows[4]['data_through'] is None


@pytest.mark.parametrize(
    'month, through, status',
    [
        ('2026-09', '2026-09-16', 400),  # after the branch's today
        ('2026-09', '2026-08-31', 400),  # before the month
        ('2026-08', '2026-09-01', 400),  # after the month
        ('2026-08', '2026-08-00', 422),
        ('2026-08', 'yesterday', 422),
        ('2026-08', '2026-8-5', 422),
        ('2026-08', 20260831, 422),  # a number is not a day, even though lax parsing reads it as a timestamp
        ('2026-08', True, 422),
        ('2026-08', '2026-02-30', 422),
    ],
)
@pytest.mark.asyncio
async def test_a_bad_data_through_is_refused_and_writes_nothing(client, seeded, month, through, status):
    response = await _save(
        client, [{'company_id': 1, 'value': 5, 'previous_value': None, 'data_through': through}], month=month
    )
    assert response.status_code == status, response.text
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 0
    assert await _audit_events(seeded) == []


@pytest.mark.asyncio
async def test_a_date_beside_a_cleared_value_is_ignored(client, seeded):
    await _store(seeded, 1, 500.0, through=date(2026, 8, 20))
    cleared = await _save(
        client,
        [{
            'company_id': 1, 'value': None, 'previous_value': 500, 'previous_data_through': '2026-08-20',
            'data_through': '2030-01-01',
        }],
    )
    assert cleared.status_code == 200, cleared.text
    assert (await seeded.scalar(select(func.count()).select_from(ManualPaymentAmount))) == 0


@pytest.mark.asyncio
async def test_changing_only_the_date_is_an_edit_with_an_audit_trail(client, seeded):
    await _store(seeded, 1, 500.0, user_id=2, through=date(2026, 8, 20))
    response = await _save(
        client,
        [{
            'company_id': 1, 'value': 500, 'previous_value': 500,
            'data_through': '2026-08-25', 'previous_data_through': '2026-08-20',
        }],
    )
    assert response.status_code == 200, response.text
    row = _rows(response.json()['data'])[1]
    assert row['value'] == 500.0 and row['data_through'] == '2026-08-25'
    assert row['updated_by'] == 1 and row['updated_at'] == FROZEN_NOW.isoformat()
    (event,) = await _audit_events(seeded)
    assert event.metadata_json['rows'] == [{
        'company_id': 1, 'old': 500.0, 'new': 500.0,
        'old_data_through': '2026-08-20', 'new_data_through': '2026-08-25',
    }]


@pytest.mark.asyncio
async def test_an_unchanged_pair_is_not_touched(client, seeded):
    await _store(seeded, 1, 500.0, user_id=2, through=date(2026, 8, 20))
    data = await _editor(client)
    items = [
        {
            'company_id': row['company_id'],
            'value': row['value'],
            'previous_value': row['value'],
            'data_through': row['data_through'],
            'previous_data_through': row['data_through'],
        }
        for row in data['rows']
    ]
    assert (await _save(client, items)).status_code == 200
    assert await _audit_events(seeded) == []
    stored = (await seeded.execute(select(ManualPaymentAmount))).scalars().one()
    assert stored.updated_by_user_id == 2 and stored.data_through == date(2026, 8, 20)


@pytest.mark.asyncio
async def test_a_stale_date_conflicts_even_when_the_amount_matches(client, seeded):
    await _store(seeded, 1, 500.0, through=date(2026, 8, 25))
    stale = await _save(
        client,
        [{
            'company_id': 1, 'value': 500, 'previous_value': 500,
            'data_through': '2026-08-31', 'previous_data_through': '2026-08-20',
        }],
    )
    assert stale.status_code == 409
    # The lock is the pair: a forgotten previous date is just as stale.
    forgot = await _save(client, [{'company_id': 1, 'value': 600, 'previous_value': 500}])
    assert forgot.status_code == 409
    assert (await seeded.execute(select(ManualPaymentAmount.data_through))).scalar_one() == date(2026, 8, 25)
    assert await _audit_events(seeded) == []


@pytest.mark.asyncio
async def test_a_date_moved_between_the_check_and_the_write_is_a_conflict(client, seeded, monkeypatch):
    """The guarded UPDATE keys on the date too: same amount, new date, still refused."""
    await _store(seeded, 1, 500.0, through=date(2026, 8, 20))
    real_stored = payment_methods._stored_amounts

    async def outdated_read(db, month_start, company_ids):
        rows = await real_stored(db, month_start, company_ids)
        await db.execute(ManualPaymentAmount.__table__.update().values(data_through=date(2026, 8, 22)))
        return rows

    monkeypatch.setattr(payment_methods, '_stored_amounts', outdated_read)
    response = await _save(
        client,
        [{
            'company_id': 1, 'value': 600, 'previous_value': 500,
            'data_through': '2026-08-31', 'previous_data_through': '2026-08-20',
        }],
    )
    assert response.status_code == 409
    monkeypatch.undo()
    assert await _audit_events(seeded) == []
    stored = (await seeded.execute(select(ManualPaymentAmount))).scalars().one()
    assert float(stored.amount) == 500.0


def test_the_database_keeps_data_through_inside_its_month():
    names = {constraint.name for constraint in ManualPaymentAmount.__table__.constraints}
    assert 'ck_manual_payment_amounts_data_through_in_period' in names
    assert ManualPaymentAmount.__table__.c.data_through.nullable is False


# --- the counting rule: start <= 1st of the month and end >= data through ----------------


@pytest.mark.asyncio
async def test_a_total_through_the_middle_of_a_month_counts_from_the_first_to_that_day_and_later(client, seeded):
    await _store(seeded, 1, 300.0, through=date(2026, 8, 15))
    for end in (date(2026, 8, 15), date(2026, 8, 20), AUGUST[1]):
        payload = (await _report(client, end=end, company_id=1)).json()['data']
        alpha = _by_company(payload)[1]
        assert alpha['yandex_pay'] == 300.0, end
        assert alpha['yandex_pay_data_through'] == {'2026-08': '2026-08-15'}
    # A period that ends before the total's last day cannot carry it.
    payload = (await _report(client, end=date(2026, 8, 14), company_id=1)).json()['data']
    alpha = _by_company(payload)[1]
    assert alpha['yandex_pay'] is None and alpha['yandex_pay_applied'] is False
    assert alpha['yandex_pay_unapplied_reason'] == 'value_beyond_period'
    assert alpha['yandex_pay_unapplied_data_through'] == '2026-08-15'
    assert alpha['yandex_pay_data_through'] == {}


@pytest.mark.asyncio
async def test_a_period_starting_after_the_first_blocks_that_branch_even_without_a_value(client, seeded):
    await _store(seeded, 1, 300.0)
    payload = (await _report(client, start=date(2026, 8, 2))).json()['data']
    branches = _by_company(payload)
    for company_id in (1, 2):
        assert branches[company_id]['yandex_pay'] is None
        assert branches[company_id]['yandex_pay_unapplied_reason'] == 'period_starts_mid_month'
    # Gamma left on 20.08: August is not its month to count, so nothing about it is blocked.
    assert branches[3]['yandex_pay_applied'] is True
    assert payload['raw']['yandex_pay_applied'] is False
    text = next(note['text'] for note in payload['notes'] if note['title'] == 'Яндекс Пэй не учтён')
    assert 'Alpha, Beta: Яндекс Пэй вводится за месяц с 1-го числа' in text and 'период начинается позже' in text
    single = (await _report(client, start=date(2026, 8, 2), company_id=1)).json()['data']
    single_text = next(note['text'] for note in single['notes'] if note['title'] == 'Яндекс Пэй не учтён')
    assert single_text.startswith('Яндекс Пэй вводится за месяц с 1-го числа')  # one branch: no list of names


@pytest.mark.asyncio
async def test_a_blocked_branch_does_not_block_its_neighbours(client, seeded):
    await _store(seeded, 1, 300.0, through=date(2026, 8, 31))
    await _store(seeded, 2, 100.0, through=date(2026, 8, 10))
    payload = (await _report(client, end=date(2026, 8, 20))).json()['data']
    alpha, beta = _by_company(payload)[1], _by_company(payload)[2]
    assert alpha['yandex_pay'] is None and beta['yandex_pay'] == 100.0
    assert beta['cashless'] == pytest.approx(700.0 - 100.0)
    assert alpha['cashless'] == alpha['cashless_yclients']
    warning = next(note['text'] for note in payload['notes'] if note['title'] == 'Яндекс Пэй не учтён')
    assert warning.startswith('Введённая сумма Яндекс Пэй покрывает дни после конца периода: Alpha (данные по 31.08)')
    assert 'Beta' not in warning


@pytest.mark.asyncio
async def test_a_month_without_a_value_does_not_block_the_others(client, seeded):
    await _store(seeded, 1, 100.0, month=8)
    payload = (await _report(client, start=date(2026, 7, 1), end=date(2026, 8, 31), company_id=1)).json()['data']
    alpha = _by_company(payload)[1]
    assert alpha['yandex_pay'] == 100.0 and alpha['yandex_pay_missing_months'] == ['2026-07']
    assert alpha['yandex_pay_applied'] is True


@pytest.mark.asyncio
async def test_the_trend_counts_each_value_by_the_same_rule(seeded):
    await _store(seeded, 1, 50.0, month=7, through=date(2026, 7, 20))
    await _store(seeded, 1, 600.0, month=8, through=date(2026, 8, 31))
    await _seed_july(seeded)
    breakdown = await payment_methods.fetch_payment_breakdown(seeded, *JULY_AUGUST, [1], FROZEN_NOW)
    assert [row['yandex_pay'] for row in breakdown['monthly']] == [50.0, 600.0]
    # Ending on 31.07 the August total is out of the period altogether; July's 20.07 total is inside.
    cut = await payment_methods.fetch_payment_breakdown(seeded, JULY, date(2026, 8, 20), [1], FROZEN_NOW)
    assert [row['yandex_pay'] for row in cut['monthly']] == [50.0, None]
    # Starting after the 1st leaves July out of the line and keeps August.
    late = await payment_methods.fetch_payment_breakdown(seeded, date(2026, 7, 2), AUGUST[1], [1], FROZEN_NOW)
    assert [row['yandex_pay'] for row in late['monthly']] == [None, 600.0]


@pytest.mark.asyncio
async def test_a_partial_month_gets_a_note_naming_the_dates_by_month(client, seeded):
    await _seed_july(seeded)
    await _store(seeded, 1, 50.0, month=7, through=date(2026, 7, 20))
    await _store(seeded, 1, 600.0, month=8, through=date(2026, 8, 20))
    await _store(seeded, 2, 100.0, month=8, through=date(2026, 8, 20))
    await _store(seeded, 3, 10.0, month=8, through=date(2026, 8, 20))  # outside Gamma's window: not counted
    payload = (await _report(client, start=JULY_AUGUST[0], end=JULY_AUGUST[1])).json()['data']
    note = next(note for note in payload['notes'] if note['title'] == 'Яндекс Пэй внесён не за весь месяц')
    assert note['kind'] == 'info'
    assert note['text'].startswith(
        'июль 2026: данные по 20.07 — Alpha. август 2026: данные по 20.08 — Alpha, Beta.'
    )
    assert 'Gamma' not in note['text']
    assert 'Выручка YClients посчитана по 31.08' in note['text']
    assert _by_company(payload)[1]['yandex_pay_data_through'] == {'2026-07': '2026-07-20', '2026-08': '2026-08-20'}


@pytest.mark.asyncio
async def test_the_note_groups_branches_by_date_and_stays_silent_for_whole_months(client, seeded):
    await _store(seeded, 1, 600.0, through=date(2026, 8, 20))
    await _store(seeded, 2, 100.0, through=date(2026, 8, 10))
    payload = (await _report(client)).json()['data']
    note = next(note for note in payload['notes'] if note['title'] == 'Яндекс Пэй внесён не за весь месяц')
    assert 'август 2026: данные по 20.08 — Alpha; по 10.08 — Beta.' in note['text']
    # Both values reach the period's end: no word about revenue running ahead.
    await seeded.execute(delete(ManualPaymentAmount))
    await _store(seeded, 1, 600.0)
    quiet = (await _report(client)).json()['data']
    assert 'Яндекс Пэй внесён не за весь месяц' not in [note['title'] for note in quiet['notes']]
    # A period that ends on the total's own day has no uncovered days to warn about.
    await seeded.execute(delete(ManualPaymentAmount))
    await _store(seeded, 1, 600.0, through=date(2026, 8, 20))
    exact = (await _report(client, end=date(2026, 8, 20))).json()['data']
    exact_note = next(note for note in exact['notes'] if note['title'] == 'Яндекс Пэй внесён не за весь месяц')
    assert 'занижена' not in exact_note['text']


@pytest.mark.asyncio
async def test_the_note_uses_the_year_only_when_it_differs_from_the_period(client, seeded):
    note = payment_methods.format_day
    assert note(date(2026, 8, 5), 2026) == '05.08'
    assert note(date(2025, 12, 31), 2026) == '31.12.2025'
