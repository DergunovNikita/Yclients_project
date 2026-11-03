"""Branch and staff filters sent as lists (`company_ids`, `staff_ids`) on the Overview, Plan/fact and reports."""
from datetime import date, datetime, timedelta

import pytest

import dashboard_service
from api import app
from auth_scope import AccessContext
from models import Appointment, Company, ManualFactMetric, Staff, StaffSchedule
from test_staff_levels import _client, _seed_overview, _visit

START, END = '2025-03-01', '2025-03-31'
PERIOD = {'start_date': START, 'end_date': END}
BUNDLE, PLAN_FACT, REPORT, STAFF = (
    '/dashboard/bundle', '/dashboard/widget/plan_fact', '/dashboard/reports/data', '/dashboard/staff',
)


@pytest.fixture(autouse=True)
def no_upstream(monkeypatch):
    """A group is counted from synced records; YClients' record stats are never reachable here."""

    async def fail(*args, **kwargs):
        raise dashboard_service.yclients_analytics.YClientsAnalyticsError('offline')

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fail)


async def _seed(async_session):
    """Alpha (1) and Beta (2) of `_seed_overview`, plus Gamma (3) with a barber and two completed March visits."""
    await _seed_overview(async_session)
    async_session.add(Company(id=3, title='Gamma', timezone='UTC', group_id=1))
    await async_session.flush()
    async_session.add(Staff(id=7, name='Gamma barber', position='Барбер', position_id=100, company_id=3))
    async_session.add(Staff(id=8, name='Gamma admin', position='Администратор', company_id=3))
    await async_session.flush()
    async_session.add_all([
        _visit(70, 70, date(2025, 3, 12), 7, company_id=3),
        _visit(71, 71, date(2025, 3, 13), 7, company_id=3),
    ])
    for company_id, staff_id in ((1, 3), (3, 8)):
        async_session.add(ManualFactMetric(
            period_start=date(2025, 3, 1), period_end=date(2025, 3, 31), company_id=company_id, staff_id=staff_id,
            metric_code='reviews_qty', value=5.0, updated_at=datetime(2025, 3, 20, 10),
        ))
    await async_session.commit()
    async_session.info.clear()


def _manager(**kwargs):
    return AccessContext.from_user(user_id=10, role='manager', portal_account_id=1, company_ids=[1, 2, 3], **kwargs)


def _barber(*rows):
    """A personal role owning one `staff` row per `(company_id, staff_id)` pair."""
    return AccessContext.from_user(
        user_id=11, role='barber', portal_account_id=1, company_ids=[1, 2, 3], staff_id=rows[0][1], staff_keys=rows,
    )


async def _get(async_session, path, ctx=None, **params):
    client = await _client(async_session, ctx)
    try:
        async with client:
            async_session.info.clear()
            return await client.get(path, params=params)
    finally:
        app.dependency_overrides.clear()


async def _data(async_session, path, ctx=None, **params):
    response = await _get(async_session, path, ctx, **params)
    assert response.status_code == 200, response.text
    return response.json()['data']


def _visits(bundle):
    return bundle['summary']['revenue']['appointments']


def _staff_ids(plan_fact):
    return [group['staff_id'] for group in plan_fact['groups']]


# ---------------------------------------------------------------------------
# Parsing, exclusion and scope
# ---------------------------------------------------------------------------

ENDPOINTS = (
    (BUNDLE, PERIOD),
    (PLAN_FACT, PERIOD),
    (REPORT, {**PERIOD, 'report_id': 'bookings_dynamics'}),
    (STAFF, {}),
)


# The staff list takes the branch filter only.
LIST_FILTERS = [(path, base, 'company_ids') for path, base in ENDPOINTS] + [
    (path, base, 'staff_ids') for path, base in ENDPOINTS[:3]
]


@pytest.mark.asyncio
@pytest.mark.parametrize('path, base, name', LIST_FILTERS)
async def test_a_malformed_list_is_a_400_on_every_filtered_endpoint(async_session, path, base, name):
    await _seed(async_session)
    for bad in ('abc', '1,,2', '-5', '0', '1.5', '99999999999', '1' * 5000, ','.join(str(i) for i in range(1, 202))):
        response = await _get(async_session, path, **base, **{name: bad})
        assert response.status_code == 400, (name, bad[:20])
        assert name in response.json()['detail']
    # Empty means absent, and repeated ids collapse.
    assert (await _get(async_session, path, **base, **{name: ''})).status_code == 200
    assert (await _get(async_session, path, **base, **{name: '1,1'})).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('path, base', ENDPOINTS[:3])
async def test_the_scalar_and_the_list_spelling_cannot_be_combined(async_session, path, base):
    await _seed(async_session)
    for params in (
        {'company_id': 1, 'company_ids': '1,2'},
        {'company_id': 1, 'company_ids': '1'},
        {'staff_id': 2, 'staff_ids': '2,4'},
    ):
        response = await _get(async_session, path, **base, **params)
        assert response.status_code == 400, params
        assert 'cannot be combined' in response.json()['detail']


@pytest.mark.asyncio
@pytest.mark.parametrize('path, base', ENDPOINTS[:3])
async def test_a_branch_list_must_lie_inside_the_principals_branches(async_session, path, base):
    await _seed(async_session)
    narrow = AccessContext.from_user(user_id=10, role='manager', portal_account_id=1, company_ids=[1, 2])

    refused = await _get(async_session, path, narrow, **base, company_ids='1,3')
    allowed = await _get(async_session, path, narrow, **base, company_ids='1,2')
    unknown = await _get(async_session, path, None, **base, company_ids='1,99')

    assert refused.status_code == 403 and refused.json()['detail'] == 'Branch not allowed'
    assert allowed.status_code == 200
    # The API key sees every branch, but an id that does not exist is still an error.
    assert unknown.status_code == 400 and unknown.json()['detail'] == 'unknown company_id'


@pytest.mark.asyncio
async def test_staff_lists_must_name_people_of_the_selected_branches(async_session):
    await _seed(async_session)

    unknown = await _get(async_session, BUNDLE, **PERIOD, staff_ids='2,999')
    elsewhere = await _get(async_session, BUNDLE, **PERIOD, company_ids='1,2', staff_ids='2,7')
    inside = await _get(async_session, BUNDLE, **PERIOD, company_ids='1,3', staff_ids='2,7')

    assert unknown.status_code == 400 and unknown.json()['detail'] == 'unknown staff_id'
    assert elsewhere.status_code == 400 and elsewhere.json()['detail'] == 'unknown staff_id'
    assert inside.status_code == 200


# ---------------------------------------------------------------------------
# One id is the scalar
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize('path, base', ENDPOINTS[:3])
async def test_a_one_element_list_answers_exactly_like_the_scalar(async_session, path, base):
    await _seed(async_session)

    for scalar, listed in (
        ({'company_id': 1}, {'company_ids': '1'}),
        ({'staff_id': 2}, {'staff_ids': '2'}),
        ({'company_id': 1, 'staff_id': 5}, {'company_ids': '1', 'staff_ids': '5'}),
    ):
        assert await _data(async_session, path, **base, **listed) == await _data(async_session, path, **base, **scalar)


@pytest.mark.asyncio
async def test_a_chosen_administrator_alone_still_gets_administrator_mode(async_session):
    await _seed(async_session)

    one = await _data(async_session, BUNDLE, **PERIOD, staff_ids='3')
    two = await _data(async_session, BUNDLE, **PERIOD, staff_ids='3,2')

    assert 'administrator_personal' in one['summary']
    # Together with someone else the group is attributed by master, as the level group is.
    assert 'administrator_personal' not in two['summary']


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_branch_list_is_the_network_restricted_to_those_branches(async_session):
    await _seed(async_session)

    chosen = await _data(async_session, BUNDLE, **PERIOD, company_ids='1,3')
    alpha = await _data(async_session, BUNDLE, **PERIOD, company_id=1)
    gamma = await _data(async_session, BUNDLE, **PERIOD, company_id=3)
    everyone = await _data(async_session, BUNDLE, **PERIOD)

    assert _visits(chosen) == _visits(alpha) + _visits(gamma) == 8
    assert _visits(everyone) == 9
    assert chosen['summary']['revenue']['total'] == pytest.approx(
        alpha['summary']['revenue']['total'] + gamma['summary']['revenue']['total']
    )
    assert 'staff_filter' not in chosen['summary']


@pytest.mark.asyncio
async def test_a_staff_list_is_a_group_of_exactly_those_people(async_session):
    await _seed(async_session)

    group = await _data(async_session, BUNDLE, **PERIOD, staff_ids='2,4,7')
    parts = [await _data(async_session, BUNDLE, **PERIOD, staff_id=staff_id) for staff_id in (2, 4, 7)]

    assert _visits(group) == sum(_visits(part) for part in parts) == 5
    assert [_visits(part) for part in parts] == [2, 1, 2]
    assert group['summary']['revenue']['total'] == pytest.approx(
        sum(part['summary']['revenue']['total'] for part in parts)
    )
    assert group['summary']['staff_filter'] == {'staff_ids': [2, 4, 7], 'staff_count': 3}
    assert [row['sold'] for row in group['top_services']] == [3]
    assert sum(day['appointments'] for day in group['revenue_daily']) == 5
    assert all('staff_filter' not in part['summary'] for part in parts)


@pytest.mark.asyncio
async def test_a_chosen_group_wins_over_the_level_and_narrows_inside_the_chosen_branches(async_session):
    await _seed(async_session)

    group = await _data(async_session, BUNDLE, **PERIOD, staff_ids='1,2', staff_position_ids='200')
    inside = await _data(async_session, BUNDLE, **PERIOD, company_ids='1,2', staff_ids='1,2,4')

    assert 'level_filter' not in group['summary']
    assert group['summary']['staff_filter']['staff_count'] == 2
    assert _visits(group) == 4  # staff 1: visits 1 and 8, staff 2: visits 2 and 6
    assert _visits(inside) == 5


# ---------------------------------------------------------------------------
# Personal roles
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_personal_role_with_a_row_in_each_of_two_branches_reads_both_or_the_chosen_one(async_session):
    await _seed(async_session)
    ctx = _barber((1, 2), (2, 4))

    both = await _data(async_session, BUNDLE, ctx, **PERIOD)
    in_beta = await _data(async_session, BUNDLE, ctx, **PERIOD, company_id=2)
    in_alpha = await _data(async_session, BUNDLE, ctx, **PERIOD, company_ids='1')
    own_pair = await _data(async_session, BUNDLE, ctx, **PERIOD, staff_ids='2,4')

    assert both['summary']['staff_filter'] == {'staff_ids': [2, 4], 'staff_count': 2}
    assert own_pair['summary']['visit_metrics'] == both['summary']['visit_metrics']
    assert both['summary']['visit_metrics']['unique_clients'] == 3
    # The second branch clamps to ITS row instead of answering `unknown staff_id`.
    assert in_beta['summary']['visit_metrics']['unique_clients'] == 1
    assert in_alpha['summary']['visit_metrics']['unique_clients'] == 2
    assert 'staff_filter' not in in_beta['summary'] and 'staff_filter' not in in_alpha['summary']


@pytest.mark.asyncio
async def test_a_personal_role_cannot_ask_for_anyone_elses_row(async_session):
    await _seed(async_session)
    ctx = _barber((1, 2), (2, 4))

    for params in (
        {'staff_id': 1}, {'staff_ids': '2,1'}, {'staff_ids': '2,4,5'},
        # Own row, but outside the branch that was asked for.
        {'company_id': 2, 'staff_id': 2}, {'company_ids': '1,2', 'staff_ids': '2,7'},
    ):
        for path, base in ENDPOINTS[:3]:
            response = await _get(async_session, path, ctx, **base, **params)
            assert response.status_code == 403, (path, params)


@pytest.mark.asyncio
async def test_a_personal_role_without_a_row_in_the_asked_branch_is_refused(async_session):
    await _seed(async_session)
    ctx = _barber((1, 2))

    response = await _get(async_session, BUNDLE, ctx, **PERIOD, company_id=2)

    assert response.status_code == 403 and response.json()['detail'] == 'No staff row assigned'


@pytest.mark.asyncio
async def test_the_staff_list_of_a_personal_role_is_its_own_rows_in_the_chosen_branches(async_session):
    await _seed(async_session)
    ctx = _barber((1, 2), (2, 4))

    async def listed(**params):
        return [row['id'] for row in await _data(async_session, STAFF, ctx, **params)]

    assert await listed() == [2, 4]
    assert await listed(company_ids='2') == [4]
    assert await listed(company_id=1) == [2]
    # Everyone for a branch role, restricted to the chosen branches.
    manager = [
        row['id'] for row in await _data(async_session, STAFF, _manager(), company_ids='2,3')
    ]
    assert manager == [4, 8, 7]


@pytest.mark.asyncio
async def test_a_personal_role_is_told_where_its_latest_past_shift_was_and_other_roles_are_not(async_session):
    await _seed(async_session)
    today = dashboard_service.factual_branch_date()
    slot = {'slot_from': datetime(2000, 1, 1, 10).time(), 'slot_to': datetime(2000, 1, 1, 20).time()}
    async_session.add_all([
        StaffSchedule(staff_id=2, company_id=1, date=today - timedelta(days=10), **slot),
        StaffSchedule(staff_id=2, company_id=1, date=today + timedelta(days=1), **slot),
        StaffSchedule(staff_id=4, company_id=2, date=today - timedelta(days=3), **slot),
        StaffSchedule(staff_id=4, company_id=2, date=today - timedelta(days=5), **slot),
        StaffSchedule(staff_id=8, company_id=3, date=today - timedelta(days=1), **slot),
    ])
    await async_session.commit()

    own = {row['id']: row for row in await _data(async_session, STAFF, _barber((1, 2), (2, 4)))}
    assert own[2]['last_shift_date'] == (today - timedelta(days=10)).isoformat()
    assert own[4]['last_shift_date'] == (today - timedelta(days=3)).isoformat()
    no_shift = await _data(async_session, STAFF, _barber((3, 7)))
    assert no_shift == [{**no_shift[0], 'last_shift_date': None}]
    assert all('last_shift_date' not in row for row in await _data(async_session, STAFF, _manager()))


# ---------------------------------------------------------------------------
# Plan/fact
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_plan_fact_for_a_branch_list_has_network_and_branch_rows_of_the_chosen_ones_only(async_session):
    await _seed(async_session)

    chosen = await _data(async_session, PLAN_FACT, **PERIOD, company_ids='1,3')

    branch_rows = [group['company_id'] for group in chosen['groups'] if group['scope'] == 'branch']
    assert branch_rows == [1, 3]
    assert [group['scope'] for group in chosen['groups']][0] == 'network'
    assert chosen['view_scope'] == 'branch'
    assert {group['company_id'] for group in chosen['groups'] if group['scope'] == 'staff'} <= {1, 3}


@pytest.mark.asyncio
async def test_plan_fact_for_a_staff_list_shows_those_people_across_branches(async_session):
    await _seed(async_session)

    chosen = await _data(async_session, PLAN_FACT, **PERIOD, staff_ids='2,4,7')

    assert chosen['view_scope'] == 'staff'
    assert sorted(_staff_ids(chosen)) == [2, 4, 7]
    assert {group['scope'] for group in chosen['groups']} == {'staff'}
    assert chosen['plan_rules'] is None
    assert 'parent_group' not in chosen and 'branch' not in chosen and chosen['diagnostics'] == []
    assert chosen['staff_filter'] == {'staff_ids': [2, 4, 7], 'staff_count': 3}
    assert 'level_filter' not in chosen
    cells = {group['staff_id']: {cell['code']: cell['fact'] for cell in group['metrics']} for group in chosen['groups']}
    single = await _data(async_session, PLAN_FACT, **PERIOD, staff_id=2)
    assert cells[2] == {cell['code']: cell['fact'] for cell in single['groups'][0]['metrics']}


@pytest.mark.asyncio
async def test_plan_fact_for_a_staff_list_inside_one_branch_keeps_the_branch_and_its_rules(async_session):
    await _seed(async_session)

    chosen = await _data(async_session, PLAN_FACT, **PERIOD, company_id=1, staff_ids='2,5,3')

    assert chosen['view_scope'] == 'staff'
    assert sorted(_staff_ids(chosen)) == [2, 3, 5]
    assert chosen['branch']['id'] == 1
    assert 'plan_rules' in chosen and 'parent_group' not in chosen
    # An administrator in the group keeps the administrator row and metrics: the row's category decides.
    categories = {group['staff_id']: group['category'] for group in chosen['groups']}
    assert categories[3] == 'administrator' and categories[2] == 'barber'


@pytest.mark.asyncio
async def test_a_chosen_group_in_the_plan_fact_wins_over_the_level(async_session):
    await _seed(async_session)

    chosen = await _data(async_session, PLAN_FACT, **PERIOD, staff_ids='1,2', staff_position_ids='999')

    assert sorted(_staff_ids(chosen)) == [1, 2]
    assert 'level_filter' not in chosen


@pytest.mark.asyncio
async def test_a_personal_role_in_two_branches_gets_a_group_view_of_its_own_rows(async_session):
    await _seed(async_session)

    chosen = await _data(async_session, PLAN_FACT, _barber((1, 2), (2, 4)), **PERIOD)

    assert sorted(_staff_ids(chosen)) == [2, 4]
    assert 'parent_group' not in chosen and chosen['staff_filter']['staff_count'] == 2


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def _report(**params):
    return {**PERIOD, **params}


@pytest.mark.asyncio
async def test_staff_efficiency_lists_exactly_the_chosen_people(async_session):
    await _seed(async_session)

    rows = (await _data(async_session, REPORT, **_report(report_id='staff_efficiency', staff_ids='2,4')))['tables'][0]
    everyone = (await _data(async_session, REPORT, **_report(report_id='staff_efficiency')))['tables'][0]

    assert {row['staff_id'] for row in rows['rows']} == {2, 4}
    assert {row['staff_id'] for row in everyone['rows']} > {2, 4, 1}
    assert [row for row in rows['rows'] if row['staff_id'] == 2] == [
        row for row in everyone['rows'] if row['staff_id'] == 2
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize('report_id', [
    'bookings_dynamics', 'avg_check_by_service', 'service_staff_profit', 'peak_load', 'booking_channels',
    'new_vs_returning_cross', 'top_clients_pareto', 'retention_3_6_12', 'revenue_at_risk', 'goods_dynamics',
    'nps_dashboard', 'financial_overview', 'year_over_year', 'service_combos',
])
async def test_every_staff_filtered_report_accepts_a_group_and_a_one_element_list_equals_the_scalar(
    async_session, report_id,
):
    await _seed(async_session)
    params = _report(report_id=report_id)

    group = await _get(async_session, REPORT, **params, staff_ids='2,4')
    one = await _data(async_session, REPORT, **params, staff_ids='2')
    scalar = await _data(async_session, REPORT, **params, staff_id=2)

    assert group.status_code == 200, group.text
    assert one == scalar
    assert group.json()['data']['calculation_scope'] == {
        'kind': 'staff_group', 'mode': 'group', 'staff_ids': [2, 4], 'staff_count': 2, 'company_id': None,
    }


@pytest.mark.asyncio
async def test_group_reports_add_up_over_the_people_of_the_group(async_session):
    await _seed(async_session)

    def cards(data):
        return {card['label']: card['value'] for card in data['cards']}

    async def card_values(report_id, **extra):
        return cards(await _data(async_session, REPORT, **_report(report_id=report_id), **extra))

    for report_id, label in (
        ('bookings_dynamics', 'Записей'), ('financial_overview', 'Завершённые визиты'),
    ):
        group = await card_values(report_id, staff_ids='2,4,7')
        if label not in group:
            label = next(iter(group))
        parts = [(await card_values(report_id, staff_id=staff_id))[label] for staff_id in (2, 4, 7)]
        assert group[label] == sum(parts), report_id


@pytest.mark.asyncio
async def test_the_group_of_a_staff_filtered_report_survives_a_comparison(async_session):
    await _seed(async_session)

    data = await _data(
        async_session, REPORT, **_report(report_id='bookings_dynamics', staff_ids='2,4', compare_previous='true'),
    )

    assert data['comparison']['staff_id'] == [2, 4]


@pytest.mark.asyncio
async def test_the_notes_of_a_filtered_report_speak_of_one_person_for_one_and_of_a_group_for_several(async_session):
    await _seed(async_session)

    def note_texts(data):
        return ' '.join(note['text'] for note in data['notes'] if note['title'] == 'Фильтр по сотруднику')

    one = await _data(async_session, REPORT, **_report(report_id='goods_dynamics', staff_id=2))
    group = await _data(async_session, REPORT, **_report(report_id='goods_dynamics', staff_ids='2,4'))

    assert 'этого сотрудника как продавца' in note_texts(one)
    assert 'выбранных сотрудников как продавцов' in note_texts(group)


@pytest.mark.asyncio
async def test_a_report_without_a_staff_filter_ignores_the_group(async_session):
    await _seed(async_session)
    params = _report(report_id='payment_methods')

    assert await _data(async_session, REPORT, **params, staff_ids='2,4') == await _data(async_session, REPORT, **params)


@pytest.mark.asyncio
async def test_the_ratings_of_a_group_keep_only_the_chosen_people_and_drop_branch_totals(async_session):
    await _seed(async_session)
    params = _report(report_id='staff_leaderboard')

    chosen = await _data(async_session, REPORT, **params, staff_ids='2,4,3')
    everyone = await _data(async_session, REPORT, **params)

    def board_people(data, table_id):
        return {row['staff_id'] for table in data['tables'] if table['id'] == table_id for row in table['rows']}

    for board in ('revenue_barber', 'avg_check_barber'):
        assert board_people(chosen, board) == {2, 4}, board
        assert board_people(everyone, board) > {2, 4}, board
    assert board_people(chosen, 'reviews_admin') == {3}
    assert board_people(everyone, 'reviews_admin') == {3, 8}
    chosen_tables = {table['id'] for table in chosen['tables']}
    assert 'avg_check_plan_branch' not in chosen_tables
    assert 'avg_check_plan_branch' in {table['id'] for table in everyone['tables']}
    assert any(note['title'] == 'Рейтинги выбранных сотрудников' for note in chosen['notes'])
    assert chosen['calculation_scope']['kind'] == 'staff_group'


@pytest.mark.asyncio
async def test_a_branch_list_narrows_a_report(async_session):
    await _seed(async_session)
    params = _report(report_id='staff_efficiency')

    chosen = await _data(async_session, REPORT, **params, company_ids='2,3')

    assert {row['staff_id'] for row in chosen['tables'][0]['rows']} == {4, 7}


@pytest.mark.asyncio
async def test_a_personal_role_reads_a_report_over_its_own_rows_only(async_session):
    await _seed(async_session)
    ctx = _barber((1, 2), (2, 4))
    params = _report(report_id='bookings_dynamics')

    def people(data):
        return {row['staff_id'] for table in data['tables'] for row in table['rows'] if 'staff_id' in row}

    default = await _data(async_session, REPORT, ctx, **params)
    in_beta = await _data(async_session, REPORT, ctx, **params, company_id=2)

    assert people(default) == {2, 4}
    assert people(in_beta) == {4}


# ---------------------------------------------------------------------------
# Builders that compare with a literal id, exercised under a group
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_retention_and_churn_follow_the_first_and_last_master_of_the_group(async_session):
    await _seed(async_session)
    # Client 90: first visit with barber 2, a return a month later with barber 1, then silence long enough to churn.
    async_session.add_all([
        Appointment(
            id=90, company_id=1, client_id=90, staff_id=2, date=date(2024, 6, 3),
            datetime=datetime(2024, 6, 3, 12), attendance=1, create_date=datetime(2024, 6, 1),
        ),
        Appointment(
            id=91, company_id=1, client_id=90, staff_id=1, date=date(2024, 7, 5),
            datetime=datetime(2024, 7, 5, 12), attendance=1, create_date=datetime(2024, 6, 1),
        ),
    ])
    await async_session.commit()
    wide = {'start_date': '2024-06-01', 'end_date': '2024-12-31'}

    async def card_values(report_id, **extra):
        data = await _data(async_session, REPORT, report_id=report_id, **wide, **extra)
        return {card['label']: card['value'] for card in data['cards']}

    retention = await card_values('retention_3_6_12', staff_ids='2,5')
    nobody = await card_values('retention_3_6_12', staff_ids='4,7')
    churn = await card_values('revenue_at_risk', staff_ids='1,4')
    churn_other = await card_values('revenue_at_risk', staff_ids='2,4')

    # The cohort belongs to the barber of the first visit (2), the loss to the barber of the last one (1).
    assert retention != nobody
    assert churn != churn_other


@pytest.mark.asyncio
@pytest.mark.parametrize('path, base', ENDPOINTS[:3])
async def test_a_person_and_a_branch_list_can_be_combined(async_session, path, base):
    await _seed(async_session)

    inside = await _get(async_session, path, **base, staff_id=2, company_ids='1,3')
    outside = await _get(async_session, path, **base, staff_id=2, company_ids='2,3')

    assert inside.status_code == 200
    assert outside.status_code == 400 and outside.json()['detail'] == 'unknown staff_id'
