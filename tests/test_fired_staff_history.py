"""A fired employee stays in the months he worked in and drops out of the following ones."""

from datetime import date, datetime, time

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import api
from api import app
from models import (
    Appointment,
    Client,
    Company,
    FinancialTransaction,
    Group,
    ManualFactMetric,
    PlanStaffInput,
    Staff,
    StaffSchedule,
    Transaction,
)

JANUARY = {'start_date': '2025-01-01', 'end_date': '2025-01-31'}
FEBRUARY = {'start_date': '2025-02-01', 'end_date': '2025-02-28'}
ADMIN_USER_ID = 900


@pytest_asyncio.fixture
async def january_data(async_session):
    """Branch 1: a working barber, a fired barber with January visits, a fired barber with none, an administrator."""
    async_session.add_all([Group(id=1, title='G1'), Company(id=1, title='Salon', group_id=1)])
    async_session.add_all([
        Staff(id=1, name='Working', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Left in spring', position='Барбер', company_id=1, fired=1),
        Staff(id=3, name='Never came', position='Барбер', company_id=1, fired=1),
        Staff(id=4, name='Desk', position='Администратор', company_id=1, fired=0, user_id=ADMIN_USER_ID),
        *[Client(id=client_id, name=f'Client {client_id}', company_id=1) for client_id in (1, 2, 3)],
    ])
    await async_session.flush()
    visits = [(1, 1, 1, 10), (2, 2, 2, 11), (3, 2, 3, 12)]
    async_session.add_all([
        Appointment(
            id=visit_id,
            company_id=1,
            staff_id=staff_id,
            client_id=client_id,
            created_user_id=ADMIN_USER_ID,
            date=date(2025, 1, day),
            datetime=datetime(2025, 1, day, 12),
            create_date=datetime(2025, 1, day - 1, 12),
            seance_length=3600,
            attendance=1,
        )
        for visit_id, staff_id, client_id, day in visits
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(
            id=visit_id, appointment_id=visit_id, service_id=100 + visit_id, service_title='воск',
            cost=1000.0, first_cost=1000.0, amount=1, company_id=1,
        )
        for visit_id, *_ in visits
    ])
    async_session.add_all([
        FinancialTransaction(
            id=visit_id, date=datetime(2025, 1, day, 12), amount=1000.0, record_id=visit_id, visit_id=visit_id,
            sold_item_id=100 + visit_id, sold_item_type='service', master_id=staff_id, company_id=1,
        )
        for visit_id, staff_id, _client_id, day in visits
    ])
    await async_session.commit()


@pytest_asyncio.fixture
async def http(async_session, january_data):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        yield client
    app.dependency_overrides.clear()


async def _plan_fact(http, period, **params):
    response = await http.get('/dashboard/widget/plan_fact', params={**period, **params})
    assert response.status_code == 200
    return response.json()['data']


def _fact(cells, code):
    return next(cell for cell in cells if cell['code'] == code)['fact']


def _staff_names(data):
    return {group['title'] for group in data['groups'] if group.get('staff_id') is not None}


@pytest.mark.asyncio
async def test_fired_barber_has_a_row_only_in_the_months_he_worked(http):
    january = await _plan_fact(http, JANUARY, company_id=1)
    february = await _plan_fact(http, FEBRUARY, company_id=1)

    january_names = _staff_names(january)
    assert 'Left in spring' in january_names
    assert 'Never came' not in january_names
    assert 'Left in spring' not in _staff_names(february)


@pytest.mark.asyncio
async def test_visits_of_a_fired_barber_are_assigned_to_the_administrator(http):
    data = await _plan_fact(http, JANUARY, company_id=1)

    admins = [group for group in data['groups'] if group['category'] == 'administrator']
    branch_clients = _fact(data['parent_group']['metrics'], 'clients')
    assert sum(_fact(group['metrics'], 'clients') for group in admins) == branch_clients


@pytest.mark.asyncio
async def test_goods_kpi_tile_equals_the_branch_total_with_a_fired_barber(http):
    data = await _plan_fact(http, JANUARY, company_id=1)

    tile = next(row for row in data['goods_kpi_execution'] if row['code'] == 'wax_qty')
    assert tile['fact'] == 3.0
    assert tile['fact'] == _fact(data['parent_group']['metrics'], 'wax_qty')


@pytest.mark.asyncio
async def test_staff_selector_lists_a_fired_barber_only_for_his_months(http):
    async def names(params):
        response = await http.get('/dashboard/staff', params={'company_id': 1, **params})
        assert response.status_code == 200
        return {row['name'] for row in response.json()['data']}

    assert 'Left in spring' in await names(JANUARY)
    assert 'Left in spring' not in await names(FEBRUARY)
    assert 'Never came' not in await names(JANUARY)
    assert not {'Left in spring', 'Never came'} & await names({})


@pytest.mark.asyncio
async def test_ratings_include_a_fired_barber_of_the_period(http):
    response = await http.get(
        '/dashboard/reports/data', params={'report_id': 'staff_leaderboard', **JANUARY, 'company_id': 1}
    )
    assert response.status_code == 200
    table = next(table for table in response.json()['data']['tables'] if table['id'] == 'revenue_barber')
    assert 'Left in spring' in {row['staff'] for row in table['rows']}


@pytest.mark.asyncio
async def test_fired_barber_with_only_an_unfinished_booking_is_gone_next_month(http, async_session):
    async_session.add(Appointment(
        id=50, company_id=1, staff_id=2, client_id=1, date=date(2025, 2, 20), datetime=datetime(2025, 2, 20, 12),
        create_date=datetime(2025, 1, 20, 12), seance_length=3600, attendance=0,
    ))
    await async_session.commit()

    assert 'Left in spring' not in _staff_names(await _plan_fact(http, FEBRUARY, company_id=1))
    response = await http.get('/dashboard/staff', params={'company_id': 1, **FEBRUARY})
    assert 'Left in spring' not in {row['name'] for row in response.json()['data']}


QUARTER = {'start_date': '2025-01-01', 'end_date': '2025-03-31'}


async def _quarter_visits(async_session, *, fired_barber_in_january=False):
    """Two completed visits of the working barber in each of January-March, no shifts at all."""
    async_session.add_all([
        Appointment(
            id=100 + month * 10 + visit, company_id=1, staff_id=1, client_id=1, date=date(2025, month, 10 + visit),
            datetime=datetime(2025, month, 10 + visit, 12), create_date=datetime(2025, month, 9, 12),
            seance_length=3600, attendance=1,
        )
        for month in (1, 2, 3)
        for visit in (1, 2)
    ])
    if fired_barber_in_january:
        async_session.add(Appointment(
            id=99, company_id=1, staff_id=2, client_id=1, date=date(2025, 1, 20), datetime=datetime(2025, 1, 20, 12),
            create_date=datetime(2025, 1, 19, 12), seance_length=3600, attendance=1,
        ))
    await async_session.commit()


@pytest.mark.asyncio
async def test_fired_administrator_takes_no_share_of_the_months_after_he_left(http, async_session):
    async_session.add(Staff(id=5, name='Left desk', position='Администратор', company_id=1, fired=1, user_id=901))
    async_session.add(PlanStaffInput(
        period_start=date(2025, 1, 1), period_end=date(2025, 1, 31), company_id=1, staff_id=5,
        staff_category='administrator', updated_at=datetime(2024, 12, 1),
    ))
    await _quarter_visits(async_session)

    data = await _plan_fact(http, QUARTER, company_id=1)

    clients = {
        group['title']: _fact(group['metrics'], 'clients')
        for group in data['groups'] if group['category'] == 'administrator'
    }
    # Both of his January visits could be his; the six visits of February and March belong to the present desk.
    assert clients['Left desk'] <= 2
    assert clients['Desk'] >= 4
    assert sum(clients.values()) == _fact(data['parent_group']['metrics'], 'clients')


@pytest.mark.asyncio
async def test_fired_barber_row_over_a_quarter_keeps_the_facts_of_his_month(http, async_session):
    await _quarter_visits(async_session, fired_barber_in_january=True)

    data = await _plan_fact(http, QUARTER, company_id=1)

    row = next(group for group in data['groups'] if group['title'] == 'Left in spring')
    # The two January visits of the fixture plus the one added here; nothing from February and March.
    assert _fact(row['metrics'], 'clients') == 3


@pytest.mark.asyncio
async def test_fired_administrator_whose_last_shift_ends_the_month_takes_no_share_of_the_next_one(http, async_session):
    async_session.add(Staff(id=5, name='Left desk', position='Администратор', company_id=1, fired=1, user_id=901))
    async_session.add(StaffSchedule(
        staff_id=5, company_id=1, date=date(2025, 1, 31), slot_from=time(9), slot_to=time(18),
    ))
    await _quarter_visits(async_session)

    data = await _plan_fact(http, FEBRUARY, company_id=1)

    clients = {
        group['title']: _fact(group['metrics'], 'clients')
        for group in data['groups'] if group['category'] == 'administrator'
    }
    assert clients.get('Left desk', 0) == 0
    assert clients['Desk'] == _fact(data['parent_group']['metrics'], 'clients')


@pytest.mark.asyncio
async def test_fired_administrator_role_does_not_bridge_a_month_he_sat_out(async_session, january_data):
    import dashboard_service

    async_session.add(Staff(id=5, name='Left desk', position='Администратор', company_id=1, fired=1, user_id=901))
    async_session.add_all([
        StaffSchedule(staff_id=5, company_id=1, date=date(2025, month, 5), slot_from=time(9), slot_to=time(18))
        for month in (1, 3)
    ])
    await async_session.commit()

    roles = await dashboard_service._administrator_role_periods_by_company(
        async_session, date(2025, 1, 1), date(2025, 3, 31), [1]
    )

    assert roles[1][5] == [(date(2025, 1, 1), date(2025, 1, 31)), (date(2025, 3, 1), date(2025, 3, 31))]


@pytest.mark.asyncio
async def test_manual_reviews_of_a_fired_administrator_count_in_a_month_without_other_traces(http, async_session):
    async_session.add(Staff(id=5, name='Left desk', position='Администратор', company_id=1, fired=1, user_id=901))
    async_session.add(ManualFactMetric(
        period_start=date(2025, 2, 1), period_end=date(2025, 2, 28), company_id=1, staff_id=5,
        metric_code='reviews_qty', value=4.0, updated_at=datetime(2025, 3, 1),
    ))
    await async_session.commit()

    data = await _plan_fact(http, QUARTER, company_id=1)

    assert _fact(data['parent_group']['metrics'], 'reviews_qty') == 4.0
    assert 'Left desk' in _staff_names(data)
    assert 'Left desk' not in _staff_names(await _plan_fact(http, JANUARY, company_id=1))


@pytest.mark.asyncio
async def test_whole_month_manual_fact_does_not_keep_a_fired_administrator_in_a_part_of_the_month(http, async_session):
    async_session.add(Staff(id=5, name='Left desk', position='Администратор', company_id=1, fired=1, user_id=901))
    async_session.add(ManualFactMetric(
        period_start=date(2025, 2, 1), period_end=date(2025, 2, 28), company_id=1, staff_id=5,
        metric_code='reviews_qty', value=4.0, updated_at=datetime(2025, 3, 1),
    ))
    await async_session.commit()

    week = {'start_date': '2025-02-10', 'end_date': '2025-02-16'}
    assert 'Left desk' not in _staff_names(await _plan_fact(http, week, company_id=1))
    assert 'Left desk' in _staff_names(await _plan_fact(http, FEBRUARY, company_id=1))


@pytest.mark.asyncio
async def test_manual_fact_outside_the_reporting_window_is_not_a_trace(http, async_session):
    company = await async_session.get(Company, 1)
    company.reporting_start_date = date(2025, 1, 1)
    async_session.add(Staff(id=5, name='Left desk', position='Администратор', company_id=1, fired=1, user_id=901))
    async_session.add(ManualFactMetric(
        period_start=date(2024, 12, 5), period_end=date(2024, 12, 5), company_id=1, staff_id=5,
        metric_code='reviews_qty', value=4.0, updated_at=datetime(2024, 12, 6),
    ))
    await async_session.commit()

    december = {'start_date': '2024-12-01', 'end_date': '2024-12-31'}
    assert 'Left desk' not in _staff_names(await _plan_fact(http, december, company_id=1))


@pytest.mark.asyncio
async def test_visit_outside_the_reporting_window_is_not_a_trace(http, async_session):
    company = await async_session.get(Company, 1)
    company.reporting_start_date = date(2024, 12, 20)
    async_session.add(Staff(id=5, name='Left desk', position='Барбер', company_id=1, fired=1))
    async_session.add(Appointment(
        id=60, company_id=1, staff_id=5, client_id=1, date=date(2024, 12, 5), datetime=datetime(2024, 12, 5, 12),
        create_date=datetime(2024, 12, 4, 12), seance_length=3600, attendance=1,
    ))
    await async_session.commit()

    december = {'start_date': '2024-12-01', 'end_date': '2024-12-31'}
    response = await http.get('/dashboard/staff', params={'company_id': 1, **december})
    assert 'Left desk' not in {row['name'] for row in response.json()['data']}


@pytest.mark.asyncio
async def test_fired_staff_months_are_asked_in_batches_over_the_whole_calendar(async_session, january_data):
    import dashboard_service

    async_session.add(Staff(id=5, name='Left desk', position='Администратор', company_id=1, fired=1, user_id=901))
    async_session.add(StaffSchedule(
        staff_id=5, company_id=1, date=date(2050, 6, 5), slot_from=time(9), slot_to=time(18),
    ))
    await async_session.commit()
    months = dashboard_service._month_slices(date(2000, 1, 1), date(2099, 12, 31))

    # More branch ids than one query could bind for 1200 months (SQLite and asyncpg both cap parameters near 32 k).
    active = await dashboard_service._fired_staff_active_months(async_session, months, list(range(1, 21)), [5])

    assert [months[index][0] for index in active[(1, 5)]] == [date(2050, 6, 1)]
