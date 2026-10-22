"""Regression tests for the W3-D1 pass over dashboard_service: the extra-services fold, the fewer queries
behind `fetch_summary` and the role periods, the placeholder-staff cut of administrator clients, and the
baseline of a window at the start of the calendar."""

from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

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
async def test_summary_counts_visits_and_clients_whatever_the_joins(async_session):
    await _seed_visits(async_session)

    summary = await dashboard_service.fetch_summary(
        async_session, date(2025, 1, 1), date(2025, 1, 31), 1,
        factual_at=NOW,
    )

    revenue = summary['revenue']
    assert (revenue['appointments'], revenue['unique_clients']) == (3, 2)
    assert summary['source_status'] == 'ready'


@pytest.mark.asyncio
async def test_a_synced_range_that_stops_before_the_period_makes_the_average_check_partial(async_session):
    await _seed_visits(async_session)
    window = (date(2025, 1, 1), date(2025, 1, 31))

    own = await dashboard_service.fetch_summary(async_session, *window, 1, factual_at=NOW)
    assert own['average_check']['source_status'] == 'ready'

    state = (await async_session.execute(select(SyncSourceState))).scalar_one()
    state.period_end = date(2025, 1, 5)
    await async_session.commit()
    async_session.info.clear()
    short = await dashboard_service.fetch_summary(async_session, *window, 1, factual_at=NOW)

    assert short['average_check']['source_status'] == 'partial'


@pytest.mark.asyncio
async def test_a_branch_outside_the_allowed_set_matches_nothing(async_session):
    await _seed_visits(async_session)
    window = (date(2025, 1, 1), date(2025, 1, 31))

    async def visits(company_id, allowed):
        async_session.info.clear()
        summary = await dashboard_service.fetch_summary(
            async_session, *window, company_id, factual_at=NOW, allowed_company_ids=allowed
        )
        return summary['revenue']['appointments']

    assert await visits(None, None) == 3
    assert await visits(None, [1]) == 3
    assert await visits(None, []) == 0
    assert await visits(1, [2]) == 0


async def _seed_branches(session, branch_ids=(1, 2, 3)):
    session.add(Group(id=1, title='G1'))
    session.add_all(Company(id=cid, title=f'B{cid}', group_id=1) for cid in branch_ids)
    await session.flush()
    for cid in branch_ids:
        session.add(Staff(id=cid * 10, name=f'Admin {cid}', position='Администратор', company_id=cid, fired=0))
        session.add(Staff(id=cid * 10 + 1, name=f'Barber {cid}', position='Барбер', company_id=cid, fired=0))
    await session.commit()


@pytest.mark.asyncio
async def test_administrator_role_periods_cost_the_same_queries_for_one_branch_or_many(async_session):
    await _seed_branches(async_session)
    window = (date(2025, 1, 1), date(2025, 1, 31))

    async def statements_for(company_ids):
        async_session.info.clear()
        with _Statements(async_session) as statements:
            await dashboard_service._administrator_role_periods_by_company(async_session, *window, company_ids)
        return len(statements.sql)

    assert await statements_for([1]) == await statements_for([1, 2, 3])


@pytest.mark.asyncio
async def test_role_periods_ignore_plan_rows_filed_under_another_branch(async_session):
    """One query serves several branches, but a plan row of branch 2 must not decide a staff row of branch 1."""
    await _seed_branches(async_session, (1, 2))
    async_session.add(PlanStaffInput(
        period_start=date(2025, 1, 1), period_end=date(2025, 1, 31), company_id=2, staff_id=11,
        staff_category='administrator', updated_at=datetime(2025, 1, 1),
    ))
    await async_session.commit()

    listed = await dashboard_service.fetch_staff(async_session, start=date(2025, 1, 1), end=date(2025, 1, 31))

    assert {row['id']: row['category'] for row in listed} == {
        10: 'administrator', 11: 'barber', 20: 'administrator', 21: 'barber',
    }


@pytest.mark.asyncio
async def test_administrator_clients_skip_placeholder_staffed_visits(async_session):
    """A branch without barbers has no staff clause: the "clients" fact still excludes placeholder staff."""
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
        Staff(id=3, name='Администратор', position='Администратор', company_id=1),
        Staff(id=4, name='Лист ожидания', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    # Staffed by nobody, by the placeholder, by the waitlist row, and by nobody again.
    for appointment_id, staff_id in ((1, None), (2, 3), (3, 4), (4, None)):
        async_session.add(Appointment(
            id=appointment_id, company_id=1, staff_id=staff_id, client_id=appointment_id, date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0), attendance=1,
        ))
    await async_session.commit()

    payload = await dashboard_service.fetch_plan_fact(
        async_session, date(2025, 1, 1), date(2025, 1, 31), company_id=1, factual_at=NOW
    )

    def clients(group):
        return next(cell['fact'] for cell in group['metrics'] if cell['code'] == 'clients')

    admin = next(group for group in payload['groups'] if group['staff_id'] == 2)
    assert clients(admin) == 2.0
    assert clients(payload['parent_group']) == 2.0


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
async def test_overview_answers_400_for_a_window_without_a_baseline(async_session):
    params = {'start_date': '0001-01-10', 'end_date': '0001-01-20', 'period_preset': 'month'}

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
