"""Regression tests for the W3-D1 pass over dashboard_service: the extra-services fold, the fewer queries
behind `fetch_summary` and the role periods, the placeholder-staff cut of administrator clients, and the
baseline of a window at the start of the calendar."""

from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select

import api
import dashboard_service
from api import app
from models import (
    AccountCatalog,
    Appointment,
    Client,
    Company,
    FinancialTransaction,
    Group,
    PlanStaffInput,
    ServiceCatalog,
    ServiceLabel,
    Staff,
    SyncSourceState,
    Transaction,
)

NOW = datetime(2025, 3, 1, 12, 0)


class _Statements:
    """Collects the SQL a block of awaited calls sends to the test engine."""

    def __init__(self, session):
        self.engine = session.bind.sync_engine
        self.sql: list[str] = []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.sql.append(' '.join(statement.split()).lower())

    def __enter__(self):
        event.listen(self.engine, 'before_cursor_execute', self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self.engine, 'before_cursor_execute', self._record)

    def matching(self, *needles: str) -> list[str]:
        return [sql for sql in self.sql if all(needle in sql for needle in needles)]


async def _seed_services(session):
    session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        AccountCatalog(company_id=1, account_id=1, title='Наличные', updated_at=datetime(2025, 1, 1)),
    ])
    await session.flush()
    session.add(Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 1, 10), attendance=1))
    for sid, qty in ((10, 1), (11, 3), (12, 5)):
        session.add(Transaction(
            id=sid, appointment_id=1, service_id=sid, service_title=f'S{sid}', amount=qty, company_id=1
        ))
    for sid, amount in ((10, 100.0), (11, 50.0), (12, 500.0), (13, 70.0)):
        session.add(ServiceCatalog(
            company_id=1, service_id=sid, title=f'S{sid}', updated_at=datetime(2025, 1, 1)
        ))
        session.add(FinancialTransaction(
            id=sid, company_id=1, account_id=1, record_id=1, sold_item_id=sid, sold_item_type='service',
            master_id=1, date=datetime(2025, 1, 10, 12, 0), amount=amount,
        ))
    for sid in (10, 11, 13):
        session.add(ServiceLabel(
            company_id=1, service_id=sid, is_extra=True, source='test', updated_at=datetime(2025, 1, 1)
        ))
    await session.commit()


@pytest.mark.asyncio
async def test_only_extra_lists_counted_extra_services_by_units_first(async_session):
    await _seed_services(async_session)
    window = (date(2025, 1, 1), date(2025, 1, 31))

    plain = await dashboard_service.fetch_top_services(async_session, *window, company_id=1, limit=None)
    extra = await dashboard_service.fetch_top_services(
        async_session, *window, company_id=1, limit=None, only_extra=True
    )

    # Plain list: revenue first, and a service that was only paid for (13) is listed.
    assert [row['title'] for row in plain] == ['S12', 'S10', 'S13', 'S11']
    # Extra list: units sold first, and only services that were counted are listed.
    assert [(row['title'], row['sold'], row['revenue']) for row in extra] == [('S11', 3, 50.0), ('S10', 1, 100.0)]
    limited = await dashboard_service.fetch_top_services(
        async_session, *window, company_id=1, limit=1, only_extra=True
    )
    assert [row['title'] for row in limited] == ['S11']


async def _seed_visits(session):
    session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='C1', company_id=1),
        Client(id=2, name='C2', company_id=1),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.PERSONAL_ACCOUNT_SOURCE,
            period_start=date(2024, 1, 1),
            period_end=date(2025, 3, 1),
        ),
    ])
    await session.flush()
    # A visit without transactions and one with two: the counts must not depend on the joins.
    for appointment_id, client_id, day in ((1, 1, 10), (2, 1, 11), (3, 2, 12)):
        session.add(Appointment(
            id=appointment_id, company_id=1, staff_id=1, client_id=client_id,
            date=date(2025, 1, day), datetime=datetime(2025, 1, day, 12, 0), attendance=1,
        ))
    for transaction_id in (1, 2):
        session.add(Transaction(
            id=transaction_id, appointment_id=3, service_id=10, service_title='S', amount=1, company_id=1
        ))
    await session.commit()


@pytest.mark.asyncio
async def test_summary_counts_visits_once_and_reads_coverage_once(async_session):
    await _seed_visits(async_session)

    with _Statements(async_session) as statements:
        summary = await dashboard_service.fetch_summary(
            async_session, date(2025, 1, 1), date(2025, 1, 31), 1,
            factual_at=NOW,
        )

    revenue = summary['revenue']
    assert (revenue['appointments'], revenue['unique_clients']) == (3, 2)
    assert summary['source_status'] == 'ready'
    # The period-independent coverage read is shared by the current and the previous window.
    assert len(statements.matching('from sync_source_states', 'sync_source_states.source')) == 1
    # Visits and clients are counted by the average-check block alone: the join over transactions and
    # service labels only feeds the service and extra-service counts.
    assert statements.matching('service_labels', 'count(distinct appointments.id)') == []
    assert statements.matching('service_labels', 'count(distinct appointments.client_id)') == []


@pytest.mark.asyncio
async def test_average_check_block_accepts_the_shared_coverage(async_session):
    await _seed_visits(async_session)
    window = dashboard_service.DateRange(date(2025, 1, 1), date(2025, 1, 31))
    # Coverage that stops before the period must read as partial, whatever the table says.
    short = {1: (date(2025, 1, 1), date(2025, 1, 5))}

    own = await dashboard_service._average_check_block(async_session, window, 1, company_ids=[1], factual_at=NOW)
    shared = await dashboard_service._average_check_block(
        async_session, window, 1, company_ids=[1], factual_at=NOW, covered_ranges=short
    )

    assert own['source_status'] == 'ready'
    assert shared['source_status'] == 'partial'


async def _seed_branches(session, branch_ids=(1, 2, 3)):
    session.add(Group(id=1, title='G1'))
    session.add_all(Company(id=cid, title=f'B{cid}', group_id=1) for cid in branch_ids)
    await session.flush()
    for cid in branch_ids:
        session.add(Staff(id=cid * 10, name=f'Admin {cid}', position='Администратор', company_id=cid, fired=0))
        session.add(Staff(id=cid * 10 + 1, name=f'Barber {cid}', position='Барбер', company_id=cid, fired=0))
    await session.commit()


@pytest.mark.asyncio
async def test_administrator_role_periods_take_a_fixed_number_of_queries(async_session):
    await _seed_branches(async_session)
    window = (date(2025, 1, 1), date(2025, 1, 31))

    with _Statements(async_session) as statements:
        periods = await dashboard_service._administrator_role_periods_by_company(async_session, *window, [1, 2, 3])

    # The staff of all branches, then plan inputs and plan metrics: three queries, not 1 + 2 per branch.
    assert len(statements.sql) == 3
    assert periods == {
        cid: {cid * 10: [(date(2025, 1, 1), date(2025, 1, 31))]} for cid in (1, 2, 3)
    }


@pytest.mark.asyncio
async def test_staff_role_periods_are_remembered_per_request(async_session):
    await _seed_branches(async_session, (1,))
    window = (date(2025, 1, 1), date(2025, 1, 31))
    positions = {10: 'Администратор', 11: 'Барбер'}

    with _Statements(async_session) as statements:
        first = await dashboard_service._staff_role_periods_by_staff(async_session, *window, 1, positions)
        issued = len(statements.sql)
        again = await dashboard_service._staff_role_periods_by_staff(async_session, *window, 1, positions)
        # A new staff member is the only thing that costs queries.
        more = await dashboard_service._staff_role_periods_by_staff(
            async_session, *window, 1, {**positions, 12: 'Барбер'}
        )

    assert issued == 2
    assert len(statements.sql) == 4
    assert again == first
    assert more[12] == ([], [(date(2025, 1, 1), date(2025, 1, 31))])
    # Callers get copies, so editing a result cannot corrupt the remembered periods.
    first[10][0].clear()
    assert (await dashboard_service._staff_role_periods_by_staff(async_session, *window, 1, positions))[10][0]


@pytest.mark.asyncio
async def test_role_periods_ignore_plan_rows_filed_under_another_branch(async_session):
    """One query serves several branches, but a plan row of branch 2 must not decide a staff row of branch 1."""
    await _seed_branches(async_session, (1, 2))
    async_session.add(PlanStaffInput(
        period_start=date(2025, 1, 1), period_end=date(2025, 1, 31), company_id=2, staff_id=11,
        staff_category='administrator', updated_at=datetime(2025, 1, 1),
    ))
    await async_session.commit()

    both = await dashboard_service._staff_role_periods_by_company(
        async_session, date(2025, 1, 1), date(2025, 1, 31), {1: {11: 'Барбер'}, 2: {21: 'Барбер'}}
    )

    assert both[1][11][0] == []
    assert both[2] == {21: ([], [(date(2025, 1, 1), date(2025, 1, 31))])}


@pytest.mark.asyncio
async def test_saving_the_plan_drops_the_remembered_role_periods(async_session):
    await _seed_branches(async_session, (1,))
    window = (date(2025, 1, 1), date(2025, 1, 31))
    await dashboard_service._administrator_role_periods_by_company(async_session, *window, [1])
    await dashboard_service._staff_role_periods_by_staff(async_session, *window, 1, {10: 'Администратор'})
    assert async_session.info['staff_role_periods'] and async_session.info['administrator_role_periods']

    await dashboard_service.save_plan_settings(
        async_session,
        '2025-01',
        [{'company_id': 1, 'wax_pct': 10}],
        [{'company_id': 1, 'staff_id': 10, 'staff_category': 'administrator'}],
        actor_user_id=7,
        portal_account_id=3,
    )

    assert 'staff_role_periods' not in async_session.info
    assert 'administrator_role_periods' not in async_session.info


@pytest.mark.asyncio
async def test_administrator_clients_skip_placeholder_staffed_visits(async_session):
    """A segment without barbers has no staff clause: the branch's "clients" fact still excludes placeholder staff."""
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
        Staff(id=3, name='Администратор', position='Администратор', company_id=1),
        Staff(id=4, name='Лист ожидания', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    for appointment_id, staff_id in ((1, 1), (2, 3), (3, 4), (4, None)):
        async_session.add(Appointment(
            id=appointment_id, company_id=1, staff_id=staff_id, date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0), attendance=1,
        ))
    await async_session.commit()
    window = (date(2025, 1, 1), date(2025, 1, 31))

    async def admin_total(barber_staff_ids):
        counts = await dashboard_service._admin_clients_by_finished_appointments(
            async_session, *window, 1, [2], {2: None}, barber_staff_ids, factual_at=NOW
        )
        return sum(counts.values())

    branch_clients = await async_session.scalar(
        select(func.count(func.distinct(Appointment.id))).where(
            dashboard_service._appt_revenue_filters(*window, 1, factual_at=NOW)
        )
    )
    # Staffed by a master or by nobody: the placeholder and the waitlist rows are not visits.
    assert branch_clients == 2
    assert await admin_total(None) == branch_clients
    assert await admin_total([1]) == 1


def test_a_window_at_the_start_of_the_calendar_has_no_baseline():
    with pytest.raises(dashboard_service.PeriodOutOfRangeError):
        dashboard_service.DateRange(date(1, 1, 10), date(1, 1, 20)).previous_period('month')
    with pytest.raises(dashboard_service.PeriodOutOfRangeError):
        dashboard_service.DateRange(date(1, 2, 10), date(1, 2, 20)).previous_period('year')
    with pytest.raises(dashboard_service.PeriodOutOfRangeError):
        dashboard_service.DateRange(date(1, 1, 10), date(1, 2, 10)).previous_period()
    # A window with room before it is untouched.
    assert dashboard_service.DateRange(date(2025, 3, 1), date(2025, 3, 31)).previous_period('month') == (
        dashboard_service.DateRange(date(2025, 2, 1), date(2025, 2, 28))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [
        {'start_date': '0001-01-10', 'end_date': '0001-01-20', 'period_preset': 'month'},
        {'start_date': '0001-02-10', 'end_date': '0001-02-20', 'period_preset': 'year'},
        {'start_date': '0001-01-10', 'end_date': '0001-02-10'},
    ],
)
async def test_overview_answers_400_for_a_window_without_a_baseline(async_session, params):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/dashboard/bundle', params=params)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 400
    assert response.json() == {'detail': 'the period has no previous period to compare with'}
