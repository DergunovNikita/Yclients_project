"""Barber levels: the position id the sync keeps, its monthly history, and the Plan/fact and ratings filters on it."""
import json
from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

import api
import dashboard_routes
import dashboard_service
import sync_pipeline
from api import app
from auth_scope import AccessContext
from dashboard_service import _staff_leaderboards_payload, fetch_plan_fact, level_key
from helpers import load_migration, sqlite_session_with_system
from models import (
    Appointment, Company, FinancialTransaction, Group, ManualFactMetric, PlanMetric, Staff, StaffPositionMonth,
    Transaction,
)
from sync_pipeline import sync_staff

FACTUAL_AT = datetime(2026, 1, 1, 12, 0)
MARCH = (date(2025, 3, 1), date(2025, 3, 31))
APRIL = (date(2025, 4, 1), date(2025, 4, 30))


# ---------------------------------------------------------------------------
# level_key
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('title', ['ТОП-барбер', 'ТОП Барбер', 'топ барбер', ' Топ_Барбер '])
def test_level_key_ignores_case_spacing_and_punctuation(title):
    assert level_key(title) == 'топбарбер'


def test_level_key_folds_yo_and_keeps_digits_and_rejects_empty():
    assert level_key('Ёлка 2') == 'елка2'
    assert level_key(None) is None
    assert level_key(' - ') is None


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

class StaffAPI:
    def __init__(self, staff):
        self._staff = staff

    def get_staff(self, company_id):
        return self._staff


def _sync_session():
    return sqlite_session_with_system([
        Group.__table__, Company.__table__, Staff.__table__, StaffPositionMonth.__table__,
    ])


def _seed_company(db):
    db.add(Group(id=1, title='G'))
    db.add(Company(id=1, title='Salon', timezone='UTC', group_id=1))
    db.commit()


def test_sync_stores_the_position_id_and_snapshots_active_staff_for_the_branch_month(monkeypatch):
    monkeypatch.setattr(sync_pipeline, '_branch_today', lambda db, company_id: date(2025, 3, 17))
    with _sync_session() as db:
        _seed_company(db)
        payload = [
            {'id': 1, 'name': 'Top', 'position': {'id': 200, 'title': 'ТОП-барбер'}},
            {'id': 2, 'name': 'Legacy', 'position': 'Барбер'},
            {'id': 3, 'name': 'Odd id', 'position': {'id': 'abc', 'title': 'Барбер'}},
            {'id': 4, 'name': 'Fired', 'fired': 1, 'position': {'id': 100, 'title': 'Барбер'}},
        ]

        assert sync_staff(StaffAPI(payload), db, '1', db_company_id=1) is True

        staff = {row.external_id: row for row in db.query(Staff)}
        assert [(staff[i].position, staff[i].position_id) for i in (1, 2, 3, 4)] == [
            ('ТОП-барбер', 200), ('Барбер', None), ('Барбер', None), ('Барбер', 100),
        ]
        snapshots = {row.staff_id: row for row in db.query(StaffPositionMonth)}
        # Fired staff are not snapshotted: YClients leaves a stale position on them.
        assert sorted(snapshots) == sorted([staff[1].id, staff[2].id, staff[3].id])
        top = snapshots[staff[1].id]
        assert (top.month, top.company_id, top.position_id, top.position_title) == (
            date(2025, 3, 1), 1, 200, 'ТОП-барбер',
        )


@pytest.mark.parametrize('value, expected', [
    (200, 200), ('200', 200), (2**31 - 1, 2**31 - 1),
    (2**31, None), (0, None), ('abc', None), (None, None), (float('inf'), None),
])
def test_position_external_id_rejects_what_the_integer_column_cannot_hold(value, expected):
    assert sync_pipeline._position_external_id(value) == expected


def test_one_out_of_range_position_id_does_not_fail_the_staff_step(monkeypatch):
    monkeypatch.setattr(sync_pipeline, '_branch_today', lambda db, company_id: date(2025, 3, 17))
    with _sync_session() as db:
        _seed_company(db)
        payload = [
            {'id': 1, 'name': 'Huge', 'position': {'id': 10**12, 'title': 'Барбер'}},
            {'id': 2, 'name': 'Fine', 'position': {'id': 200, 'title': 'ТОП-барбер'}},
        ]

        assert sync_staff(StaffAPI(payload), db, '1', db_company_id=1) is True

        staff = {row.external_id: row for row in db.query(Staff)}
        assert (staff[1].position, staff[1].position_id) == ('Барбер', None)
        assert staff[2].position_id == 200
        snapshots = {row.staff_id: row.position_id for row in db.query(StaffPositionMonth)}
        assert snapshots == {staff[1].id: None, staff[2].id: 200}


def test_a_later_sync_in_the_same_month_overwrites_the_row_and_a_new_month_adds_one(monkeypatch):
    today = {'value': date(2025, 3, 5)}
    monkeypatch.setattr(sync_pipeline, '_branch_today', lambda db, company_id: today['value'])
    with _sync_session() as db:
        _seed_company(db)
        base = [{'id': 1, 'name': 'M', 'position': {'id': 100, 'title': 'Барбер'}}]
        sync_staff(StaffAPI(base), db, '1', db_company_id=1)

        today['value'] = date(2025, 3, 28)
        promoted = [{'id': 1, 'name': 'M', 'position': {'id': 200, 'title': 'ТОП-барбер'}}]
        sync_staff(StaffAPI(promoted), db, '1', db_company_id=1)
        rows = db.query(StaffPositionMonth).all()
        assert [(row.month, row.position_id, row.position_title) for row in rows] == [
            (date(2025, 3, 1), 200, 'ТОП-барбер'),
        ]

        today['value'] = date(2025, 4, 2)
        sync_staff(StaffAPI(promoted), db, '1', db_company_id=1)
        rows = db.query(StaffPositionMonth).order_by(StaffPositionMonth.month)
        months = [(row.month, row.position_id) for row in rows]
        assert months == [(date(2025, 3, 1), 200), (date(2025, 4, 1), 200)]


# ---------------------------------------------------------------------------
# Resolver and /dashboard/staff
# ---------------------------------------------------------------------------

def _visit(id, client_id, day, staff_id, company_id=1):
    return Appointment(
        id=id, company_id=company_id, client_id=client_id, staff_id=staff_id, date=day,
        datetime=datetime.combine(day, datetime.min.time()).replace(hour=12),
        attendance=1, create_date=datetime(2024, 1, 1, 9),
    )


async def _seed_branches(async_session):
    async_session.add_all([
        Group(id=1, title='Group'),
        Company(id=1, title='Alpha', timezone='UTC', group_id=1),
        Company(id=2, title='Beta', timezone='UTC', group_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Staff(id=1, name='Base barber', position='Барбер', position_id=100, company_id=1),
        Staff(id=2, name='Top barber', position='ТОП-барбер', position_id=200, company_id=1),
        # An administrator never matches, even when their position id is in the requested set.
        Staff(id=3, name='Admin', position='Администратор', position_id=300, company_id=1),
        Staff(id=4, name='Top Beta', position='ТОП Барбер', position_id=201, company_id=2),
    ])
    await async_session.flush()
    async_session.add_all([
        _visit(1, 1, date(2025, 3, 3), 1),
        _visit(2, 2, date(2025, 3, 4), 2),
        _visit(3, 3, date(2025, 3, 5), 4, company_id=2),
        _visit(4, 4, date(2025, 4, 3), 1),
        _visit(5, 5, date(2025, 4, 4), 2),
    ])
    await async_session.commit()


async def _seed_march_revenue(async_session):
    """One paid March service per finished March visit: the base barber, the top barber, the Beta top barber."""
    paid = [(1, 1, 1, 1000.0), (2, 1, 2, 2000.0), (3, 2, 4, 3000.0)]  # visit id, company, staff, amount
    async_session.add_all([
        Transaction(
            id=visit_id, appointment_id=visit_id, service_id=visit_id, service_title='Стрижка',
            cost=amount, first_cost=amount, amount=1, company_id=company_id,
        )
        for visit_id, company_id, _staff_id, amount in paid
    ])
    async_session.add_all([
        FinancialTransaction(
            id=visit_id, date=datetime(2025, 3, 10 + visit_id, 12, 0), amount=amount, record_id=visit_id,
            visit_id=visit_id, sold_item_id=visit_id, sold_item_type='service', master_id=staff_id,
            company_id=company_id,
        )
        for visit_id, company_id, staff_id, amount in paid
    ])
    await async_session.commit()


def _march_plan_role(staff_id, category):
    return PlanMetric(
        period_start=date(2025, 3, 1), period_end=date(2025, 3, 31), company_id=1, staff_id=staff_id,
        staff_category=category, metric_code='clients', value=1.0, updated_at=datetime(2025, 3, 1),
    )


@pytest.mark.asyncio
async def test_the_selector_level_is_the_latest_snapshot_up_to_the_period_end_month(async_session):
    await _seed_branches(async_session)
    async_session.add_all([
        StaffPositionMonth(
            staff_id=1, month=date(2025, 2, 1), company_id=1, position_id=100, position_title='Барбер',
            updated_at=FACTUAL_AT,
        ),
        StaffPositionMonth(
            staff_id=1, month=date(2025, 4, 1), company_id=1, position_id=200, position_title='ТОП-барбер',
            updated_at=FACTUAL_AT,
        ),
    ])
    await async_session.commit()

    async def levels(end):
        async_session.info.clear()
        listed = await dashboard_service.fetch_staff(async_session, start=MARCH[0], end=end)
        return {item['id']: (item['position_id'], item['level']) for item in listed}

    base, top = (100, {'key': 'барбер', 'title': 'Барбер'}), (200, {'key': 'топбарбер', 'title': 'ТОП-барбер'})
    assert (await levels(MARCH[1]))[1] == base
    # A promotion counts from the month it was snapshotted, so a period ending in it reads the new level.
    assert (await levels(date(2025, 4, 1)))[1] == top
    assert (await levels(date(2025, 12, 31)))[1] == top
    # Staff 2 has no snapshot at all: the period falls back to the position the staff row holds now.
    assert (await levels(MARCH[1]))[2] == top


@pytest.mark.asyncio
async def test_fetch_staff_gives_barbers_and_mixed_a_level_and_administrators_none(async_session):
    await _seed_branches(async_session)
    async_session.add_all([
        Staff(id=5, name='No id', position='Барбер', company_id=1),
        # A barber by plan category whose title does not read as a barber: the Overview group would omit him, so
        # the selector must not offer a level for him either.
        Staff(id=6, name='Trainee', position='Стажёр', position_id=400, company_id=1),
    ])
    await async_session.commit()

    by_id = {item['id']: item for item in await dashboard_service.fetch_staff(async_session)}

    assert by_id[1]['level'] == {'key': 'барбер', 'title': 'Барбер'}
    assert by_id[2]['level'] == {'key': 'топбарбер', 'title': 'ТОП-барбер'}
    assert by_id[2]['position_id'] == 200
    assert by_id[3]['level'] is None and by_id[3]['position_id'] == 300
    assert by_id[4]['level'] == {'key': 'топбарбер', 'title': 'ТОП Барбер'}
    assert by_id[5]['level'] is None and by_id[5]['position_id'] is None
    assert by_id[6]['level'] is None and by_id[6]['position_id'] == 400
    assert by_id[1]['position'] == 'Барбер'

    # With a period the id and title come from the snapshot, not from the current row.
    async_session.add(StaffPositionMonth(
        staff_id=1, month=date(2025, 3, 1), company_id=1, position_id=500, position_title='ПРЕМИУМ-барбер',
        updated_at=FACTUAL_AT,
    ))
    await async_session.commit()
    async_session.info.clear()
    listed = await dashboard_service.fetch_staff(async_session, start=MARCH[0], end=MARCH[1])
    periodic = {item['id']: item for item in listed}
    assert periodic[1]['position_id'] == 500
    assert periodic[1]['level'] == {'key': 'премиумбарбер', 'title': 'ПРЕМИУМ-барбер'}

    # A person who worked the stand in March and the chair in April is `mixed` and still gets a level.
    async_session.add(_march_plan_role(2, 'administrator'))
    await async_session.commit()
    async_session.info.clear()
    listed = await dashboard_service.fetch_staff(async_session, start=MARCH[0], end=APRIL[1])
    by_id = {item['id']: item for item in listed}
    assert by_id[2]['category'] == 'mixed'
    assert by_id[2]['level'] == {'key': 'топбарбер', 'title': 'ТОП-барбер'}


@pytest.mark.asyncio
async def test_a_mixed_person_who_ends_the_period_as_an_administrator_has_no_level(async_session):
    await _seed_branches(async_session)
    # Staff 3 is an «Администратор» (id 300) who sat in a barber chair in March.
    async_session.add(_march_plan_role(3, 'barber'))
    await async_session.commit()

    listed = await dashboard_service.fetch_staff(async_session, start=MARCH[0], end=APRIL[1])
    by_id = {item['id']: item for item in listed}

    # It would otherwise offer a barber level named after the post.
    assert by_id[3]['category'] == 'mixed'
    assert by_id[3]['level'] is None
    assert by_id[2]['level'] == {'key': 'топбарбер', 'title': 'ТОП-барбер'}


# ---------------------------------------------------------------------------
# Plan/fact filter
# ---------------------------------------------------------------------------

def _staff_ids(payload):
    return [group['staff_id'] for group in payload['groups']]


@pytest.mark.asyncio
async def test_single_branch_filter_keeps_matching_barbers_and_drops_branch_totals(async_session):
    await _seed_branches(async_session)

    payload = await fetch_plan_fact(
        async_session, *MARCH, company_id=1, factual_at=FACTUAL_AT, staff_position_ids=frozenset({200, 300}),
    )

    assert _staff_ids(payload) == [2]
    assert 'parent_group' not in payload
    assert payload['diagnostics'] == []
    assert payload['view_scope'] == 'staff'
    assert payload['level_filter'] == {'position_ids': [200, 300], 'titles': ['ТОП-барбер']}
    assert 'plan_rules' in payload
    async_session.info.clear()
    unfiltered = await fetch_plan_fact(async_session, *MARCH, company_id=1, factual_at=FACTUAL_AT)
    assert 'parent_group' in unfiltered and 'level_filter' not in unfiltered
    assert set(_staff_ids(unfiltered)) >= {1, 2}


@pytest.mark.asyncio
async def test_network_filter_lists_matching_staff_of_every_branch_without_network_rows(async_session):
    await _seed_branches(async_session)

    payload = await fetch_plan_fact(
        async_session, *MARCH, factual_at=FACTUAL_AT, staff_position_ids=frozenset({200, 201}),
    )

    assert payload['view_scope'] == 'staff'
    assert _staff_ids(payload) == [2, 4]
    assert [(group['company_id'], group['company_title']) for group in payload['groups']] == [(1, 'Alpha'), (2, 'Beta')]
    assert {group['scope'] for group in payload['groups']} == {'staff'}
    assert payload['plan_rules'] is None
    assert 'branch' not in payload and 'parent_group' not in payload
    assert payload['diagnostics'] == []
    assert payload['level_filter']['position_ids'] == [200, 201]
    # One title per level: «ТОП Барбер» and «ТОП-барбер» are the same level under two ids.
    assert payload['level_filter']['titles'] == ['ТОП-барбер']


@pytest.mark.asyncio
async def test_the_filter_reads_the_level_as_of_the_period_end(async_session):
    await _seed_branches(async_session)
    async_session.add_all([
        StaffPositionMonth(
            staff_id=1, month=date(2025, 3, 1), company_id=1, position_id=100, position_title='Барбер',
            updated_at=FACTUAL_AT,
        ),
        StaffPositionMonth(
            staff_id=1, month=date(2025, 4, 1), company_id=1, position_id=200, position_title='ТОП-барбер',
            updated_at=FACTUAL_AT,
        ),
    ])
    await async_session.commit()

    march = await fetch_plan_fact(async_session, *MARCH, company_id=1, factual_at=FACTUAL_AT,
                                  staff_position_ids=frozenset({200}))
    async_session.info.clear()
    april = await fetch_plan_fact(async_session, *APRIL, company_id=1, factual_at=FACTUAL_AT,
                                  staff_position_ids=frozenset({200}))

    assert _staff_ids(march) == [2]
    assert _staff_ids(april) == [1, 2]


@pytest.mark.asyncio
async def test_a_selected_staff_member_wins_over_the_filter(async_session):
    await _seed_branches(async_session)

    payload = await fetch_plan_fact(
        async_session, *MARCH, company_id=1, staff_id=1, factual_at=FACTUAL_AT,
        staff_position_ids=frozenset({200}),
    )

    assert _staff_ids(payload) == [1]
    assert 'level_filter' not in payload
    assert 'parent_group' in payload


async def _client(async_session, ctx=None):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    if ctx is not None:
        async def override_access():
            return ctx

        app.dependency_overrides[dashboard_routes.get_dashboard_access] = override_access
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


PLAN_FACT_PARAMS = {'start_date': '2025-03-01', 'end_date': '2025-03-31'}


@pytest.mark.asyncio
async def test_plan_fact_route_parses_the_filter_and_rejects_malformed_ids(async_session):
    await _seed_branches(async_session)
    client = await _client(async_session)
    try:
        async with client:
            ok = await client.get(
                '/dashboard/widget/plan_fact', params={**PLAN_FACT_PARAMS, 'staff_position_ids': '201, 200,200'},
            )
            blank = await client.get(
                '/dashboard/widget/plan_fact', params={**PLAN_FACT_PARAMS, 'staff_position_ids': ''},
            )
            bad = [
                await client.get(
                    '/dashboard/widget/plan_fact', params={**PLAN_FACT_PARAMS, 'staff_position_ids': value},
                )
                for value in (
                    'abc', '1,,2', '-5', '0', '1.5', '99999999999', '\u0661', '1' * 5000,
                    ','.join(str(i) for i in range(1, 202)),
                )
            ]
    finally:
        app.dependency_overrides.clear()

    assert ok.status_code == 200
    data = ok.json()['data']
    assert data['level_filter']['position_ids'] == [200, 201]
    assert _staff_ids(data) == [2, 4]
    assert blank.status_code == 200 and 'level_filter' not in blank.json()['data']
    assert [response.status_code for response in bad] == [400] * 9
    assert 'staff_position_ids' in bad[0].json()['detail']


@pytest.mark.asyncio
async def test_a_personal_role_clamped_to_its_row_ignores_the_filter(async_session):
    await _seed_branches(async_session)
    ctx = AccessContext.from_user(user_id=10, role='barber', portal_account_id=1, company_ids=[1, 2], staff_id=1)
    client = await _client(async_session, ctx)
    try:
        async with client:
            response = await client.get(
                '/dashboard/widget/plan_fact', params={**PLAN_FACT_PARAMS, 'staff_position_ids': '200'},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert _staff_ids(data) == [1]
    assert 'level_filter' not in data and 'parent_group' not in data


@pytest.mark.asyncio
async def test_money_is_hidden_from_a_staff_scope_payload_without_a_branch_row(async_session):
    await _seed_branches(async_session)
    ctx = AccessContext.from_user(user_id=10, role='manager', portal_account_id=1, company_ids=[1, 2])
    client = await _client(async_session, ctx)
    try:
        async with client:
            response = await client.get(
                '/dashboard/widget/plan_fact', params={**PLAN_FACT_PARAMS, 'staff_position_ids': '200,201'},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['financials_hidden'] is True
    assert _staff_ids(data) == [2, 4]
    codes = {cell['code'] for group in data['groups'] for cell in group['metrics']}
    assert 'revenue' not in codes and 'clients' in codes


# ---------------------------------------------------------------------------
# Ratings report
# ---------------------------------------------------------------------------

def _board_group(staff_id, title, *, avg, visits, category='barber', company_id=1, company_title='Salon'):
    return {
        'staff_id': staff_id, 'title': title, 'company_id': company_id, 'company_title': company_title,
        'category': category,
        'metrics': [
            {'code': 'avg_check_total', 'plan': None, 'fact': avg},
            {'code': 'clients', 'plan': None, 'fact': visits},
        ],
    }


def test_the_average_check_board_ranks_barbers_and_skips_the_ineligible():
    boards = _staff_leaderboards_payload([
        _board_group(1, 'Low', avg=1000.0, visits=10.0),
        _board_group(2, 'High', avg=2500.0, visits=4.0),
        _board_group(3, 'No visits', avg=9000.0, visits=0.0),
        _board_group(4, 'No check', avg=None, visits=5.0),
        _board_group(5, 'Admin', avg=7000.0, visits=3.0, category='administrator'),
        _board_group(6, 'Tie A', avg=1000.0, visits=2.0),
    ])

    assert [(row['staff'], row['value'], row['visits']) for row in boards['avg_check_barber']] == [
        ('High', 2500.0, 4.0), ('Low', 1000.0, 10.0), ('Tie A', 1000.0, 2.0),
    ]
    assert boards['avg_check_barber'][0]['company_title'] == 'Salon'


@pytest.mark.asyncio
async def test_the_catalog_offers_the_level_filter_only_to_the_ratings(async_session):
    client = await _client(async_session)
    try:
        async with client:
            response = await client.get('/dashboard/reports')
    finally:
        app.dependency_overrides.clear()

    levels = {item['id']: item['filters']['staff_level'] for item in response.json()['data']}
    assert levels['staff_leaderboard'] is True
    assert [report_id for report_id, flag in levels.items() if flag] == ['staff_leaderboard']


def _board_staff_ids(data, table_id):
    return {row['staff_id'] for table in data['tables'] if table['id'] == table_id for row in table['rows']}


@pytest.mark.asyncio
async def test_the_ratings_narrow_to_the_level_and_drop_administrator_and_branch_tables(async_session):
    await _seed_branches(async_session)
    await _seed_march_revenue(async_session)
    client = await _client(async_session)
    params = {'report_id': 'staff_leaderboard', **PLAN_FACT_PARAMS}
    try:
        async with client:
            narrowed = await client.get('/dashboard/reports/data', params={**params, 'staff_position_ids': '200,201'})
            everyone = await client.get('/dashboard/reports/data', params=params)
            ignored = await client.get(
                '/dashboard/reports/data',
                params={**{**params, 'report_id': 'peak_load'}, 'staff_position_ids': '200'},
            )
            bad = await client.get('/dashboard/reports/data', params={**params, 'staff_position_ids': 'x'})
    finally:
        app.dependency_overrides.clear()

    assert narrowed.status_code == 200
    data = narrowed.json()['data']
    all_data = everyone.json()['data']
    # Without the filter the base barber (id 1, position 100) is ranked; with it only the two top barbers are.
    for board in ('revenue_barber', 'avg_check_barber'):
        assert _board_staff_ids(all_data, board) == {1, 2, 4}, board
        assert _board_staff_ids(data, board) == {2, 4}, board
    table_ids = {table['id'] for table in data['tables']}
    hidden = {'extra_services_admin', 'cosmo_admin', 'opz_admin', 'reviews_admin', 'avg_check_plan_branch'}
    assert not table_ids & hidden
    assert 'reviews_admin' in {table['id'] for table in all_data['tables']}
    note = next(note for note in data['notes'] if note['title'] == 'Рейтинги по ролям барберов')
    assert note['kind'] == 'info'
    assert 'ТОП-барбер' in note['text'] and '200' not in note['text']
    assert ignored.status_code == 200
    assert bad.status_code == 400


@pytest.mark.asyncio
async def test_the_average_check_table_follows_the_avg_check_permission(async_session):
    await _seed_branches(async_session)
    await _seed_march_revenue(async_session)
    params = {'report_id': 'staff_leaderboard', **PLAN_FACT_PARAMS}
    seen = {}
    for name, metrics in (('without', frozenset({'cosmo_sum'})), ('with', frozenset({'avg_check'}))):
        ctx = AccessContext.from_user(
            user_id=10, role='manager', portal_account_id=1, company_ids=[1, 2], money_metrics=metrics,
        )
        client = await _client(async_session, ctx)
        try:
            async with client:
                response = await client.get('/dashboard/reports/data', params=params)
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        seen[name] = response.json()['data']

    assert 'avg_check_barber' not in {table['id'] for table in seen['without']['tables']}
    table = next(table for table in seen['with']['tables'] if table['id'] == 'avg_check_barber')
    assert {row['staff_id']: (row['value'], row['visits']) for row in table['rows']} == {
        1: (1000.0, 1.0), 2: (2000.0, 1.0), 4: (3000.0, 1.0),
    }
    # The check is allowed; the revenue it is made of is not.
    assert 'revenue_barber' not in {table['id'] for table in seen['with']['tables']}
    assert 'avg_check_barber' not in json.dumps(seen['without'])


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_the_migration_adds_and_removes_the_position_objects():
    import sqlalchemy as sa
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migration = load_migration('0060_staff_position_levels.py', 'migration_0060')
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE companies (id INTEGER PRIMARY KEY)'))
        conn.execute(sa.text('CREATE TABLE staff (id INTEGER PRIMARY KEY, name VARCHAR)'))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        inspector = sa.inspect(conn)
        assert 'position_id' in {column['name'] for column in inspector.get_columns('staff')}
        assert {column['name'] for column in inspector.get_columns('staff_position_months')} == {
            'staff_id', 'month', 'company_id', 'position_id', 'position_title', 'updated_at',
        }
        assert inspector.get_pk_constraint('staff_position_months')['constrained_columns'] == ['staff_id', 'month']
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        inspector = sa.inspect(conn)
        assert 'staff_position_months' not in inspector.get_table_names()
        assert 'position_id' not in {column['name'] for column in inspector.get_columns('staff')}


# ---------------------------------------------------------------------------
# Overview filter: the selected barbers as one group
# ---------------------------------------------------------------------------

GROUP = frozenset({200, 201})


async def _seed_overview(async_session):
    """Branch 1 and 2 as in `_seed_branches`, plus a second top barber, a shared client, rebookings and February."""
    await _seed_branches(async_session)
    await _seed_march_revenue(async_session)
    async_session.add_all([
        Staff(id=5, name='Second top', position='ТОП-барбер', position_id=200, company_id=1),
        Staff(id=6, name='Fired top', position='ТОП-барбер', position_id=200, company_id=1, fired=1),
    ])
    await async_session.flush()
    async_session.add_all([
        # Client 6 visits two members of the group: one client, two visits.
        _visit(6, 6, date(2025, 3, 6), 2),
        _visit(7, 6, date(2025, 3, 7), 5),
        _visit(8, 7, date(2025, 3, 8), 1),
        _visit(9, 8, date(2025, 3, 9), 6),
        # February: the baseline of the deltas. Staff 1 is not in the group.
        _visit(30, 20, date(2025, 2, 10), 2),
        _visit(31, 21, date(2025, 2, 11), 1),
    ])
    for visit_id, staff_id, amount in ((6, 2, 500.0), (7, 5, 700.0), (8, 1, 100.0), (9, 6, 900.0)):
        async_session.add(Transaction(
            id=visit_id, appointment_id=visit_id, service_id=visit_id, service_title='Стрижка',
            cost=amount, first_cost=amount, amount=1, company_id=1,
        ))
        async_session.add(FinancialTransaction(
            id=visit_id, date=datetime(2025, 3, 20 + visit_id % 5, 12, 0), amount=amount, record_id=visit_id,
            visit_id=visit_id, sold_item_id=visit_id, sold_item_type='service', master_id=staff_id, company_id=1,
        ))
    # Rebookings made after a visit: the events of the visit's barber (2, 4 and, not in the group, 1).
    for booking_id, client_id, company_id, staff_id, created in (
        (50, 2, 1, 2, datetime(2025, 3, 4, 15)),
        (51, 3, 2, 4, datetime(2025, 3, 5, 16)),
        (52, 1, 1, 1, datetime(2025, 3, 3, 15)),
        (53, 6, 1, 5, datetime(2025, 3, 7, 15)),
    ):
        async_session.add(Appointment(
            id=booking_id, company_id=company_id, client_id=client_id, staff_id=staff_id,
            date=date(2025, 3, 25), datetime=datetime(2025, 3, 25, 12), attendance=0, create_date=created,
        ))
    # An administrator's manual top-up that an unfiltered Overview adds to OPZ.
    async_session.add(ManualFactMetric(
        period_start=date(2025, 3, 1), period_end=date(2025, 3, 31), company_id=1, staff_id=3,
        metric_code='opz_qty', value=4.0, updated_at=datetime(2025, 3, 20, 10),
    ))
    await async_session.commit()
    async_session.info.clear()


@pytest.fixture
def no_upstream(monkeypatch):
    """YClients' record stats are never reachable here; a group must not even ask."""
    calls = []

    async def fail(*args, **kwargs):
        calls.append(args)
        raise dashboard_service.yclients_analytics.YClientsAnalyticsError('offline')

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fail)
    return calls


async def _summary(async_session, staff, **kwargs):
    async_session.info.clear()
    return await dashboard_service.fetch_summary(
        async_session, *MARCH, staff_id=staff, factual_at=FACTUAL_AT, **kwargs,
    )


@pytest.mark.asyncio
async def test_staff_ids_for_levels_picks_barbers_of_the_scope_only(async_session):
    await _seed_overview(async_session)
    async_session.add_all([
        Staff(id=7, name='Лист ожидания', position='ТОП-барбер', position_id=200, company_id=1),
        Staff(id=8, name='Owner mirror', position='owner', position_id=200, company_id=1),
    ])
    await async_session.commit()

    async def members(**kwargs):
        async_session.info.clear()
        args = {'company_id': None, 'allowed_company_ids': None, 'position_ids': GROUP, **kwargs}
        return await dashboard_service.staff_ids_for_levels(async_session, MARCH[1], **args)

    # The fired barber resolves into the level, so his visits count; the waitlist row and the mirror do not.
    assert await members() == (frozenset({2, 4, 5, 6}), ['ТОП-барбер'])
    assert (await members(company_id=1))[0] == frozenset({2, 5, 6})
    assert (await members(allowed_company_ids=[2]))[0] == frozenset({4})
    assert (await members(company_id=2, allowed_company_ids=[1]))[0] == frozenset()
    # An administrator never matches, even on a position id that is in the set.
    assert await members(position_ids=frozenset({300})) == (frozenset(), [])
    assert await members(position_ids=frozenset({100, 200})) == (
        frozenset({1, 2, 5, 6}), ['Барбер', 'ТОП-барбер'],
    )


@pytest.mark.asyncio
async def test_a_group_overview_is_the_sum_of_its_people(async_session, no_upstream):
    await _seed_overview(async_session)
    members = (await dashboard_service.staff_ids_for_levels(async_session, MARCH[1], None, None, GROUP))[0]
    assert members == frozenset({2, 4, 5, 6})

    group = await _summary(async_session, members)
    # YClients' record stats filter one employee at most; the group reads the synced records.
    assert no_upstream == []
    singles = [await _summary(async_session, staff_id) for staff_id in sorted(members)]
    whole = await _summary(async_session, None)

    for key in ('appointments', 'total', 'service_count'):
        assert group['revenue'][key] == sum(single['revenue'][key] for single in singles), key
    assert group['revenue']['appointments'] == 5
    assert group['revenue']['total'] == 3000.0 + 2000.0 + 500.0 + 700.0 + 900.0
    assert group['visit_metrics']['opz_qty'] == sum(single['visit_metrics']['opz_qty'] for single in singles) == 3.0
    first, repeat = 'opz_first_visit_qty', 'opz_repeat_visit_qty'
    assert group['visit_metrics'][first] + group['visit_metrics'][repeat] == 3.0
    # Client 6 visited two of the barbers: unique clients of the group count him once.
    assert group['visit_metrics']['unique_clients'] == 4
    assert sum(single['visit_metrics']['unique_clients'] for single in singles) == 5
    assert group['revenue']['appointments'] < whole['revenue']['appointments']
    # The group is a staff filter: no manual top-up (the unfiltered branch carries it) and no administrator mode.
    assert whole['visit_metrics']['opz_qty'] > group['visit_metrics']['opz_qty']
    assert group['service_attribution']['mode'] == 'master' and 'administrator_personal' not in group
    assert group['average_check']['total'] == group['revenue']['total'] / 5
    # The baseline holds the same people: one February visit of staff 2, none of staff 1.
    assert group['revenue']['appointments_change_pct'] == 400.0
    assert group['appointments_breakdown']['source_status'] == 'local'
    assert group['appointments_breakdown']['local_reason'] == 'staff_group'
    assert group['appointments_breakdown']['completed'] == 5


@pytest.mark.asyncio
async def test_a_group_top_services_and_daily_series_add_up(async_session, no_upstream):
    await _seed_overview(async_session)
    members = frozenset({2, 4, 5})

    async def services(staff):
        async_session.info.clear()
        return await dashboard_service.fetch_top_services(
            async_session, *MARCH, None, 10, staff, factual_at=FACTUAL_AT, use_administrator_schedule=True,
        )

    async def daily(staff):
        async_session.info.clear()
        return await dashboard_service.fetch_revenue_daily(
            async_session, *MARCH, None, staff, factual_at=FACTUAL_AT, administrator_attribution=True,
        )

    group_services = await services(members)
    singles = [await services(staff_id) for staff_id in sorted(members)]
    assert [(row['title'], row['sold'], row['revenue']) for row in group_services] == [
        ('Стрижка', 4, 3000.0 + 2000.0 + 500.0 + 700.0),
    ]
    assert sum(row['sold'] for rows in singles for row in rows) == 4

    group_days = await daily(members)
    single_days = [await daily(staff_id) for staff_id in sorted(members)]
    assert sum(day['appointments'] for day in group_days) == 4
    assert sum(day['revenue'] for day in group_days) == sum(day['revenue'] for days in single_days for day in days)
    assert sum(day['opz_qty'] for day in group_days) == 3


@pytest.mark.asyncio
async def test_a_group_nobody_belongs_to_is_zeros_never_the_whole_branch(async_session, no_upstream):
    await _seed_overview(async_session)

    empty = await _summary(async_session, frozenset())
    whole = await _summary(async_session, None)

    assert whole['revenue']['appointments'] > 0
    assert empty['revenue']['appointments'] == 0 and empty['revenue']['total'] == 0
    assert empty['visit_metrics']['opz_qty'] == 0 and empty['visit_metrics']['unique_clients'] == 0
    assert empty['appointments_breakdown']['completed'] == 0
    async_session.info.clear()
    assert await dashboard_service.fetch_top_services(
        async_session, *MARCH, None, 10, frozenset(), factual_at=FACTUAL_AT,
    ) == []
    async_session.info.clear()
    days = await dashboard_service.fetch_revenue_daily(
        async_session, *MARCH, None, frozenset(), factual_at=FACTUAL_AT,
    )
    assert sum(day['appointments'] + day['opz_qty'] + day['revenue'] for day in days) == 0


@pytest.mark.asyncio
async def test_one_session_keeps_two_groups_and_a_single_person_apart(async_session, no_upstream):
    await _seed_overview(async_session)

    async def summary(staff):
        # No `info.clear()` between the calls: the session's caches must tell the filters apart.
        return await dashboard_service.fetch_summary(
            async_session, *MARCH, staff_id=staff, factual_at=FACTUAL_AT,
        )

    first = await summary(frozenset({2, 4}))
    second = await summary(frozenset({2, 4, 5, 6}))
    one = await summary(2)
    again = await summary(frozenset({2, 4}))

    assert [first['revenue']['appointments'], second['revenue']['appointments'], one['revenue']['appointments']] == [
        3, 5, 2,
    ]
    assert first['revenue']['total'] == 5500.0 and second['revenue']['total'] == 7100.0
    assert one['revenue']['total'] == 2500.0
    assert [first['visit_metrics']['opz_qty'], second['visit_metrics']['opz_qty'], one['visit_metrics']['opz_qty']] == [
        2.0, 3.0, 1.0,
    ]
    assert again == first


BUNDLE_PARAMS = {'start_date': '2025-03-01', 'end_date': '2025-03-31'}


@pytest.mark.asyncio
async def test_bundle_route_applies_the_level_and_echoes_it(async_session, no_upstream):
    await _seed_overview(async_session)
    client = await _client(async_session)
    try:
        async with client:
            grouped = await client.get('/dashboard/bundle', params={**BUNDLE_PARAMS, 'staff_position_ids': '200,201'})
            nobody = await client.get('/dashboard/bundle', params={**BUNDLE_PARAMS, 'staff_position_ids': '999'})
            admins = await client.get('/dashboard/bundle', params={**BUNDLE_PARAMS, 'staff_position_ids': '300'})
            person = await client.get(
                '/dashboard/bundle', params={**BUNDLE_PARAMS, 'staff_id': 1, 'staff_position_ids': '200'},
            )
            plain = await client.get('/dashboard/bundle', params=BUNDLE_PARAMS)
            bad = [
                await client.get('/dashboard/bundle', params={**BUNDLE_PARAMS, 'staff_position_ids': value})
                for value in ('abc', '1,,2', '-5', '0', ','.join(str(i) for i in range(1, 202)))
            ]
    finally:
        app.dependency_overrides.clear()

    data = grouped.json()['data']
    assert data['summary']['level_filter'] == {'position_ids': [200, 201], 'titles': ['ТОП-барбер'], 'staff_count': 4}
    assert data['summary']['revenue']['appointments'] == 5
    assert [row['sold'] for row in data['top_services']] == [5]
    assert sum(day['appointments'] for day in data['revenue_daily']) == 5

    for response in (nobody, admins):
        empty = response.json()['data']
        assert empty['summary']['level_filter']['staff_count'] == 0
        assert empty['summary']['revenue']['appointments'] == 0 and empty['top_services'] == []
    assert admins.json()['data']['summary']['level_filter']['titles'] == []

    # A selected person wins; without the param nothing changes.
    assert 'level_filter' not in person.json()['data']['summary']
    assert person.json()['data']['summary']['revenue']['appointments'] == 2
    assert 'level_filter' not in plain.json()['data']['summary']
    assert plain.json()['data']['summary']['revenue']['appointments'] > 5
    assert [response.status_code for response in bad] == [400] * 5


@pytest.mark.asyncio
async def test_bundle_route_a_personal_role_ignores_the_level(async_session, no_upstream):
    await _seed_overview(async_session)
    ctx = AccessContext.from_user(user_id=10, role='barber', portal_account_id=1, company_ids=[1, 2], staff_id=1)
    client = await _client(async_session, ctx)
    try:
        async with client:
            response = await client.get('/dashboard/bundle', params={**BUNDLE_PARAMS, 'staff_position_ids': '200'})
    finally:
        app.dependency_overrides.clear()

    summary = response.json()['data']['summary']
    assert response.status_code == 200
    assert 'level_filter' not in summary
    # Money is hidden for this role; clients 1 and 7 are staff 1's own.
    assert summary['visit_metrics']['unique_clients'] == 2


@pytest.mark.asyncio
async def test_bundle_route_still_hides_money_under_a_level(async_session, no_upstream):
    await _seed_overview(async_session)
    ctx = AccessContext.from_user(user_id=10, role='manager', portal_account_id=1, company_ids=[1, 2])
    client = await _client(async_session, ctx)
    try:
        async with client:
            response = await client.get('/dashboard/bundle', params={**BUNDLE_PARAMS, 'staff_position_ids': '200,201'})
    finally:
        app.dependency_overrides.clear()

    data = response.json()['data']
    assert response.status_code == 200
    assert data['financials_hidden'] is True
    assert data['summary']['level_filter']['staff_count'] == 4
    assert 'revenue' not in data['summary'] and data['summary']['visit_metrics']['unique_clients'] == 4


@pytest.mark.asyncio
async def test_staff_count_is_the_people_the_numbers_are_about(async_session, no_upstream):
    await _seed_overview(async_session)
    # Fired and never seen in a completed visit: in the group, but nothing of the figures is his.
    async_session.add(Staff(id=9, name='Fired idle', position='ТОП-барбер', position_id=200, company_id=1, fired=1))
    await async_session.commit()
    client = await _client(async_session)

    async def count(start, end):
        async_session.info.clear()
        response = await client.get(
            '/dashboard/bundle', params={'start_date': start, 'end_date': end, 'staff_position_ids': '200,201'},
        )
        return response.json()['data']['summary']['level_filter']['staff_count']

    try:
        async with client:
            # March: staff 2, 4, 5 work, the fired staff 6 has a completed visit; staff 9 has none.
            assert await count('2025-03-01', '2025-03-31') == 4
            # April: only staff 1 and 2 have visits; the fired 6 and 9 have none, so only the working barbers count.
            assert await count('2025-04-01', '2025-04-30') == 3
    finally:
        app.dependency_overrides.clear()
