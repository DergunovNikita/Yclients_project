"""Regression tests for the second audit pass over dashboard_service (worker 1)."""

from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

import api
import dashboard_routes
import dashboard_service
from api import app
from auth_scope import AccessContext
from models import (
    Appointment,
    Company,
    FinancialTransaction,
    Group,
    Staff,
    Transaction,
)


async def _seed_two_barbers(session):
    session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Own', position='Барбер', company_id=1, fired=0, portal_user_id=7),
        Staff(id=2, name='Colleague', position='Барбер', company_id=1, fired=0),
    ])
    await session.flush()
    for staff_id in (1, 2):
        session.add_all([
            Appointment(id=staff_id, company_id=1, staff_id=staff_id, client_id=staff_id,
                        date=date(2025, 1, 10), attendance=1),
            Transaction(id=staff_id, appointment_id=staff_id, service_id=10, service_title='Cut',
                        amount=1, company_id=1),
            FinancialTransaction(id=staff_id, company_id=1, record_id=staff_id, sold_item_id=10,
                                 sold_item_type='service', master_id=staff_id,
                                 date=datetime(2025, 1, 10, 12, 0), amount=1000.0 * staff_id),
        ])
    await session.commit()


async def _plan_fact(session, ctx):
    async def override_db():
        yield session

    async def override_access():
        return ctx

    app.dependency_overrides[api.get_async_db] = override_db
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = override_access
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.get(
                '/dashboard/widget/plan_fact',
                params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['barber', 'admin'])
async def test_plan_fact_of_a_personal_role_carries_no_branch_totals(async_session, role):
    await _seed_two_barbers(async_session)
    ctx = AccessContext.from_user(
        user_id=7, role=role, portal_account_id=1, company_ids=[1], staff_id=1, staff_keys=((1, 1),)
    )

    response = await _plan_fact(async_session, ctx)

    assert response.status_code == 200
    data = response.json()['data']
    assert 'parent_group' not in data
    assert data['diagnostics'] == []
    assert [group['staff_id'] for group in data['groups']] == [1]
    assert data['selected_staff_plan']['staff_id'] == 1
    assert 'Colleague' not in response.text


@pytest.mark.asyncio
async def test_plan_fact_of_a_branch_role_keeps_the_branch_row(async_session):
    await _seed_two_barbers(async_session)
    ctx = AccessContext.from_user(user_id=8, role='manager', portal_account_id=1, company_ids=[1])

    for params_staff in (None, 1):
        async def override_db():
            yield async_session

        async def override_access():
            return ctx

        app.dependency_overrides[api.get_async_db] = override_db
        app.dependency_overrides[dashboard_routes.get_dashboard_access] = override_access
        try:
            params = {'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1}
            if params_staff is not None:
                params['staff_id'] = params_staff
            async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
                response = await client.get('/dashboard/widget/plan_fact', params=params)
        finally:
            app.dependency_overrides.clear()
        assert response.status_code == 200
        assert response.json()['data']['parent_group']['scope'] == 'branch'


@pytest.mark.asyncio
@pytest.mark.parametrize('preset', [None, 'month'])
async def test_client_mix_metrics_match_the_overview_without_its_other_paths(async_session, preset):
    from test_dashboard_service_w3 import NOW, _seed_visits, _Statements

    await _seed_visits(async_session)
    async_session.add_all([
        Appointment(id=10, company_id=1, staff_id=1, client_id=1, date=date(2024, 12, 20),
                    datetime=datetime(2024, 12, 20, 12, 0), attendance=1),
        Appointment(id=11, company_id=1, staff_id=1, client_id=3, date=date(2024, 12, 21),
                    datetime=datetime(2024, 12, 21, 12, 0), attendance=1),
    ])
    await async_session.commit()
    window = (date(2025, 1, 1), date(2025, 1, 31))

    summary = await dashboard_service.fetch_summary(
        async_session, *window, 1, factual_at=NOW, period_preset=preset, include_appointments_breakdown=False
    )
    async_session.info.clear()
    with _Statements(async_session) as statements:
        mix = await dashboard_service.fetch_client_mix_metrics(
            async_session, *window, 1, factual_at=NOW, period_preset=preset
        )

    assert mix and all(summary['visit_metrics'][key] == value for key, value in mix.items())
    assert mix['unique_clients'] == 2 and mix['new_clients'] == 1
    for untouched in ('financial_transactions', 'transactions', 'sync_source_states', 'staff_schedules'):
        assert statements.matching(f'from {untouched}') == [], untouched


@pytest.mark.asyncio
async def test_summary_without_the_breakdown_runs_no_breakdown_query(async_session):
    from test_dashboard_service_w3 import NOW, _seed_visits, _Statements

    await _seed_visits(async_session)

    with _Statements(async_session) as statements:
        summary = await dashboard_service.fetch_summary(
            async_session, date(2025, 1, 1), date(2025, 1, 31), 1, factual_at=NOW,
            include_appointments_breakdown=False,
        )

    assert summary['appointments_breakdown'] is None
    assert statements.matching('as cancelled') == []


@pytest.mark.asyncio
async def test_summary_with_the_breakdown_still_reads_it(async_session):
    from test_dashboard_service_w3 import NOW, _seed_visits, _Statements

    await _seed_visits(async_session)

    with _Statements(async_session) as statements:
        summary = await dashboard_service.fetch_summary(
            async_session, date(2025, 1, 1), date(2025, 1, 31), 1, factual_at=NOW
        )

    assert summary['appointments_breakdown']['total'] == 3
    assert statements.matching('as cancelled')
