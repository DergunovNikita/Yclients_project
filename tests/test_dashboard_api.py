"""Dashboard JSON API (product portal metrics)."""

from calendar import monthrange
from datetime import UTC, date, datetime, time, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select

import api
import auth_deps
import config
import dashboard_reports
import dashboard_routes
import dashboard_service
import sync_pipeline
from auth_scope import AccessContext
from auth_service import create_access_token, hash_password
from dashboard_service import _staff_leaderboards_payload, fetch_plan_fact
from api import app
from models import (
    AccountCatalog,
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodCatalog,
    GoodCategoryCatalog,
    GoodTransaction,
    Group,
    ManualFactMetric,
    PortalAccount,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    PlanBranchSetting,
    PlanMetric,
    PlanStaffInput,
    Service,
    ServiceCatalog,
    ServiceKpiAssignment,
    ServiceKpiGroup,
    ServiceLabel,
    Staff,
    StaffSchedule,
    SyncSourceState,
    Transaction,
)


def _paid_service_revenue_filter_rows() -> list[FinancialTransaction]:
    return [
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=1,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=2,
            date=datetime(2025, 1, 11, 12, 0, 0),
            amount=500.0,
            record_id=2,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=3,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=600.0,
            record_id=1,
            sold_item_type='goods_transaction',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=4,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=-200.0,
            record_id=1,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=5,
            date=datetime(2025, 2, 1, 12, 0, 0),
            amount=300.0,
            record_id=1,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
    ]


@pytest.mark.asyncio
async def test_dashboard_reports_registry_contract(async_session):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get('/dashboard/reports')
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert len(data) == 61
    by_id = {item['id']: item for item in data}
    assert by_id['revenue_dynamics']['status'] == 'ready'
    assert by_id['conversion_funnel']['status'] == 'source_missing'
    assert by_id['nps_dashboard']['status'] == 'partial'
    assert by_id['revenue_dynamics']['filters']['compare'] is True
    assert by_id['year_over_year']['status'] == 'ready'
    assert by_id['year_over_year']['group'] == 'finance'
    assert by_id['year_over_year']['filters']['date_range'] is False
    assert by_id['new_vs_returning_cross']['status'] == 'ready'
    assert by_id['new_vs_returning_cross']['group'] == 'clients'
    assert by_id['staff_leaderboard']['status'] == 'ready'
    assert by_id['staff_leaderboard']['group'] == 'team'
    # Compare is offered only for dynamics/aggregate reports, not rankings or plan duplicates.
    assert by_id['staff_leaderboard']['filters']['compare'] is False
    assert by_id['top_goods_revenue']['filters']['compare'] is False
    assert 'plan_execution' not in by_id
    assert 'masters_rating' not in by_id
    assert not any(report_id.startswith('milena_') for report_id in by_id)


@pytest.mark.asyncio
async def test_dashboard_report_data_ready_report_and_compare(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client', phone='+100', company_id=1))
    async_session.add(Service(id=10, title='Cut', company_id=1))
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 12, 10),
            datetime=datetime(2024, 12, 10, 12, 0, 0),
            attendance=1,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Cut', amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=10, service_title='Cut', amount=1, company_id=1),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1200.0,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=2,
            date=datetime(2024, 12, 10, 12, 0, 0),
            amount=800.0,
            record_id=2,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'revenue_dynamics',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'granularity': 'month',
                'compare_start_date': '2024-12-01',
                'compare_end_date': '2024-12-31',
            },
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['source_status'] == 'ready'
    assert data['average_check_source_status'] == 'partial'
    assert data['report_id'] == 'revenue_dynamics'
    assert data['cards'][0]['value'] == 1200.0
    assert data['comparison']['cards'][0]['value'] == 800.0
    assert data['comparison']['rows'][0]['label'] == 'Выручка'
    assert data['comparison']['rows'][0]['current'] == 1200.0
    assert data['comparison']['rows'][0]['compare'] == 800.0
    assert data['comparison']['rows'][0]['delta'] == 400.0
    assert data['comparison']['rows'][0]['delta_pct'] == 50.0
    assert data['charts']
    assert data['tables']


@pytest.mark.asyncio
async def test_dashboard_client_reports_are_aggregated_without_client_pii(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([
        Client(id=1, name='Alice Personal', phone='+100000001', company_id=1),
        Client(id=2, name='Bob Personal', phone='+100000002', company_id=1),
        Client(id=3, name='Carol Personal', phone='+100000003', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2024, 12, 20), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2025, 1, 11), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=3, date=date(2024, 6, 1), attendance=1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        recency = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'new_vs_returning_cross',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
            },
        )
        churn = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'lost_clients_list',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
            },
        )
    app.dependency_overrides.clear()

    assert recency.status_code == 200
    recency_data = recency.json()['data']
    assert recency_data['source_status'] == 'ready'
    assert recency_data['cards'][1]['value'] == 1
    assert recency_data['cards'][3]['value'] == 1

    assert churn.status_code == 200
    assert churn.json()['data']['tables'][0]['id'] == 'risk_segments'

    combined_payload = recency.text + churn.text
    assert 'Alice Personal' not in combined_payload
    assert 'Bob Personal' not in combined_payload
    assert 'Carol Personal' not in combined_payload
    assert '+100000001' not in combined_payload
    assert '+100000002' not in combined_payload
    assert '+100000003' not in combined_payload


@pytest.mark.asyncio
async def test_dashboard_client_reports_revenue_uses_paid_service_rows(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([
        Client(id=1, name='Paying Client', company_id=1),
        Client(id=2, name='No Payment Client', company_id=1),
        Client(id=3, name='Payment Only Client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 11), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2025, 1, 12), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=3, date=date(2024, 12, 20), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        *(
            row
            for row in _paid_service_revenue_filter_rows()
            if row.sold_item_type != 'goods_transaction'
        ),
        AccountCatalog(
            company_id=1,
            account_id=99,
            title='Бонусный счет',
            updated_at=datetime(2025, 1, 1),
        ),
        FinancialTransaction(
            id=6,
            date=datetime(2025, 1, 20, 12),
            amount=700.0,
            record_id=4,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=7,
            date=datetime(2025, 1, 20, 13),
            amount=9000.0,
            record_id=1,
            sold_item_type='service',
            master_id=1,
            account_id=99,
            company_id=1,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'top_clients_pareto', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    card_values = [card['value'] for card in data['cards']]
    assert card_values[:3] == [3, 3, 2200.0]
    assert card_values[3] == pytest.approx(2200.0 / 3)
    # Buckets count the client's whole history at the branch, not the period, so the
    # client whose only visit predates the window lands in "1 визит" and carries its
    # in-period revenue there. "0 визитов" is left for clients who never attended.
    frequency_by_bucket = {row['bucket']: row for row in data['raw']['visit_frequency']}
    assert frequency_by_bucket['0 визитов']['clients'] == 0
    assert frequency_by_bucket['0 визитов']['revenue'] == 0.0
    assert frequency_by_bucket['1 визит']['clients'] == 2
    assert frequency_by_bucket['1 визит']['revenue'] == 700.0
    assert frequency_by_bucket['2-3 визита']['clients'] == 1
    assert frequency_by_bucket['2-3 визита']['revenue'] == 1500.0

    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    plan_fact = await dashboard_service._fact_metric_components(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        1,
    )
    assert data['cards'][2]['value'] == overview['revenue']['service_revenue'] == 2200.0
    assert data['cards'][2]['value'] == plan_fact['revenue']


@pytest.mark.asyncio
async def test_dashboard_year_over_year_uses_actual_history_and_overview_formulas(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([Client(id=value, name=f'Client {value}', company_id=1) for value in range(1, 9)])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2023, 5, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2023, 6, 11), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=3, date=date(2024, 1, 10), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=4, date=date(2024, 12, 11), attendance=1),
        Appointment(id=5, company_id=1, staff_id=1, client_id=5, date=date(2025, 1, 10), attendance=1),
        Appointment(id=6, company_id=1, staff_id=1, client_id=6, date=date(2025, 8, 12), attendance=1),
        # Future visits, including one later today, are not factual boundaries.
        Appointment(id=7, company_id=1, staff_id=1, client_id=7, date=date(2027, 1, 10), attendance=0),
        Appointment(id=8, company_id=1, staff_id=1, client_id=8, date=date(2026, 8, 1), datetime=datetime(2026, 8, 1, 18, 0), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2023, 5, 10, 12, 0), amount=1000.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2023, 6, 11, 12, 0), amount=1200.0, record_id=2, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=3, date=datetime(2023, 5, 15, 12, 0), amount=100.0, expense_title='Пополнение личного счета', sold_item_type='personal_account', master_id=1, company_id=1),
        FinancialTransaction(id=4, date=datetime(2024, 1, 10, 12, 0), amount=1500.0, record_id=3, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=5, date=datetime(2024, 12, 11, 12, 0), amount=2000.0, record_id=4, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=6, date=datetime(2024, 2, 10, 12, 0), amount=400.0, sold_item_id=10, sold_item_type='goods_transaction', master_id=1, company_id=1),
        FinancialTransaction(id=7, date=datetime(2025, 1, 10, 12, 0), amount=2500.0, record_id=5, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=8, date=datetime(2025, 8, 12, 12, 0), amount=3000.0, record_id=6, sold_item_type='service', master_id=1, company_id=1),
        # A revenue fact after the last visit must advance actual latest_year.
        FinancialTransaction(id=9, date=datetime(2026, 7, 20, 12, 0), amount=500.0, expense_title='Пополнение личного счета', sold_item_type='personal_account', master_id=1, company_id=1),
        FinancialTransaction(id=10, date=datetime(2026, 8, 1, 10, 0), amount=200.0, expense_title='Пополнение личного счета', sold_item_type='personal_account', master_id=1, company_id=1),
        FinancialTransaction(id=11, date=datetime(2026, 8, 1, 18, 0), amount=999.0, expense_title='Пополнение личного счета', sold_item_type='personal_account', master_id=1, company_id=1),
        SyncSourceState(company_id=1, source='appointments_detail', period_start=date(2023, 1, 1), period_end=date(2026, 12, 31), synced_at=datetime(2026, 8, 1)),
        SyncSourceState(company_id=1, source='financial_transactions_detail', period_start=date(2023, 1, 1), period_end=date(2026, 12, 31), synced_at=datetime(2026, 8, 1)),
        SyncSourceState(company_id=1, source='goods_transactions_detail', period_start=date(2023, 1, 1), period_end=date(2026, 12, 31), synced_at=datetime(2026, 8, 1)),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    statement_count = 0

    def count_statements(*_args):
        nonlocal statement_count
        statement_count += 1

    event.listen(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
    try:
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            r = await client.get(
                '/dashboard/reports/data',
                params={
                    'report_id': 'year_over_year',
                    'start_date': '2025-01-01',
                    'end_date': '2025-01-31',
                    'company_id': 1,
                },
            )
    finally:
        event.remove(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['report_id'] == 'year_over_year'
    assert statement_count <= 16
    assert data['source_status'] == 'ready'
    assert data['raw']['service_detail_excluded'] is True
    assert [row['year'] for row in data['raw']['years']] == [2023, 2024, 2025, 2026]
    assert data['raw']['activity_start'] == '2023-05-10'
    assert data['raw']['activity_end'] == '2026-08-01'
    assert data['raw']['latest_year'] == 2026
    assert data['raw']['months_in_scope'] == [f'{month:02d}' for month in range(1, 13)]
    years_by_year = {row['year']: row for row in data['raw']['years']}
    assert years_by_year[2023]['period_start'] == '2023-05-10'
    assert years_by_year[2023]['is_partial_year'] is True
    assert years_by_year[2023]['revenue'] == 2300.0
    assert years_by_year[2024]['is_partial_year'] is False
    assert years_by_year[2024]['revenue'] == 3900.0
    assert years_by_year[2024]['appointments'] == 2
    assert years_by_year[2024]['avg_check'] == 1950.0
    assert years_by_year[2025]['period_end'] == '2025-12-31'
    assert years_by_year[2025]['is_partial_year'] is False
    assert years_by_year[2025]['revenue'] == 5500.0
    assert years_by_year[2025]['appointments'] == 2
    assert years_by_year[2025]['avg_check'] == 2750.0
    assert years_by_year[2025]['revenue_change_pct'] == 41.03
    assert years_by_year[2025]['comparison_status'] == 'comparable'
    assert years_by_year[2026]['period_end'] == '2026-08-01'
    assert years_by_year[2026]['is_partial_year'] is True
    assert years_by_year[2026]['revenue'] == 700.0
    assert years_by_year[2026]['appointments'] == 0
    assert years_by_year[2026]['comparison_status'] == 'different_period'
    assert {table['id'] for table in data['tables']} == {'years', 'months'}
    monthly_chart = next(chart for chart in data['charts'] if chart['id'] == 'monthly_revenue_yoy')
    assert monthly_chart['labels'] == [f'{month:02d}' for month in range(1, 13)]
    assert [dataset['label'] for dataset in monthly_chart['datasets']] == ['2023', '2024', '2025', '2026']
    datasets = {dataset['label']: dataset['data'] for dataset in monthly_chart['datasets']}
    assert datasets['2023'][:4] == [None, None, None, None]
    assert datasets['2023'][4:6] == [1100.0, 1200.0]
    assert datasets['2024'][0:2] == [1500.0, 400.0]
    assert datasets['2024'][11] == 2000.0
    assert datasets['2025'][0] == 2500.0
    assert datasets['2025'][7] == 3000.0
    assert datasets['2025'][8:] == [0.0, 0.0, 0.0, 0.0]
    assert datasets['2026'][:6] == [0.0] * 6
    assert datasets['2026'][6] == 500.0
    assert datasets['2026'][7] == 200.0
    assert datasets['2026'][8:] == [None, None, None, None]
    for year, expected in ((2023, 2300.0), (2024, 3900.0), (2025, 5500.0), (2026, 700.0)):
        assert sum(value or 0 for value in datasets[str(year)]) == expected

    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    plan_fact_components = await dashboard_service._fact_metric_components(
        async_session, date(2025, 1, 1), date(2025, 12, 31), 1
    )
    assert years_by_year[2025]['revenue'] == overview['revenue']['total']
    assert years_by_year[2025]['revenue'] == plan_fact_components['revenue']
    assert years_by_year[2025]['appointments'] == overview['revenue']['appointments']
    assert years_by_year[2025]['appointments'] == plan_fact_components['avg_check_denominator']


@pytest.mark.asyncio
async def test_year_over_year_charts_render_values_after_full_historical_sync(
    async_session,
    monkeypatch,
):
    """Charts must carry numbers, not an all-None series, once a full sync certifies coverage.

    The coverage window is derived from sync_pipeline rather than hardcoded, so a change
    that stops a full sync from certifying the history fails here instead of silently
    rendering empty charts.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([Client(id=value, name=f'Client {value}', company_id=1) for value in range(1, 5)])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2023, 5, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2024, 6, 11), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=3, date=date(2025, 7, 12), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=4, date=date(2026, 2, 13), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2023, 5, 10, 12, 0), amount=1000.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2024, 6, 11, 12, 0), amount=2000.0, record_id=2, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 7, 12, 12, 0), amount=3000.0, record_id=3, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=4, date=datetime(2026, 2, 13, 12, 0), amount=4000.0, record_id=4, sold_item_type='service', master_id=1, company_id=1),
    ])

    # Exactly what execute_sync() persists after one forced full historical pass.
    sync_end = report_now.date()
    sync_start = sync_pipeline.historical_sync_start_date(sync_end)
    schedule_end = sync_end + timedelta(days=config.SCHEDULE_DAYS)
    async_session.add_all([
        SyncSourceState(company_id=1, source='appointments_detail', period_start=sync_start, period_end=schedule_end, synced_at=report_now),
        SyncSourceState(company_id=1, source='financial_transactions_detail', period_start=sync_start, period_end=sync_end, synced_at=report_now),
        SyncSourceState(company_id=1, source='goods_transactions_detail', period_start=sync_start, period_end=sync_end, synced_at=report_now),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/reports/data',
            # year_over_year ignores the range, but the endpoint still requires it
            # and the frontend keeps sending it from its hidden date inputs.
            params={
                'report_id': 'year_over_year',
                'start_date': '2026-07-01',
                'end_date': '2026-07-31',
                'company_id': 1,
            },
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['source_status'] == 'ready'
    assert data['missing_sources'] == []

    charts = {chart['id']: chart for chart in data['charts']}
    assert 'year_revenue' in charts

    revenue_series = charts['year_revenue']['datasets'][0]['data']
    assert None not in revenue_series
    assert revenue_series == [1000.0, 2000.0, 3000.0, 4000.0]

    appointments_series = charts['year_appointments']['datasets'][0]['data']
    assert None not in appointments_series
    assert appointments_series == [1, 1, 1, 1]

    avg_check_series = charts['year_avg_check']['datasets'][0]['data']
    assert avg_check_series == [1000.0, 2000.0, 3000.0, 4000.0]

    monthly_avg_check = {
        dataset['label']: dataset['data']
        for dataset in charts['monthly_avg_check_yoy']['datasets']
    }
    assert monthly_avg_check['2023'][4] == 1000.0
    assert monthly_avg_check['2024'][5] == 2000.0
    assert monthly_avg_check['2025'][6] == 3000.0
    assert monthly_avg_check['2026'][1] == 4000.0
    # A month without completed visits has no average check rather than a zero one.
    assert monthly_avg_check['2025'][0] is None

    # This tenant has no personal-account top-ups, so the permanently zero column
    # is dropped rather than shown as a wall of nulls.
    for table_id in ('years', 'months'):
        table = next(item for item in data['tables'] if item['id'] == table_id)
        assert 'topup_revenue' not in {column['key'] for column in table['columns']}
        assert 'revenue' in {column['key'] for column in table['columns']}

    # Every chart must plot at least one real point; an all-None series reads as an empty chart.
    for chart_id, chart in charts.items():
        for dataset in chart['datasets']:
            assert any(
                value is not None for value in dataset['data']
            ), f'chart {chart_id} dataset {dataset.get("label")!r} has no plottable value'


@pytest.mark.asyncio
async def test_year_over_year_current_december_31_remains_partial(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 12, 31, 12, 0),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 1),
            datetime=datetime(2025, 1, 1, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            date=date(2025, 12, 31),
            datetime=datetime(2025, 12, 31, 10),
            attendance=1,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            date=date(2026, 1, 1),
            datetime=datetime(2026, 1, 1, 10),
            attendance=1,
        ),
        Appointment(
            id=4,
            company_id=1,
            staff_id=1,
            date=date(2026, 12, 31),
            datetime=datetime(2026, 12, 31, 10),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_type='service',
            date=datetime(2025, 1, 1, 10),
            amount=100.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=1,
            record_id=3,
            sold_item_type='service',
            date=datetime(2026, 1, 1, 10),
            amount=200.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2025, 1, 1),
            period_end=date(2026, 12, 31),
            synced_at=datetime(2026, 12, 31, 12),
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 12, 1),
        date(2026, 12, 31),
        allowed_company_ids=[1],
    )
    years = {row['year']: row for row in report['raw']['years']}

    assert years[2025]['is_partial_year'] is False
    assert years[2026]['is_partial_year'] is True
    assert years[2026]['period_status'] == 'Неполный'
    assert years[2026]['comparison_status'] == 'different_period'
    assert years[2026]['revenue_change_pct'] is None


@pytest.mark.asyncio
async def test_dashboard_year_over_year_empty_scope_returns_empty_actual_history(async_session):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'year_over_year',
                'start_date': '2024-12-01',
                'end_date': '2025-01-31',
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['source_status'] == 'partial'
    assert data['raw']['years'] == []
    assert data['raw']['months'] == []
    assert data['raw']['latest_year'] is None


def _yoy_source_states(company_id: int, period_start: date, period_end: date, synced_at: datetime):
    return [
        SyncSourceState(
            company_id=company_id,
            source=source,
            period_start=period_start,
            period_end=period_end,
            synced_at=synced_at,
        )
        for source in (
            'appointments_detail',
            'financial_transactions_detail',
            'goods_transactions_detail',
        )
    ]


@pytest.mark.asyncio
async def test_reporting_start_keeps_overview_and_year_over_year_in_agreement(
    async_session,
    monkeypatch,
):
    """The report claims one formula with Обзор and План/факт, so the cutoff must reach both.

    A cutoff applied only inside the year-over-year report would make the opening year
    contradict the main dashboard by whatever the pre-opening records sum to.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 4, 30), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, date=date(2025, 6, 10), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2025, 4, 30, 12), amount=800.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 6, 10, 12), amount=700.0, record_id=2, sold_item_type='service', master_id=1, company_id=1),
    ])
    async_session.add_all(
        _yoy_source_states(1, date(2023, 1, 1), report_now.date(), report_now)
    )
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    year_2025 = next(row for row in report['raw']['years'] if row['year'] == 2025)

    assert summary['revenue']['total'] == 700.0
    assert summary['revenue']['appointments'] == 1
    assert year_2025['revenue'] == summary['revenue']['total']
    assert year_2025['appointments'] == summary['revenue']['appointments']
    assert year_2025['avg_check'] == summary['average_check']['total']


@pytest.mark.asyncio
async def test_reporting_start_keeps_topup_kpi_and_trend_chart_in_agreement(
    async_session,
    monkeypatch,
):
    """The revenue KPI and the trend chart under it come from different queries.

    Both are returned in one payload, so a cutoff missing from either one renders a
    headline number that contradicts the chart directly beneath it.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        AccountCatalog(
            company_id=1,
            account_id=7,
            title='Личный счет клиента',
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2025, 4, 30, 12), amount=5000.0, account_id=7, master_id=1, sold_item_id=1, sold_item_type='account_replenishment', company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 6, 10, 12), amount=1000.0, account_id=7, master_id=1, sold_item_id=2, sold_item_type='account_replenishment', company_id=1),
    ])
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    daily = await dashboard_service.fetch_revenue_daily(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
    )

    assert summary['revenue']['topup_revenue'] == 1000.0
    assert summary['revenue']['total'] == 1000.0
    assert sum(float(row.get('revenue') or 0) for row in daily) == summary['revenue']['total']


@pytest.mark.asyncio
async def test_reporting_start_cuts_service_revenue_paid_across_the_boundary(
    async_session,
    monkeypatch,
):
    """A service fact needs both its visit and its payment on or after the opening.

    Revenue is bucketed by payment date but the average check is divided by visits, so
    the two anchors have to agree or the paths that sum service revenue diverge.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        # Visit before the opening, settled after it — only the visit anchor drops this.
        Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 4, 20), attendance=1),
        # Visit after the opening, prepaid before it — only the payment anchor drops this.
        Appointment(id=2, company_id=1, staff_id=1, date=date(2025, 6, 10), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, date=date(2025, 7, 10), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2025, 5, 20, 12), amount=300.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 3, 15, 12), amount=500.0, record_id=2, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 7, 10, 12), amount=700.0, record_id=3, sold_item_type='service', master_id=1, company_id=1),
    ])
    async_session.add_all(
        _yoy_source_states(1, date(2023, 1, 1), report_now.date(), report_now)
    )
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    daily = await dashboard_service.fetch_revenue_daily(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
    )
    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )

    # Both boundary rows are dropped; only the fully post-opening 700 survives.
    assert summary['revenue']['total'] == 700.0
    assert sum(float(row.get('revenue') or 0) for row in daily) == 700.0
    # Visits follow the same rule, so the average check divides 700 by one visit.
    assert summary['revenue']['appointments'] == 2
    assert summary['average_check']['services'] == 350.0
    assert report['raw']['activity_start'] == '2025-06-10'
    assert [row['year'] for row in report['raw']['years']] == [2025, 2026]
    year_2025 = report['raw']['years'][0]
    assert year_2025['revenue'] == summary['revenue']['total']
    assert year_2025['source_status'] == 'ready'


@pytest.mark.asyncio
async def test_reporting_start_keeps_coverage_status_consistent_across_reports(
    async_session,
    monkeypatch,
):
    """A branch synced only from its opening must not read as degraded coverage.

    The year-over-year and Overview coverage checks are separate; if only one of them
    respects the cutoff, the same year is trustworthy in one report and partial in the other.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    async_session.add(
        Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 6, 10), attendance=1)
    )
    await async_session.flush()
    async_session.add(FinancialTransaction(
        id=1,
        date=datetime(2025, 6, 10, 12),
        amount=700.0,
        record_id=1,
        sold_item_type='service',
        master_id=1,
        company_id=1,
    ))
    # Synced only from the opening date, never from before it.
    async_session.add(SyncSourceState(
        company_id=1,
        source=dashboard_service.PERSONAL_ACCOUNT_SOURCE,
        period_start=date(2025, 5, 1),
        period_end=report_now.date(),
        synced_at=report_now,
    ))
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )

    assert summary['average_check']['source_status'] == 'ready'
    assert summary['average_check']['missing_components'] == []

    # A period the branch entirely predates is not certified either.
    assert await dashboard_service._source_coverage_status(
        async_session, date(2024, 1, 1), date(2024, 12, 31), 1, None,
    ) == ('partial', ['personal_account_topups'])

    # The clamp must not degenerate into "always ready": a gap that starts after the
    # reporting start is still a gap.
    state = (await async_session.execute(
        select(SyncSourceState).where(SyncSourceState.company_id == 1)
    )).scalars().one()
    state.period_start = date(2025, 8, 1)
    await async_session.commit()
    assert await dashboard_service._source_coverage_status(
        async_session, date(2025, 1, 1), date(2025, 12, 31), 1, None,
    ) == ('partial', ['personal_account_topups'])


@pytest.mark.asyncio
async def test_reporting_start_keeps_pre_opening_direct_revenue_out_of_years_and_bounds(
    async_session,
    monkeypatch,
):
    """Goods and top-up revenue carry no visit, so they are cut on their payment date.

    They are also what the report derives its own start from, so a missed cutoff here
    both invents whole pre-opening years and fills them with revenue.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Old branch', group_id=1),
        Company(id=2, title='New branch', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
        AccountCatalog(company_id=2, account_id=7, title='Личный счет клиента', updated_at=datetime(2025, 1, 1)),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2024, 2, 1), attendance=1),
        Appointment(id=2, company_id=2, staff_id=2, date=date(2025, 6, 10), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2024, 2, 1, 12), amount=100.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 6, 10, 12), amount=700.0, record_id=2, sold_item_type='service', master_id=2, company_id=2),
        # The older branch keeps 2024 in the report, so these pre-opening direct payments
        # of the newer branch fall inside a reported year and only the cutoff removes them.
        FinancialTransaction(id=4, date=datetime(2025, 3, 1, 12), amount=900.0, account_id=7, master_id=2, sold_item_id=4, sold_item_type='account_replenishment', company_id=2),
        FinancialTransaction(id=5, date=datetime(2025, 4, 1, 12), amount=600.0, master_id=2, sold_item_id=5, sold_item_type='goods_transaction', company_id=2),
    ])
    async_session.add(
        GoodTransaction(id=1, company_id=2, master_id=2, type_id=1, document_id=1, date=datetime(2025, 4, 1, 12), amount=3.0, cost=300.0)
    )
    for company_id in (1, 2):
        async_session.add_all(
            _yoy_source_states(company_id, date(2023, 1, 1), report_now.date(), report_now)
        )
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1, 2],
    )

    # The scope opens with the older branch, not with the newer branch's pre-opening money.
    assert report['raw']['activity_start'] == '2024-02-01'
    assert sum(row['revenue'] or 0 for row in report['raw']['years']) == 800.0
    year_2025 = next(row for row in report['raw']['years'] if row['year'] == 2025)
    assert year_2025['revenue'] == 700.0
    assert year_2025['topup_revenue'] == 0.0
    assert year_2025['goods_revenue'] == 0.0
    assert year_2025['goods_count'] == 0.0


@pytest.mark.asyncio
async def test_reporting_start_certifies_the_opening_year_of_a_branch_synced_from_it(
    async_session,
    monkeypatch,
):
    """A branch synced from exactly its opening must have its opening year certified.

    The year is only partly covered by sync, and a payment settled after the opening for
    a pre-opening visit must not widen the appointment coverage the year demands.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 4, 20), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, date=date(2025, 6, 10), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2025, 7, 5, 12), amount=400.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 6, 10, 12), amount=700.0, record_id=2, sold_item_type='service', master_id=1, company_id=1),
    ])
    # Synced from the opening date only — never from the start of 2025.
    async_session.add_all(
        _yoy_source_states(1, date(2025, 5, 1), report_now.date(), report_now)
    )
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    year_2025 = next(row for row in report['raw']['years'] if row['year'] == 2025)

    assert year_2025['source_status'] == 'ready'
    assert year_2025['missing_components'] == []
    assert year_2025['revenue'] == 700.0
    assert year_2025['appointments'] == 1

    # Pin the dependency query itself: the coverage clamp above would otherwise hide a
    # dependency window that still reaches behind the reporting start.
    facts = await dashboard_service.fetch_year_over_year_facts(
        async_session,
        date(2023, 1, 1),
        report_now.date(),
        company_id=1,
        factual_at=report_now,
    )
    dependency = facts['appointment_dependencies']['annual'].get(2025, {}).get(1)
    assert dependency is None or dependency[0] >= date(2025, 5, 1)


@pytest.mark.asyncio
async def test_reporting_start_clamps_coverage_for_a_branch_opened_mid_year(
    async_session,
    monkeypatch,
):
    """A branch opening inside a reported year is only asked for coverage from its opening.

    The year's period start comes from the older branch, so without a per-branch clamp
    the newer branch would look under-synced and blank the year for the whole tenant.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Old branch', group_id=1),
        Company(id=2, title='New branch', group_id=1, reporting_start_date=date(2025, 6, 1)),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 2, 4), attendance=1),
        Appointment(id=2, company_id=2, staff_id=2, date=date(2025, 7, 4), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2025, 2, 4, 12), amount=100.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 7, 4, 12), amount=300.0, record_id=2, sold_item_type='service', master_id=2, company_id=2),
    ])
    async_session.add_all(_yoy_source_states(1, date(2025, 1, 1), report_now.date(), report_now))
    # Branch 2 synced from its opening, which is inside the reported year.
    async_session.add_all(_yoy_source_states(2, date(2025, 6, 1), report_now.date(), report_now))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1, 2],
    )
    year_2025 = next(row for row in report['raw']['years'] if row['year'] == 2025)

    assert year_2025['source_status'] == 'ready'
    assert year_2025['missing_components'] == []
    assert year_2025['revenue'] == 400.0
    assert year_2025['appointments'] == 2


@pytest.mark.asyncio
async def test_reporting_start_trims_breakdown_cards_client_blocks_and_staff_facts(
    async_session,
    monkeypatch,
):
    """Cards and blocks rendered beside the trimmed KPIs must be trimmed as well.

    The record-count cards, the client blocks and the per-staff plan/fact revenue each
    come from their own query, so any one of them missing the cutoff shows a number that
    contradicts the panel next to it.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Before', company_id=1),
        Client(id=2, name='After', company_id=1),
        AccountCatalog(company_id=1, account_id=7, title='Личный счет клиента', updated_at=datetime(2025, 1, 1)),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 4, 20), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2025, 6, 10), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2025, 4, 20, 12), amount=900.0, account_id=7, master_id=1, sold_item_id=1, sold_item_type='account_replenishment', company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 6, 10, 12), amount=300.0, account_id=7, master_id=1, sold_item_id=2, sold_item_type='account_replenishment', company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 4, 20, 14), amount=800.0, master_id=1, sold_item_id=3, sold_item_type='goods_transaction', company_id=1),
        FinancialTransaction(id=4, date=datetime(2025, 6, 10, 14), amount=200.0, master_id=1, sold_item_id=4, sold_item_type='goods_transaction', company_id=1),
        Comment(id=1, company_id=1, master_id=1, date=datetime(2025, 4, 20, 13), rating=5.0, text='before'),
        Comment(id=2, company_id=1, master_id=1, date=datetime(2025, 6, 10, 13), rating=4.0, text='after'),
    ])
    async_session.add_all([
        GoodTransaction(id=1, company_id=1, master_id=1, type_id=1, document_id=1, date=datetime(2025, 4, 20, 15), amount=2.0, cost=200.0),
        GoodTransaction(id=2, company_id=1, master_id=1, type_id=1, document_id=2, date=datetime(2025, 6, 10, 15), amount=1.0, cost=100.0),
    ])
    await async_session.commit()

    # Upstream would happily return untrimmed counts; a period reaching behind the
    # cutoff must not use them.
    async def untrimmed_record_stats(*args, **kwargs):
        return {'total': 2, 'cancelled': 0, 'completed': 2, 'incomplete': 0}

    monkeypatch.setattr(
        dashboard_service.yclients_analytics, 'fetch_record_stats', untrimmed_record_stats
    )
    breakdown = await dashboard_service.fetch_appointments_breakdown(
        async_session, date(2025, 1, 1), date(2025, 12, 31), company_id=1, factual_at=report_now,
    )
    assert breakdown['completed'] == 1
    assert breakdown['total'] == 1
    # Once a scope has a cutoff every period of that scope counts the same way, so a
    # subset period can never report more records than the period containing it.
    subset = await dashboard_service.fetch_appointments_breakdown(
        async_session, date(2025, 6, 1), date(2025, 12, 31), company_id=1, factual_at=report_now,
    )
    assert subset['total'] <= breakdown['total']
    assert subset['completed'] <= breakdown['completed']

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    assert summary['visit_metrics']['unique_clients'] == 1
    assert summary['revenue']['topup_revenue'] == 300.0
    assert summary['revenue']['goods_revenue'] == 200.0
    assert summary['average_check']['goods_checks'] == 1
    # The client blocks are separate queries from the visit KPI above.
    assert summary['visit_metrics']['client_visit_frequency']['total_clients'] == 1
    # A client whose only visit predates the opening is not a new client of this branch.
    assert summary['visit_metrics']['new_clients'] == 1

    operations = await dashboard_reports.fetch_report_data(
        async_session,
        'bookings_dynamics',
        date(2025, 1, 1),
        date(2025, 12, 31),
        allowed_company_ids=[1],
    )
    assert sum(row['records'] for row in operations['raw']['by_staff']) == 1
    assert sum(row['records'] for row in operations['raw']['by_period']) == 1

    staff_facts = await dashboard_service._staff_fact_components_by_branch(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        1,
        [1],
        {1: 'barber'},
        {},
        {},
        {},
        {},
        {},
        [],
        factual_at=report_now,
    )
    # Plan/fact must not credit a barber with revenue the branch does not report:
    # the 200 goods sale plus the 300 top-up, never the pre-opening 800 and 900.
    assert staff_facts[1]['revenue'] == 500.0

    nps = await dashboard_reports.fetch_report_data(
        async_session,
        'nps_dashboard',
        date(2025, 1, 1),
        date(2025, 12, 31),
        allowed_company_ids=[1],
    )
    reviews = next(card for card in nps['cards'] if card['label'] == 'Отзывы YClients')
    assert reviews['value'] == 1

    # Churn counts lost clients from the same trimmed visit history, so a client whose
    # only visit predates the opening is not a client of this branch at all.
    churn = await dashboard_reports.fetch_report_data(
        async_session,
        'losses_by_staff',
        date(2025, 1, 1),
        date(2025, 12, 31),
        allowed_company_ids=[1],
    )
    assert sum(row['clients'] for row in churn['raw']['staff']) == 1

    # Goods revenue reaches plan/fact through its own helper, and admin attribution
    # counts finished appointments directly — both must see the trimmed history.
    goods_total = await dashboard_service._goods_paid_revenue_total(
        async_session,
        dashboard_service.DateRange(date(2025, 1, 1), date(2025, 12, 31)),
        1,
        factual_at=report_now,
    )
    assert goods_total == 200.0
    admin_clients = await dashboard_service._admin_clients_by_finished_appointments(
        async_session, date(2025, 1, 1), date(2025, 12, 31), 1, [1], {1: None},
        factual_at=report_now,
    )
    assert sum(admin_clients.values()) == 1


@pytest.mark.asyncio
async def test_reporting_start_excludes_pre_opening_goods_and_opz(
    async_session,
    monkeypatch,
):
    """Goods sales and OPZ rebooking anchors respect the cutoff too.

    OPZ counts a rebooking against the visit that anchors it; anchoring on an excluded
    visit would inflate opz_pct, whose denominator is the trimmed visit count.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Cut off', company_id=1),
        Client(id=2, name='Counted', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        # An OPZ event needs the rebooking created on the visit day or the day after.
        # Client 1's anchor visit sits before the cutoff, so the rebooking loses its
        # anchor; client 2 is the control that proves the fixture can produce an event.
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 4, 30), attendance=1, create_date=datetime(2025, 4, 1, 10)),
        Appointment(id=2, company_id=1, staff_id=1, client_id=1, date=date(2025, 7, 1), attendance=1, create_date=datetime(2025, 5, 1, 10)),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2025, 6, 10), attendance=1, create_date=datetime(2025, 6, 1, 10)),
        Appointment(id=4, company_id=1, staff_id=1, client_id=2, date=date(2025, 8, 1), attendance=1, create_date=datetime(2025, 6, 11, 10)),
    ])
    async_session.add_all([
        GoodTransaction(id=1, company_id=1, master_id=1, type_id=1, document_id=1, date=datetime(2025, 4, 20, 12), amount=3.0, cost=300.0),
        GoodTransaction(id=2, company_id=1, master_id=1, type_id=1, document_id=2, date=datetime(2025, 6, 20, 12), amount=2.0, cost=200.0),
    ])
    async_session.add_all(
        _yoy_source_states(1, date(2023, 1, 1), report_now.date(), report_now)
    )
    await async_session.commit()

    goods_facts = await dashboard_service.fetch_year_over_year_facts(
        async_session,
        date(2023, 1, 1),
        report_now.date(),
        company_id=1,
        factual_at=report_now,
    )
    assert goods_facts['annual'][2025]['goods_count'] == 2.0

    opz_facts = await dashboard_service.fetch_opz_year_facts(
        async_session,
        date(2023, 1, 1),
        report_now.date(),
        company_id=1,
        staff_id=None,
        factual_at=report_now,
    )
    # Only client 2's rebooking survives; client 1's anchor visit is before the cutoff.
    assert opz_facts['counts'] == {2025: 1.0}
    assert opz_facts['appointment_dependencies'][2025][1] == (date(2025, 6, 10), date(2025, 8, 1))

    goods_report = await dashboard_reports.fetch_report_data(
        async_session,
        'goods_dynamics',
        date(2025, 1, 1),
        date(2025, 12, 31),
        allowed_company_ids=[1],
    )
    units = next(card for card in goods_report['cards'] if card['label'] == 'Единиц продано')
    assert units['value'] == 2.0


@pytest.mark.asyncio
async def test_reporting_start_does_not_blank_years_a_branch_predates(
    async_session,
    monkeypatch,
):
    """A later-opened branch must not drag earlier years of the whole tenant to partial.

    The branch contributes no facts before its reporting start, so demanding sync
    coverage from before it would blank a year it simply did not exist for.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Old branch', group_id=1),
        Company(id=2, title='New branch', group_id=1, reporting_start_date=date(2025, 1, 1)),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2024, 3, 4), attendance=1),
        Appointment(id=2, company_id=2, staff_id=2, date=date(2025, 3, 4), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2024, 3, 4, 12), amount=100.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 3, 4, 12), amount=300.0, record_id=2, sold_item_type='service', master_id=2, company_id=2),
    ])
    async_session.add_all(_yoy_source_states(1, date(2024, 1, 1), report_now.date(), report_now))
    # Branch 2 opened in 2025 and is deliberately left with no sync state at all: a year
    # the branch did not exist for must not be read as missing coverage.
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1, 2],
    )
    years = {row['year']: row for row in report['raw']['years']}

    assert years[2024]['source_status'] == 'ready'
    assert years[2024]['missing_components'] == []
    assert years[2024]['revenue'] == 100.0
    assert years[2024]['appointments'] == 1


@pytest.mark.asyncio
async def test_year_over_year_reporting_start_trims_pre_opening_history(
    async_session,
    monkeypatch,
):
    """A branch carrying upstream records from before it opened starts at its opening.

    Upstream keeps bookings from a previous location on the same YClients id, so
    without the cutoff the report would open years before the branch existed.
    """
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 5, 1)),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2023, 3, 4), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, date=date(2025, 4, 30), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, date=date(2025, 6, 10), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2023, 3, 4, 12), amount=900.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 4, 30, 12), amount=800.0, record_id=2, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 6, 10, 12), amount=700.0, record_id=3, sold_item_type='service', master_id=1, company_id=1),
    ])
    async_session.add_all(
        _yoy_source_states(1, date(2023, 1, 1), report_now.date(), report_now)
    )
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )

    assert report['raw']['activity_start'] == '2025-06-10'
    assert [row['year'] for row in report['raw']['years']] == [2025, 2026]
    year_2025 = report['raw']['years'][0]
    assert year_2025['revenue'] == 700.0
    assert year_2025['appointments'] == 1
    assert year_2025['avg_check'] == 700.0

    months_2025 = {
        row['month']: row
        for row in report['raw']['months']
        if row['year'] == 2025
    }
    assert months_2025[4]['revenue'] is None
    assert months_2025[6]['revenue'] == 700.0


@pytest.mark.asyncio
async def test_year_over_year_reporting_start_cuts_each_branch_separately(
    async_session,
    monkeypatch,
):
    """In a multi-branch scope every branch is cut at its own opening, not the earliest one."""
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Old branch', group_id=1, reporting_start_date=date(2024, 1, 1)),
        Company(id=2, title='New branch', group_id=1, reporting_start_date=date(2025, 1, 1)),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2024, 3, 4), attendance=1),
        Appointment(id=2, company_id=2, staff_id=2, date=date(2024, 3, 4), attendance=1),
        Appointment(id=3, company_id=2, staff_id=2, date=date(2025, 3, 4), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(id=1, date=datetime(2024, 3, 4, 12), amount=100.0, record_id=1, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2024, 3, 4, 12), amount=500.0, record_id=2, sold_item_type='service', master_id=2, company_id=2),
        FinancialTransaction(id=3, date=datetime(2025, 3, 4, 12), amount=300.0, record_id=3, sold_item_type='service', master_id=2, company_id=2),
    ])
    for company_id in (1, 2):
        async_session.add_all(
            _yoy_source_states(company_id, date(2024, 1, 1), report_now.date(), report_now)
        )
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1, 2],
    )

    years = {row['year']: row for row in report['raw']['years']}
    assert report['raw']['activity_start'] == '2024-03-04'
    # Branch 2 opened in 2025, so its 2024 payment stays out of the shared 2024 row.
    assert years[2024]['revenue'] == 100.0
    assert years[2024]['appointments'] == 1
    assert years[2025]['revenue'] == 300.0
    assert years[2025]['appointments'] == 1


@pytest.mark.asyncio
async def test_year_over_year_without_reporting_start_keeps_full_history(
    async_session,
    monkeypatch,
):
    report_now = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: report_now)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    async_session.add(
        Appointment(id=1, company_id=1, staff_id=1, date=date(2023, 3, 4), attendance=1)
    )
    await async_session.flush()
    async_session.add(FinancialTransaction(
        id=1,
        date=datetime(2023, 3, 4, 12),
        amount=900.0,
        record_id=1,
        sold_item_type='service',
        master_id=1,
        company_id=1,
    ))
    async_session.add_all(
        _yoy_source_states(1, date(2023, 1, 1), report_now.date(), report_now)
    )
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )

    assert report['raw']['activity_start'] == '2023-03-04'
    assert report['raw']['years'][0]['year'] == 2023
    assert report['raw']['years'][0]['revenue'] == 900.0


@pytest.mark.asyncio
async def test_year_over_year_history_can_start_with_direct_revenue_only(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    fact_day = date(2021, 4, 5)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            sold_item_id=10,
            sold_item_type='goods_transaction',
            date=datetime(2021, 4, 5, 12),
            amount=250.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=fact_day,
            period_end=date(2026, 8, 1),
            synced_at=datetime(2021, 4, 5, 13),
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )

    assert report['raw']['activity_start'] == fact_day.isoformat()
    assert report['raw']['activity_end'] == fact_day.isoformat()
    assert report['raw']['latest_year'] == 2021
    assert report['raw']['years'][0]['revenue'] == 250.0
    assert report['raw']['years'][0]['goods_revenue'] == 250.0
    assert report['raw']['years'][0]['appointments'] == 0
    # No completed visits means no average check, matching the monthly rows and leaving
    # the average-check chart without a misleading zero bar.
    assert report['raw']['years'][0]['avg_check'] is None


@pytest.mark.asyncio
async def test_year_over_year_history_includes_payment_before_completed_visit(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2024, 1, 2),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2023, 12, 30, 12),
            amount=100.0,
        ),
    ])
    appointment_state = SyncSourceState(
        company_id=1,
        source='appointments_detail',
        period_start=date(2023, 12, 30),
        period_end=date(2023, 12, 31),
        synced_at=datetime(2024, 1, 2, 13),
    )
    async_session.add(appointment_state)
    for source in ('financial_transactions_detail', 'goods_transactions_detail'):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2023, 12, 30),
            period_end=date(2026, 8, 1),
            synced_at=datetime(2024, 1, 2, 13),
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    years = {row['year']: row for row in report['raw']['years']}

    assert report['raw']['activity_start'] == '2023-12-30'
    assert report['raw']['activity_end'] == '2024-01-02'
    december = next(
        row
        for row in report['raw']['months']
        if row['year'] == 2023 and row['month'] == 12
    )
    assert years[2023]['source_status'] == 'partial'
    assert years[2023]['revenue'] is None
    assert december['revenue'] is None

    appointment_state.period_end = date(2026, 8, 1)
    await async_session.commit()
    covered_report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    covered_years = {row['year']: row for row in covered_report['raw']['years']}
    assert covered_years[2023]['revenue'] == 100.0
    assert covered_years[2024]['appointments'] == 1


@pytest.mark.asyncio
async def test_year_over_year_masks_linked_direct_revenue_without_appointment_coverage(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2024, 1, 10),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=None,
            record_id=1,
            sold_item_id=10,
            sold_item_type='goods_transaction',
            date=datetime(2024, 1, 10, 12),
            amount=100.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=None,
            record_id=1,
            expense_title='Пополнение личного счета',
            sold_item_type='personal_account',
            date=datetime(2024, 1, 10, 13),
            amount=50.0,
        ),
        SyncSourceState(
            company_id=1,
            source='financial_transactions_detail',
            period_start=date(2024, 1, 10),
            period_end=date(2024, 1, 10),
            synced_at=datetime(2024, 1, 10, 14),
        ),
        SyncSourceState(
            company_id=1,
            source='goods_transactions_detail',
            period_start=date(2024, 1, 10),
            period_end=date(2024, 1, 10),
            synced_at=datetime(2024, 1, 10, 14),
        ),
    ])
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    annual = report['raw']['years'][0]
    january = next(row for row in report['raw']['months'] if row['month'] == 1)

    assert annual['source_status'] == 'partial'
    assert annual['goods_revenue'] is None
    assert annual['topup_revenue'] is None
    assert annual['revenue'] is None
    assert january['goods_revenue'] is None
    assert january['topup_revenue'] is None
    assert january['revenue'] is None


@pytest.mark.asyncio
async def test_factual_cutoff_is_shared_by_overview_plan_fact_daily_and_years(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12, 0)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Company(id=2, title='Salon 2', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2025, 1, 10, 12),
            amount=100.0,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 18),
            attendance=1,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=1,
            record_id=2,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2026, 8, 1, 10),
            amount=700.0,
        ),
        Appointment(
            id=3,
            company_id=2,
            staff_id=2,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 18),
            attendance=1,
        ),
        FinancialTransaction(
            id=3,
            company_id=2,
            master_id=None,
            record_id=3,
            sold_item_id=20,
            sold_item_type='goods_transaction',
            date=datetime(2026, 8, 1, 10),
            amount=300.0,
        ),
        FinancialTransaction(
            id=4,
            company_id=1,
            master_id=None,
            record_id=2,
            sold_item_id=30,
            sold_item_type='goods_transaction',
            date=datetime(2026, 8, 1, 10),
            amount=250.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2025, 1, 1),
            period_end=date(2026, 12, 31),
            synced_at=factual_at,
        ))
    await async_session.commit()

    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2026, 1, 1),
        date(2026, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    daily = await dashboard_service.fetch_revenue_daily(
        async_session,
        date(2026, 1, 1),
        date(2026, 12, 31),
        company_id=1,
        include_opz=False,
        factual_at=factual_at,
    )
    plan_fact = await fetch_plan_fact(
        async_session,
        date(2026, 1, 1),
        date(2026, 12, 31),
        company_id=1,
        include_all_staff_in_leaderboards=True,
        factual_at=factual_at,
    )
    network_plan_fact = await fetch_plan_fact(
        async_session,
        date(2026, 1, 1),
        date(2026, 12, 31),
        allowed_company_ids=[1, 2],
        include_all_staff_in_leaderboards=True,
        factual_at=factual_at,
    )
    year_report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        company_id=1,
    )
    parent_facts = {
        cell['code']: cell['fact']
        for cell in plan_fact['parent_group']['metrics']
    }

    assert overview['revenue']['total'] == 0.0
    assert overview['revenue']['appointments'] == 0
    assert daily == []
    assert parent_facts['revenue'] == 0.0
    assert parent_facts['clients'] == 0.0
    network_parent_facts = {
        cell['code']: cell['fact']
        for cell in next(
            group for group in network_plan_fact['groups'] if group['scope'] == 'network'
        )['metrics']
    }
    assert network_parent_facts['revenue'] == 0.0
    assert all(
        next(cell['fact'] for cell in group['metrics'] if cell['code'] == 'revenue') == 0.0
        for group in network_plan_fact['groups']
        if group['scope'] == 'branch'
    )
    assert year_report['raw']['latest_year'] == 2025
    assert year_report['period']['end'] == '2026-08-01'
    assert year_report['raw']['activity_end'] == '2025-01-10'
    assert [row['year'] for row in year_report['raw']['years']] == [2025, 2026]
    years = {row['year']: row for row in year_report['raw']['years']}
    assert years[2025]['revenue'] == 100.0
    assert years[2026]['revenue'] == 0.0
    assert years[2026]['appointments'] == 0
    assert years[2026]['is_partial_year'] is True


@pytest.mark.asyncio
async def test_year_over_year_uses_configured_branch_scope_when_access_is_unrestricted(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Configured salon', group_id=1),
        Company(id=2, title='Unconfigured salon', group_id=1),
        Staff(id=1, name='Configured master', position='Барбер', company_id=1),
        Staff(id=2, name='Unconfigured master', position='Барбер', company_id=2),
        PortalAccount(id=1, label='Tenant', created_at=datetime(2024, 1, 1)),
        PortalBranch(portal_account_id=1, company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2024, 1, 1),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=2,
            staff_id=2,
            date=date(2025, 1, 1),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2024, 1, 1, 12),
            amount=100.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=2,
            master_id=2,
            record_id=2,
            sold_item_id=20,
            sold_item_type='service',
            date=datetime(2025, 1, 1, 12),
            amount=900.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2024, 1, 1),
            period_end=date(2024, 12, 31),
            synced_at=datetime(2025, 1, 1),
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2025, 1, 1),
        date(2025, 1, 31),
    )

    assert report['raw']['latest_year'] == 2024
    assert [row['year'] for row in report['raw']['years']] == [2024, 2025, 2026]
    years = {row['year']: row for row in report['raw']['years']}
    assert years[2024]['revenue'] == 100.0
    assert years[2025]['revenue'] is None
    assert years[2026]['revenue'] is None

    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2024, 1, 1),
        date(2025, 12, 31),
        include_appointments_breakdown=False,
    )
    daily = await dashboard_service.fetch_revenue_daily(
        async_session,
        date(2024, 1, 1),
        date(2025, 12, 31),
        include_opz=False,
    )
    top_services = await dashboard_service.fetch_top_services(
        async_session,
        date(2024, 1, 1),
        date(2025, 12, 31),
    )

    assert overview['revenue']['total'] == 100.0
    assert overview['revenue']['appointments'] == 1
    assert sum(row['revenue'] for row in daily) == 100.0
    assert sum(row['appointments'] for row in daily) == 1
    assert [row['date'] for row in daily] == ['2024-01-01']
    assert [row['revenue'] for row in top_services] == [100.0]


@pytest.mark.asyncio
async def test_dashboard_year_over_year_keeps_partial_multi_company_year(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12),
    )
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon 1', group_id=1),
        Company(id=2, title='Salon 2', group_id=1),
        Company(id=3, title='Unsynced salon', group_id=1),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
        Appointment(id=1, company_id=1, staff_id=1, date=date(2023, 1, 1), attendance=1),
        Appointment(id=2, company_id=2, staff_id=2, date=date(2023, 1, 1), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, date=date(2024, 12, 31), attendance=1),
        Appointment(id=4, company_id=2, staff_id=2, date=date(2024, 12, 31), attendance=1),
        FinancialTransaction(id=1, company_id=1, master_id=1, record_id=1, sold_item_type='service', date=datetime(2023, 1, 1, 12), amount=100.0),
        FinancialTransaction(id=2, company_id=2, master_id=2, record_id=2, sold_item_type='service', date=datetime(2023, 1, 1, 12), amount=200.0),
        FinancialTransaction(id=3, company_id=1, master_id=1, record_id=3, sold_item_type='service', date=datetime(2024, 12, 31, 12), amount=300.0),
        FinancialTransaction(id=4, company_id=2, master_id=2, record_id=4, sold_item_type='service', date=datetime(2024, 12, 31, 12), amount=400.0),
        SyncSourceState(company_id=1, source='appointments_detail', period_start=date(2023, 1, 1), period_end=date(2024, 12, 31), synced_at=datetime(2025, 1, 1)),
        SyncSourceState(company_id=1, source='financial_transactions_detail', period_start=date(2023, 1, 1), period_end=date(2024, 12, 31), synced_at=datetime(2025, 1, 1)),
        SyncSourceState(company_id=2, source='appointments_detail', period_start=date(2023, 1, 1), period_end=date(2024, 12, 31), synced_at=datetime(2025, 1, 1)),
        SyncSourceState(company_id=2, source='financial_transactions_detail', period_start=date(2023, 1, 1), period_end=date(2023, 12, 31), synced_at=datetime(2025, 1, 1)),
        SyncSourceState(company_id=1, source='goods_transactions_detail', period_start=date(2023, 1, 1), period_end=date(2024, 12, 31), synced_at=datetime(2025, 1, 1)),
        SyncSourceState(company_id=2, source='goods_transactions_detail', period_start=date(2023, 1, 1), period_end=date(2024, 12, 31), synced_at=datetime(2025, 1, 1)),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'year_over_year',
                'start_date': '2024-01-01',
                'end_date': '2024-12-31',
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['source_status'] == 'partial'
    assert 'financial_transactions_detail' in data['missing_sources']
    assert [row['year'] for row in data['raw']['years']] == [2023, 2024, 2025, 2026]
    years = {row['year']: row for row in data['raw']['years']}
    # Company 3 has no local facts or coverage and still participates in scope.
    assert years[2023]['source_status'] == 'partial'
    assert 'appointments_detail' in years[2023]['missing_components']
    assert years[2024]['revenue'] is None
    assert years[2024]['appointments'] is None
    assert years[2024]['avg_check'] is None
    assert years[2024]['source_status'] == 'partial'
    assert years[2024]['comparison_status'] == 'incomplete_source'
    assert years[2024]['revenue_change_pct'] is None
    assert years[2025]['revenue'] is None
    assert years[2026]['revenue'] is None
    cards = {card['label']: card['value'] for card in data['cards']}
    assert cards['Выручка последнего года'] is None
    assert cards['Визиты последнего года'] is None
    charts = {chart['id']: chart for chart in data['charts']}
    assert charts['year_revenue']['datasets'][0]['data'] == [None, None, None, None]
    assert charts['year_appointments']['datasets'][0]['data'] == [None, None, None, None]
    december_2024 = next(
        row
        for row in data['raw']['months']
        if row['year'] == 2024 and row['month'] == 12
    )
    assert december_2024['revenue'] is None
    assert december_2024['appointments'] is None
    assert december_2024['source_status'] == 'partial'


@pytest.mark.asyncio
async def test_year_over_year_query_count_does_not_grow_with_history_years(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon 1', group_id=1),
        Company(id=2, title='Salon 2', group_id=1),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
        Appointment(id=1, company_id=1, staff_id=1, date=date(2015, 1, 1), attendance=1),
        Appointment(id=2, company_id=2, staff_id=2, date=date(2026, 7, 1), attendance=1),
        FinancialTransaction(id=1, company_id=1, master_id=1, record_id=1, sold_item_type='service', date=datetime(2015, 1, 1, 12), amount=100.0),
        FinancialTransaction(id=2, company_id=2, master_id=2, record_id=2, sold_item_type='service', date=datetime(2026, 7, 1, 12), amount=200.0),
    ])
    for company_id in (1, 2):
        for source in (
            'appointments_detail',
            'financial_transactions_detail',
            'goods_transactions_detail',
        ):
            async_session.add(SyncSourceState(
                company_id=company_id,
                source=source,
                period_start=date(2015, 1, 1),
                period_end=date(2026, 12, 31),
                synced_at=datetime(2026, 8, 1),
            ))
    await async_session.commit()

    statement_count = 0

    def count_statements(*_args):
        nonlocal statement_count
        statement_count += 1

    event.listen(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
    try:
        data = await dashboard_reports.fetch_report_data(
            async_session,
            'year_over_year',
            date(2026, 1, 1),
            date(2026, 1, 31),
            allowed_company_ids=[1, 2],
        )
    finally:
        event.remove(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)

    assert [row['year'] for row in data['raw']['years']] == list(range(2015, 2027))
    assert data['raw']['latest_year'] == 2026
    assert statement_count <= 16


@pytest.mark.asyncio
async def test_year_over_year_opz_deduplicates_repeated_client_per_year(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Returning client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 1, 10),
            datetime=datetime(2024, 1, 10, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 2, 1),
            create_date=datetime(2024, 1, 10, 12),
            attendance=0,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 10),
            attendance=1,
        ),
        Appointment(
            id=4,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 1),
            create_date=datetime(2025, 1, 10, 12),
            attendance=0,
        ),
        SyncSourceState(
            company_id=1,
            source='appointments_detail',
            period_start=date(2024, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=datetime(2026, 8, 1),
        ),
        SyncSourceState(
            company_id=1,
            source='financial_transactions_detail',
            period_start=date(2024, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=datetime(2026, 8, 1),
        ),
        SyncSourceState(
            company_id=1,
            source='goods_transactions_detail',
            period_start=date(2024, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=datetime(2026, 8, 1),
        ),
    ])
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2025, 1, 1),
        date(2025, 1, 31),
        allowed_company_ids=[1],
    )
    years = {row['year']: row for row in report['raw']['years']}
    overview_2024 = await dashboard_service.fetch_summary(
        async_session,
        date(2024, 1, 10),
        date(2024, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    overview_2025 = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 2, 1),
        company_id=1,
        include_appointments_breakdown=False,
    )

    assert years[2024]['opz_qty'] == overview_2024['visit_metrics']['opz_qty'] == 1.0
    assert years[2025]['opz_qty'] == overview_2025['visit_metrics']['opz_qty'] == 1.0


@pytest.mark.asyncio
async def test_year_over_year_opz_fact_advances_latest_year(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Client', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 12, 31),
            datetime=datetime(2024, 12, 31, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 1),
            create_date=datetime(2025, 1, 1, 12),
            attendance=0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2024, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=datetime(2026, 8, 1),
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2025, 1, 1),
        date(2025, 1, 1),
        allowed_company_ids=[1],
    )
    years = {row['year']: row for row in report['raw']['years']}
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 1),
        company_id=1,
        include_appointments_breakdown=False,
    )
    plan_fact = await dashboard_service._fact_metric_components(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 1),
        1,
    )

    assert report['raw']['activity_end'] == '2025-01-01'
    assert report['raw']['latest_year'] == 2025
    assert years[2025]['opz_qty'] == overview['visit_metrics']['opz_qty'] == 1.0
    assert years[2025]['opz_qty'] == plan_fact['opz_qty']


@pytest.mark.asyncio
async def test_year_over_year_masks_opz_when_dependent_appointments_are_uncovered(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Client', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 12, 31),
            datetime=datetime(2024, 12, 31, 12),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 1),
            create_date=datetime(2025, 1, 1, 10),
            attendance=0,
        ),
    ])
    appointment_state = SyncSourceState(
        company_id=1,
        source='appointments_detail',
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        synced_at=factual_at,
    )
    async_session.add(appointment_state)
    for source in ('financial_transactions_detail', 'goods_transactions_detail'):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=factual_at,
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    year_2025 = next(row for row in report['raw']['years'] if row['year'] == 2025)

    assert year_2025['source_status'] == 'partial'
    assert year_2025['missing_components'] == ['appointments_detail']
    assert year_2025['opz_qty'] is None
    assert year_2025['opz_pct'] is None
    assert year_2025['revenue'] == 0.0
    assert year_2025['appointments'] == 0

    appointment_state.period_start = date(2024, 12, 31)
    appointment_state.period_end = date(2025, 12, 31)
    await async_session.commit()
    covered_report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    covered_2025 = next(
        row for row in covered_report['raw']['years'] if row['year'] == 2025
    )

    assert covered_2025['source_status'] == 'ready'
    assert covered_2025['opz_qty'] == 1.0


@pytest.mark.asyncio
async def test_opz_year_facts_ignore_later_same_day_visit_for_staff(
    async_session,
):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Factual master', position='Барбер', company_id=1),
        Staff(id=2, name='Future master', position='Барбер', company_id=1),
        Client(id=1, name='Client', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 7, 31),
            datetime=datetime(2026, 7, 31, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 8, 15),
            create_date=datetime(2026, 8, 1, 10),
            attendance=0,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=2,
            client_id=1,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 18),
            attendance=1,
        ),
    ])
    await async_session.commit()

    facts = await dashboard_service.fetch_opz_year_facts(
        async_session,
        date(2026, 7, 31),
        date(2026, 8, 1),
        company_id=1,
        staff_id=1,
        factual_at=datetime(2026, 8, 1, 12),
    )

    assert facts['latest_date'] == date(2026, 8, 1)
    assert facts['counts'] == {2026: 1.0}


@pytest.mark.asyncio
async def test_year_over_year_goods_source_controls_count_and_latest_year(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2024, 1, 10),
            attendance=1,
        ),
        GoodTransaction(
            id=1,
            company_id=1,
            master_id=1,
            type_id=1,
            document_id=1,
            amount=-2.0,
            date=datetime(2026, 7, 20, 12),
        ),
        SyncSourceState(
            company_id=1,
            source='appointments_detail',
            period_start=date(2024, 1, 1),
            period_end=date(2026, 12, 31),
            synced_at=datetime(2026, 8, 1),
        ),
        SyncSourceState(
            company_id=1,
            source='financial_transactions_detail',
            period_start=date(2024, 1, 1),
            period_end=date(2026, 12, 31),
            synced_at=datetime(2026, 8, 1),
        ),
    ])
    await async_session.commit()

    missing_goods = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    missing_years = {row['year']: row for row in missing_goods['raw']['years']}
    july_2026 = next(
        row
        for row in missing_goods['raw']['months']
        if row['year'] == 2026 and row['month'] == 7
    )
    assert missing_goods['raw']['latest_year'] == 2026
    assert missing_years[2026]['goods_count'] is None
    assert missing_years[2026]['source_status'] == 'partial'
    assert 'goods_transactions_detail' in missing_years[2026]['missing_components']
    assert july_2026['revenue'] == 0.0
    assert july_2026['source_status'] == 'ready'

    async_session.add(SyncSourceState(
        company_id=1,
        source='goods_transactions_detail',
        period_start=date(2024, 1, 1),
        period_end=date(2026, 12, 31),
        synced_at=datetime(2026, 8, 1),
    ))
    await async_session.commit()
    ready_goods = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )
    ready_2026 = next(row for row in ready_goods['raw']['years'] if row['year'] == 2026)
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2026, 1, 1),
        date(2026, 7, 20),
        company_id=1,
        include_appointments_breakdown=False,
    )
    assert ready_goods['raw']['activity_end'] == '2026-07-20'
    assert ready_2026['source_status'] == 'ready'
    assert ready_2026['goods_count'] == overview['revenue']['goods_count'] == 2.0


@pytest.mark.asyncio
async def test_year_over_year_nonfactual_visits_do_not_advance_latest_year(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12, 0),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2024, 1, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            date=date(2026, 6, 20),
            attendance=-1,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 18),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=3,
            sold_item_type='service',
            # The payment date is inside the retained historical window, but
            # the linked visit itself is still later than report_now.
            date=datetime(2024, 1, 10, 10),
            amount=700.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2024, 1, 1),
            period_end=date(2026, 12, 31),
            synced_at=datetime(2026, 8, 1),
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        allowed_company_ids=[1],
    )

    assert report['raw']['activity_end'] == '2024-01-10'
    assert report['raw']['latest_year'] == 2024
    assert [row['year'] for row in report['raw']['years']] == [2024, 2025, 2026]
    years = {row['year']: row for row in report['raw']['years']}
    assert years[2024]['revenue'] == 0.0
    assert years[2024]['appointments'] == 1
    assert years[2024]['is_latest_year'] is True
    assert years[2025]['revenue'] == 0.0
    assert years[2026]['revenue'] == 0.0
    assert years[2026]['is_partial_year'] is True
    current_months = [
        row
        for row in report['raw']['months']
        if row['year'] == 2026
    ]
    assert [row['revenue'] for row in current_months[:8]] == [0.0] * 8
    assert [row['revenue'] for row in current_months[8:]] == [None] * 4


@pytest.mark.asyncio
async def test_dashboard_report_data_validates_request(async_session):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        bad_granularity = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'revenue_dynamics',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'granularity': 'quarter',
            },
        )
        bad_report = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'missing', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        bad_compare = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'revenue_dynamics',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'compare_start_date': '2024-12-01',
            },
        )
        bad_company = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'revenue_dynamics',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 999,
            },
        )
        bad_staff = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'revenue_dynamics',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'staff_id': 999,
            },
        )
    app.dependency_overrides.clear()

    assert bad_granularity.status_code == 400
    assert bad_report.status_code == 400
    assert bad_compare.status_code == 400
    assert bad_company.status_code == 400
    assert bad_staff.status_code == 400


@pytest.mark.asyncio
async def test_dashboard_report_data_missing_and_partial_sources(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', company_id=1))
    async_session.add(Comment(
        id=1,
        type='review',
        master_id=1,
        text='bad',
        date=datetime(2025, 1, 10, 12, 0, 0),
        rating=2,
        company_id=1,
    ))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        missing = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'conversion_funnel', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        partial = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'nps_dashboard', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert missing.status_code == 200
    assert missing.json()['data']['source_status'] == 'missing'
    assert missing.json()['data']['missing_sources'] == ['yandex_metrika']
    assert partial.status_code == 200
    partial_data = partial.json()['data']
    assert partial_data['source_status'] == 'partial'
    assert 'telegram_nps' in partial_data['missing_sources']
    assert partial_data['tables'][0]['rows'][0]['rating'] == 2.0


@pytest.mark.asyncio
async def test_dashboard_staff_leaderboard_report_returns_top_tables(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'staff_leaderboard', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['source_status'] == 'ready'
    table_ids = {table['id'] for table in data['tables']}
    assert {
        'extra_services',
        'cosmo_barber',
        'opz_admin',
        'reviews_admin',
        'revenue_barber',
        'revenue_admin',
        'avg_check_plan_branch',
        'avg_check_plan_staff',
    } <= table_ids
    extra_services = next(table for table in data['tables'] if table['id'] == 'extra_services')
    cosmo_barber = next(table for table in data['tables'] if table['id'] == 'cosmo_barber')
    opz_admin = next(table for table in data['tables'] if table['id'] == 'opz_admin')
    revenue_barber = next(table for table in data['tables'] if table['id'] == 'revenue_barber')
    revenue_admin = next(table for table in data['tables'] if table['id'] == 'revenue_admin')
    assert extra_services['ranking']['default_metric'] == 'pct'
    assert set(extra_services['ranking']['rows_by_metric']) == {'qty', 'sum', 'pct'}
    assert cosmo_barber['ranking']['default_metric'] == 'sum'
    assert set(cosmo_barber['ranking']['rows_by_metric']) == {'qty', 'sum', 'pct'}
    assert opz_admin['ranking']['default_metric'] == 'pct'
    assert set(opz_admin['ranking']['rows_by_metric']) == {'qty', 'pct'}
    assert {'key': 'pct', 'label': 'Косметика, %', 'format': 'percent'} in cosmo_barber['columns']
    assert all(column['key'] != 'cosmo_revenue_share_pct' for column in revenue_barber['columns'])
    assert all(column['key'] != 'cosmo_revenue_share_pct' for column in revenue_admin['columns'])
    assert [card['label'] for card in data['cards']] == ['Топ выручка мастера']
    reviews_admin = next(table for table in data['tables'] if table['id'] == 'reviews_admin')
    assert extra_services.get('hide_when_empty') is True
    assert opz_admin.get('hide_when_empty') is True
    assert revenue_barber.get('hide_when_empty') is True
    assert 'hide_when_empty' not in reviews_admin


@pytest.mark.asyncio
async def test_dashboard_staff_efficiency_revenue_uses_paid_service_rows(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Staff(id=2, name='Payment only master', position='Барбер', company_id=1),
        Client(id=1, name='Client', company_id=1),
        Client(id=2, name='Payment only client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 11), attendance=1),
        Appointment(id=3, company_id=1, staff_id=2, client_id=2, date=date(2024, 12, 20), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        *(
            row
            for row in _paid_service_revenue_filter_rows()
            if row.sold_item_type != 'goods_transaction'
        ),
        AccountCatalog(
            company_id=1,
            account_id=99,
            title='Бонусный счет',
            updated_at=datetime(2025, 1, 1),
        ),
        FinancialTransaction(
            id=10,
            date=datetime(2025, 1, 10, 13, 0),
            amount=9999.0,
            record_id=1,
            sold_item_type='service',
            master_id=1,
            account_id=99,
            company_id=1,
        ),
        FinancialTransaction(
            id=11,
            date=datetime(2025, 1, 20, 12),
            amount=700.0,
            record_id=3,
            sold_item_type='service',
            master_id=2,
            company_id=1,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'staff_efficiency', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    rows = data['raw']['staff']
    assert rows == [
        {
            'staff_id': 1,
            'staff_name': 'Master',
            'company_title': 'Salon',
            'appointments': 2,
            'completed': 2,
            'not_completed': 0,
            'clients': 1,
            'revenue': 1500.0,
            'avg_check': 750.0,
        },
        {
            'staff_id': 2,
            'staff_name': 'Payment only master',
            'company_title': 'Salon',
            'appointments': 0,
            'completed': 0,
            'not_completed': 0,
            'clients': 0,
            'revenue': 700.0,
            'avg_check': 0.0,
        },
    ]
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    plan_fact = await dashboard_service._fact_metric_components(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        1,
    )
    assert data['cards'][2]['value'] == overview['revenue']['service_revenue'] == 2200.0
    assert data['cards'][2]['value'] == plan_fact['revenue']


@pytest.mark.asyncio
async def test_goods_report_revenue_matches_paid_overview_component(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            attendance=1,
        )
    )
    async_session.add_all([
        GoodCatalog(
            company_id=1,
            good_id=10,
            title='Помада',
            updated_at=datetime(2025, 1, 1),
        ),
        GoodTransaction(
            id=1,
            document_id=50,
            type_id=1,
            good_id=10,
            good_title='Помада',
            amount=-2.0,
            cost=3.0,
            master_id=1,
            company_id=1,
            date=datetime(2025, 1, 10, 12, 0),
        ),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0),
            amount=2400.0,
            record_id=1,
            sold_item_id=1,
            sold_item_type='goods_transaction',
            master_id=None,
            company_id=1,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'goods_dynamics',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'granularity': 'month',
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    cards = {card['label']: card['value'] for card in data['cards']}
    assert cards['Выручка товаров'] == 2400.0
    assert cards['Единиц продано'] == 2.0
    assert data['raw']['goods'] == [{
        'good_title': 'Помада',
        'sales_count': 1,
        'units': 2.0,
        'revenue': 2400.0,
    }]
    assert data['raw']['by_staff'] == [{
        'staff_name': 'Master',
        'sales_count': 1,
        'revenue': 2400.0,
    }]
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    assert cards['Выручка товаров'] == overview['revenue']['goods_revenue']


@pytest.mark.asyncio
async def test_service_breakdowns_use_the_overview_physical_account_filter(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Service(id=10, title='Воск', company_id=1),
        AccountCatalog(
            company_id=1,
            account_id=1,
            title='Наличные',
            updated_at=datetime(2025, 1, 1),
        ),
        AccountCatalog(
            company_id=1,
            account_id=2,
            title='Бонусный счет',
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            attendance=1,
        ),
        Transaction(
            id=1,
            appointment_id=1,
            service_id=10,
            service_title='Воск',
            amount=1,
            company_id=1,
        ),
        ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Воск',
            updated_at=datetime(2025, 1, 1),
        ),
        ServiceLabel(
            company_id=1,
            service_id=10,
            is_extra=True,
            source='test',
            updated_at=datetime(2025, 1, 1),
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            account_id=1,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            date=datetime(2025, 1, 10, 12, 0),
            amount=100.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            account_id=2,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            date=datetime(2025, 1, 10, 12, 1),
            amount=900.0,
        ),
    ])
    await async_session.commit()

    top_services = await dashboard_service.fetch_top_services(
        async_session, date(2025, 1, 1), date(2025, 1, 31), company_id=1
    )
    extra_services = await dashboard_service.fetch_extra_services(
        async_session, date(2025, 1, 1), date(2025, 1, 31), company_id=1
    )
    leaderboard_extra_revenue = await dashboard_service._extra_service_revenue_by_staff(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        [1],
    )

    assert len(top_services) == 1
    assert top_services[0]['revenue'] == 100.0
    assert len(extra_services) == 1
    assert extra_services[0]['revenue'] == 100.0
    assert leaderboard_extra_revenue == {1: 100.0}


@pytest.mark.asyncio
async def test_dashboard_staff_leaderboard_report_returns_retryable_503_on_total_failure(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    await async_session.commit()

    import dashboard_reports

    async def boom(*args, **kwargs):
        raise RuntimeError('boom')

    monkeypatch.setattr(dashboard_reports, 'fetch_plan_fact', boom)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'staff_leaderboard', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 503
    detail = r.json()['detail']
    assert detail == {
        'code': 'report_calculation_failed',
        'message': 'Не удалось рассчитать рейтинги за выбранный период.',
        'retryable': True,
    }


@pytest.mark.asyncio
async def test_dashboard_staff_leaderboard_returns_partial_with_null_optional_sums(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1, fired=0))
    async_session.add(Service(id=10, title='воск', company_id=1))
    async_session.add(
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        )
    )
    async_session.add(
        Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 1, 10), attendance=1)
    )
    async_session.add(
        Transaction(
            id=1,
            appointment_id=1,
            service_id=10,
            service_title='воск',
            amount=1,
            company_id=1,
        )
    )
    await async_session.commit()

    async def unavailable(*args, **kwargs):
        return None

    monkeypatch.setattr(dashboard_service, '_extra_service_revenue_by_staff', unavailable)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'staff_leaderboard', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['source_status'] == 'partial'
    extra = next(table for table in data['tables'] if table['id'] == 'extra_services')
    assert extra['ranking']['rows_by_metric']['sum'] == []
    assert extra['ranking']['rows_by_metric']['qty'], data
    assert extra['ranking']['rows_by_metric']['qty'][0]['sum'] is None
    assert any(note['kind'] == 'warning' for note in data['notes'])


@pytest.mark.asyncio
async def test_staff_leaderboard_applies_component_money_permissions_without_raw_leaks(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    await async_session.commit()

    identity = {'staff': 'Master', 'staff_id': 1, 'company_id': 1, 'company_title': 'Salon'}
    extra_row = {**identity, 'qty': 2.0, 'sum': 500.0, 'pct': 20.0, 'share_pct': 100.0}
    admin_extra_row = {
        'staff': 'Admin',
        'staff_id': 2,
        'company_id': 1,
        'company_title': 'Salon',
        'qty': 3.0,
        'pct': 30.0,
    }
    cosmo_row = {**identity, 'qty': 1.0, 'sum': 300.0, 'pct': 12.0, 'share_pct': 100.0}
    opz_row = {**identity, 'qty': 1.0, 'pct': 10.0}
    value_row = {**identity, 'value': 2500.0}
    avg_row = {**identity, 'plan': 1000.0, 'fact': 1200.0, 'pct': 120.0}

    async def fake_plan_fact(*args, **kwargs):
        return {
            'staff_leaderboards': {
                'extra_services_barber_rankings': {
                    'qty': [extra_row],
                    'sum': [extra_row],
                    'pct': [extra_row],
                },
                'extra_services_admin_rankings': {
                    'qty': [admin_extra_row],
                    'pct': [admin_extra_row],
                },
                'cosmo_barber_rankings': {'qty': [cosmo_row], 'sum': [cosmo_row], 'pct': [cosmo_row]},
                'cosmo_admin_rankings': {'qty': [], 'sum': [], 'pct': []},
                'opz_barber_rankings': {'qty': [opz_row], 'pct': [opz_row]},
                'opz_admin_rankings': {'qty': [], 'pct': []},
                'reviews_admin': [{**identity, 'value': 3.0}],
                'revenue_barber': [value_row],
                'revenue_admin': [],
                'avg_check_plan_branch': [avg_row],
                'avg_check_plan_staff': [avg_row],
            }
        }

    async def override_db():
        yield async_session

    async def override_access():
        return AccessContext.from_user(
            user_id=10,
            role='manager',
            portal_account_id=1,
            company_ids=[1],
            money_metrics=frozenset({'cosmo_sum'}),
        )

    monkeypatch.setattr(dashboard_reports, 'fetch_plan_fact', fake_plan_fact)
    app.dependency_overrides[api.get_async_db] = override_db
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = override_access
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'staff_leaderboard', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    table_ids = {table['id'] for table in data['tables']}
    assert {
        'cosmo_barber',
        'extra_services',
        'extra_services_admin',
        'opz_barber',
        'reviews_admin',
    } <= table_ids
    assert {'revenue_barber', 'revenue_admin', 'avg_check_plan_branch', 'avg_check_plan_staff'}.isdisjoint(table_ids)
    extra = next(table for table in data['tables'] if table['id'] == 'extra_services')
    assert 'sum' not in {column['key'] for column in extra['columns']}
    assert 'sum' not in extra['ranking']['rows_by_metric']
    assert all('sum' not in row for rows in extra['ranking']['rows_by_metric'].values() for row in rows)
    extra_admin = next(table for table in data['tables'] if table['id'] == 'extra_services_admin')
    assert extra_admin['rows'][0]['staff'] == 'Admin'
    assert {option['key'] for option in extra_admin['ranking']['options']} == {'qty', 'pct'}
    assert data['cards'] == []
    assert data['raw'] == {}


def test_staff_leaderboards_sort_stably_and_require_positive_average_check_plan():
    def group(staff_id, title, *, avg_plan, avg_fact, revenue=200.0, cosmo=50.0):
        metrics = [
            {'code': 'avg_check_total', 'plan': avg_plan, 'fact': avg_fact},
            {'code': 'revenue', 'plan': None, 'fact': revenue},
            {'code': 'cosmo_sum', 'plan': None, 'fact': cosmo},
            {'code': 'cosmo_qty', 'plan': None, 'fact': 1.0},
        ]
        return {
            'staff_id': staff_id,
            'title': title,
            'company_id': 1,
            'company_title': 'Salon',
            'category': 'barber',
            'metrics': metrics,
        }

    staff_groups = [
        group(2, 'Zed', avg_plan=100.0, avg_fact=150.0),
        group(1, 'Alpha', avg_plan=100.0, avg_fact=150.0),
        group(3, 'Zero plan', avg_plan=0.0, avg_fact=500.0),
        group(4, 'No plan', avg_plan=None, avg_fact=500.0),
    ]
    branch_groups = [
        {
            'company_id': 1,
            'title': 'Salon',
            'metrics': [{'code': 'avg_check_total', 'plan': 100.0, 'fact': 80.0}],
        }
    ]

    boards = _staff_leaderboards_payload(staff_groups, branch_groups=branch_groups)

    assert [(row['staff'], row['pct']) for row in boards['avg_check_plan_staff']] == [
        ('Alpha', 150.0),
        ('Zed', 150.0),
    ]
    assert boards['avg_check_plan_branch'][0]['pct'] == 80.0
    assert boards['cosmo_barber_rankings']['pct'][0]['pct'] == 25.0


def test_staff_leaderboard_metric_variants_sort_by_the_selected_measure():
    def group(staff_id, title, facts, *, category='barber'):
        return {
            'staff_id': staff_id,
            'title': title,
            'company_id': 1,
            'company_title': 'Salon',
            'category': category,
            'metrics': [{'code': code, 'plan': None, 'fact': value} for code, value in facts.items()],
        }

    groups = [
        group(1, 'Quantity leader', {
            'extra_services_qty': 10.0,
            'wax_qty': 6.0,
            'camouflage_qty': 4.0,
            'extra_services_pct': 10.0,
            'cosmo_qty': 3.0,
            'cosmo_sum': 100.0,
            'revenue': 1000.0,
            'opz_qty': 5.0,
            'opz_pct': 20.0,
        }),
        group(2, 'Percent leader', {
            'extra_services_qty': 5.0,
            'wax_qty': 3.0,
            'camouflage_qty': 2.0,
            'extra_services_pct': 30.0,
            'cosmo_qty': 2.0,
            'cosmo_sum': 200.0,
            'revenue': 400.0,
            'opz_qty': 2.0,
            'opz_pct': 50.0,
        }),
        group(3, 'Admin leader', {
            'extra_services_qty': 100.0,
            'extra_services_pct': 80.0,
        }, category='administrator'),
    ]

    boards = _staff_leaderboards_payload(
        groups,
        extra_revenue_by_staff={1: 100.0, 2: 500.0},
    )

    assert boards['extra_services_barber_rankings']['qty'][0]['staff'] == 'Quantity leader'
    assert boards['extra_services_barber_rankings']['sum'][0]['staff'] == 'Percent leader'
    assert boards['extra_services_barber_rankings']['pct'][0]['staff'] == 'Percent leader'
    assert boards['extra_services_admin_rankings']['qty'][0]['staff'] == 'Admin leader'
    assert boards['extra_services_admin_rankings']['pct'][0]['staff'] == 'Admin leader'
    assert 'sum' not in boards['extra_services_admin_rankings']
    assert boards['extra_services'] == boards['extra_services_barber']
    assert boards['extra_services_rankings'] == boards['extra_services_barber_rankings']
    assert boards['cosmo_barber_rankings']['qty'][0]['staff'] == 'Quantity leader'
    assert boards['cosmo_barber_rankings']['sum'][0]['staff'] == 'Percent leader'
    assert boards['cosmo_barber_rankings']['pct'][0]['staff'] == 'Percent leader'
    assert boards['opz_barber_rankings']['qty'][0]['staff'] == 'Quantity leader'
    assert boards['opz_barber_rankings']['pct'][0]['staff'] == 'Percent leader'


@pytest.mark.asyncio
async def test_plan_fact_staff_query_count_does_not_grow_with_staff_count(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Master 01', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Master 02', position='Барбер', company_id=1, fired=0),
    ])
    await async_session.commit()

    statements: list[str] = []

    def count_selects(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith('SELECT'):
            statements.append(statement)

    engine = async_session.bind.sync_engine
    event.listen(engine, 'before_cursor_execute', count_selects)
    try:
        await fetch_plan_fact(
            async_session,
            date(2025, 1, 1),
            date(2025, 1, 31),
            include_all_staff_in_leaderboards=True,
        )
        small_count = len(statements)

        async_session.add_all([
            Staff(id=staff_id, name=f'Master {staff_id:02}', position='Барбер', company_id=1, fired=0)
            for staff_id in range(3, 23)
        ])
        await async_session.commit()
        statements.clear()

        await fetch_plan_fact(
            async_session,
            date(2025, 1, 1),
            date(2025, 1, 31),
            include_all_staff_in_leaderboards=True,
        )
        large_count = len(statements)
    finally:
        event.remove(engine, 'before_cursor_execute', count_selects)

    assert small_count > 0
    assert large_count <= small_count + 1


@pytest.mark.asyncio
async def test_dashboard_bundle_requires_api_key(async_session, monkeypatch):
    monkeypatch.setattr(auth_deps, 'API_KEY', 'k')

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        assert r.status_code == 401
        r2 = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'X-API-Key': 'k'},
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body['success'] is True
        assert 'summary' in body['data']
        assert 'revenue_daily' in body['data']
        assert 'top_services' in body['data']
        assert 'extra_services' in body['data']
        assert 'plan_fact' not in body['data']

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'API_KEY', '')


@pytest.mark.asyncio
async def test_dashboard_bundle_passes_one_factual_cutoff_to_every_metric_block(
    async_session,
    monkeypatch,
):
    cutoffs = []

    def fake_fetch(result):
        async def fetch(*args, **kwargs):
            cutoffs.append(kwargs.get('factual_at'))
            return result

        return fetch

    monkeypatch.setattr(dashboard_routes, 'fetch_summary', fake_fetch({}))
    monkeypatch.setattr(dashboard_routes, 'fetch_revenue_daily', fake_fetch([]))
    monkeypatch.setattr(dashboard_routes, 'fetch_top_services', fake_fetch([]))
    monkeypatch.setattr(dashboard_routes, 'fetch_extra_services', fake_fetch([]))

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2026-08-01', 'end_date': '2026-08-01'},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert len(cutoffs) == 4
    assert cutoffs[0] is not None
    assert all(cutoff == cutoffs[0] for cutoff in cutoffs)
    assert cutoffs[0].tzinfo is None
    assert abs((datetime.now(UTC).replace(tzinfo=None) - cutoffs[0]).total_seconds()) < 5


@pytest.mark.asyncio
async def test_platform_admin_dashboard_bundle_requires_selected_tenant(async_session, monkeypatch):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(Group(id=1, title='Group'))
    async_session.add(Company(id=1, title='Tenant A Branch', group_id=1))
    async_session.add(Company(id=2, title='Tenant B Branch', group_id=1))
    async_session.add(PortalAccount(id=1, label='Tenant A', created_at=datetime.utcnow()))
    async_session.add(PortalAccount(id=2, label='Tenant B', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(PortalBranch(portal_account_id=2, company_id=2))
    async_session.add(
        PortalUser(
            id=900,
            portal_account_id=None,
            email='platform.dashboard@example.com',
            password_hash=hash_password('Platform12345!'),
            full_name='Platform Admin',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(900, 'platform_admin')
    headers = {'Authorization': f'Bearer {token}'}
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        missing_tenant = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2026-06-01', 'end_date': '2026-06-21'},
            headers=headers,
        )
        selected_tenant = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2026-06-01', 'end_date': '2026-06-21'},
            headers={**headers, 'X-Portal-Account-Id': '2'},
        )

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    assert missing_tenant.status_code == 403
    assert missing_tenant.json()['detail'] == 'No branch access assigned'
    assert selected_tenant.status_code == 200
    assert selected_tenant.json()['data']['summary']['period'] == {
        'start': '2026-06-01',
        'end': '2026-06-21',
    }


@pytest.mark.parametrize(
    'path',
    [
        '/dashboard/widget/summary',
        '/dashboard/widget/revenue_daily',
        '/dashboard/widget/top_services',
        '/dashboard/widget/extra_services',
        '/dashboard/widget/plan_fact',
        '/dashboard/bundle',
    ],
)
@pytest.mark.asyncio
async def test_dashboard_widgets_reject_foreign_staff_scope(async_session, monkeypatch, path):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(Group(id=1, title='Group'))
    async_session.add_all([
        Company(id=1, title='Allowed Branch', group_id=1),
        Company(id=2, title='Foreign Branch', group_id=1),
        Staff(id=10, name='Allowed Staff', company_id=1),
        Staff(id=20, name='Foreign Staff', company_id=2),
        PortalAccount(id=1, label='Tenant', created_at=datetime.utcnow()),
        PortalBranch(portal_account_id=1, company_id=1),
        PortalUser(
            id=910,
            portal_account_id=1,
            email='owner-dashboard-scope@example.com',
            password_hash=hash_password('Owner12345!'),
            role='owner',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(910, 'owner')
    headers = {'Authorization': f'Bearer {token}'}
    params = {
        'start_date': '2025-01-01',
        'end_date': '2025-01-31',
        'company_id': 1,
        'staff_id': 20,
    }
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(path, params=params, headers=headers)

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    assert response.status_code == 400
    assert response.json()['detail'] == 'unknown staff_id'


@pytest.mark.asyncio
async def test_linked_viewer_dashboard_metrics_are_staff_scoped(async_session, monkeypatch):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(Group(id=1, title='Group'))
    async_session.add(Company(id=1, title='Branch', group_id=1))
    async_session.add(Company(id=2, title='Other Branch', group_id=1))
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(PortalBranch(portal_account_id=1, company_id=2))
    async_session.add_all([
        PortalUser(
            id=100,
            portal_account_id=1,
            email='viewer@example.com',
            password_hash=hash_password('Viewer12345!'),
            role='viewer',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUser(
            id=101,
            portal_account_id=1,
            email='manager@example.com',
            password_hash=hash_password('Manager12345!'),
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUser(
            id=102,
            portal_account_id=1,
            email='owner@example.com',
            password_hash=hash_password('Owner12345!'),
            role='owner',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUser(
            id=103,
            portal_account_id=1,
            email='branch-admin@example.com',
            password_hash=hash_password('BranchAdmin12345!'),
            role='branch_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUserBranch(user_id=100, company_id=1),
        PortalUserBranch(user_id=101, company_id=1),
        PortalUserBranch(user_id=103, company_id=1),
        Staff(id=1, name='Linked Staff', company_id=1, portal_user_id=100),
        Staff(id=2, name='Other Staff', company_id=1),
        Staff(id=3, name='Foreign Staff', company_id=2),
    ])
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=2, client_id=2, date=date(2025, 1, 11), attendance=1),
        Appointment(
            id=3,
            company_id=2,
            staff_id=3,
            client_id=3,
            date=date(2025, 1, 12),
            attendance=1,
            create_date=datetime(2025, 1, 1, 12, 0, 0),
        ),
        Appointment(
            id=4,
            company_id=2,
            staff_id=3,
            client_id=3,
            date=date(2025, 1, 20),
            attendance=0,
            create_date=datetime(2025, 1, 12, 12, 0, 0),
        ),
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Cut', amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=10, service_title='Cut', amount=1, company_id=1),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=2,
            date=datetime(2025, 1, 11, 12, 0, 0),
            amount=2000.0,
            record_id=2,
            sold_item_id=10,
            sold_item_type='service',
            master_id=2,
            company_id=1,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    viewer_token = create_access_token(100, 'viewer')
    manager_token = create_access_token(101, 'manager')
    owner_token = create_access_token(102, 'owner')
    branch_admin_token = create_access_token(103, 'branch_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        viewer_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {viewer_token}'},
        )
        forbidden_other_staff = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'staff_id': 2},
            headers={'Authorization': f'Bearer {viewer_token}'},
        )
        manager_other_staff = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'staff_id': 2},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        manager_revenue_daily = await client.get(
            '/dashboard/widget/revenue_daily',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        manager_bundle = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        manager_plan_settings = await client.get(
            '/dashboard/plan/settings',
            params={'month': '2025-01'},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        manager_services = await client.get(
            '/dashboard/services',
            params={'company_id': 1},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        manager_service_label = await client.patch(
            '/dashboard/services/1/10/labels',
            json={'is_extra': True},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        manager_service_batch = await client.patch(
            '/dashboard/services',
            json={'row_changes': [], 'group_changes': []},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        manager_review_facts = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-01'},
            headers={'Authorization': f'Bearer {manager_token}'},
        )
        owner_revenue_daily = await client.get(
            '/dashboard/widget/revenue_daily',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {owner_token}'},
        )
        branch_admin_bundle = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {branch_admin_token}'},
        )
        branch_admin_finance_report = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'revenue_dynamics', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {branch_admin_token}'},
        )
        branch_admin_operations_report = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'bookings_dynamics', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {branch_admin_token}'},
        )
        branch_admin_plan_settings = await client.get(
            '/dashboard/plan/settings',
            params={'month': '2025-01'},
            headers={'Authorization': f'Bearer {branch_admin_token}'},
        )
        branch_admin_plan_fact = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers={'Authorization': f'Bearer {branch_admin_token}'},
        )

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    assert viewer_summary.status_code == 200
    assert viewer_summary.json()['data']['financials_hidden'] is True
    assert 'revenue' not in viewer_summary.json()['data']
    assert 'average_check' not in viewer_summary.json()['data']
    assert forbidden_other_staff.status_code == 403
    assert manager_other_staff.status_code == 200
    assert manager_other_staff.json()['data']['financials_hidden'] is True
    assert 'revenue' not in manager_other_staff.json()['data']
    assert manager_revenue_daily.status_code == 403
    assert manager_bundle.status_code == 200
    manager_bundle_data = manager_bundle.json()['data']
    assert manager_bundle_data['financials_hidden'] is True
    assert 'revenue' not in manager_bundle_data['summary']
    assert manager_bundle_data['top_services'] == []
    assert manager_bundle_data['extra_services'] == []
    assert manager_bundle_data['revenue_daily'] == [
        {'date': '2025-01-10', 'appointments': 1, 'opz_qty': 0, 'opz_pct': 0.0},
        {'date': '2025-01-11', 'appointments': 1, 'opz_qty': 0, 'opz_pct': 0.0},
    ]
    assert all('revenue' not in row for row in manager_bundle_data['revenue_daily'])
    assert manager_plan_settings.status_code == 403
    assert manager_services.status_code == 403
    assert manager_service_label.status_code == 403
    assert manager_service_batch.status_code == 403
    assert manager_review_facts.status_code == 403
    assert owner_revenue_daily.status_code == 200
    assert sum(row['revenue'] for row in owner_revenue_daily.json()['data']) == 3000.0
    # branch_admin sees revenue by default (only manager/viewer are hidden out of the box).
    assert branch_admin_bundle.status_code == 200
    assert branch_admin_bundle.json()['data'].get('financials_hidden') is not True
    assert branch_admin_bundle.json()['data']['revenue_daily'] != []
    assert 'revenue' in branch_admin_bundle.json()['data']['summary']
    assert branch_admin_finance_report.status_code == 200
    assert branch_admin_operations_report.status_code == 200
    assert branch_admin_plan_settings.status_code == 200
    assert branch_admin_plan_fact.status_code == 200
    assert 'avg_check_top' in branch_admin_plan_fact.json()['data'].get('staff_leaderboards', {})


@pytest.mark.asyncio
async def test_metric_visibility_config_controls_money_metrics(async_session, monkeypatch):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(Group(id=1, title='Group'))
    async_session.add(Company(id=1, title='Branch', group_id=1))
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add_all([
        PortalUser(
            id=200,
            portal_account_id=1,
            email='owner-mv@example.com',
            password_hash=hash_password('Owner12345!'),
            role='owner',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUser(
            id=201,
            portal_account_id=1,
            email='manager-mv@example.com',
            password_hash=hash_password('Manager12345!'),
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUserBranch(user_id=201, company_id=1),
        Staff(id=1, name='Staff', company_id=1),
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Cut', amount=1, company_id=1),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    owner_headers = {'Authorization': f'Bearer {create_access_token(200, "owner")}'}
    manager_headers = {'Authorization': f'Bearer {create_access_token(201, "manager")}'}
    summary_params = {'start_date': '2025-01-01', 'end_date': '2025-01-31'}
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        manager_default = await client.get('/dashboard/widget/summary', params=summary_params, headers=manager_headers)
        manager_revenue_default = await client.get(
            '/dashboard/widget/revenue_daily', params=summary_params, headers=manager_headers
        )
        config = await client.get('/dashboard/metric-visibility', headers=owner_headers)
        manager_put_forbidden = await client.put(
            '/dashboard/metric-visibility',
            json={'role': 'manager', 'visible_codes': ['revenue']},
            headers=manager_headers,
        )
        bad_code = await client.put(
            '/dashboard/metric-visibility',
            json={'role': 'manager', 'visible_codes': ['nonsense']},
            headers=owner_headers,
        )
        bad_role = await client.put(
            '/dashboard/metric-visibility',
            json={'role': 'owner', 'visible_codes': ['revenue']},
            headers=owner_headers,
        )
        granted = await client.put(
            '/dashboard/metric-visibility',
            json={'role': 'manager', 'visible_codes': ['avg_check']},
            headers=owner_headers,
        )
        manager_after = await client.get('/dashboard/widget/summary', params=summary_params, headers=manager_headers)
        manager_revenue_after = await client.get(
            '/dashboard/widget/revenue_daily', params=summary_params, headers=manager_headers
        )

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    # Default: manager sees no money metrics.
    assert manager_default.status_code == 200
    assert manager_default.json()['data']['financials_hidden'] is True
    assert 'revenue' not in manager_default.json()['data']
    assert 'average_check' not in manager_default.json()['data']
    assert manager_revenue_default.status_code == 403

    # Config surface reports money metrics, per-role state and defaults.
    assert config.status_code == 200
    config_data = config.json()['data']
    assert {m['code'] for m in config_data['money_metrics']} == {'revenue', 'avg_check', 'cosmo_sum'}
    assert config_data['roles']['manager'] == []
    assert set(config_data['defaults']['branch_admin']) == {'revenue', 'avg_check', 'cosmo_sum'}

    # Only owner/platform_admin can configure; codes and roles are validated.
    assert manager_put_forbidden.status_code == 403
    assert bad_code.status_code == 400
    assert bad_role.status_code == 400
    assert granted.status_code == 200

    # After granting avg_check: manager sees the average check but still not revenue.
    assert manager_after.status_code == 200
    assert manager_after.json()['data']['financials_hidden'] is True
    assert 'average_check' in manager_after.json()['data']
    assert 'revenue' not in manager_after.json()['data']
    assert manager_revenue_after.status_code == 403


@pytest.mark.asyncio
async def test_dashboard_summary_matches_financial_transactions_by_external_record_id(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon 1', group_id=1),
        Company(id=2, title='Salon 2', group_id=1),
    ])
    async_session.add(
        Appointment(
            id=100,
            external_id=500,
            source_type='yclients',
            company_id=1,
            date=date(2025, 1, 10),
            attendance=1,
        )
    )
    async_session.add_all([
        FinancialTransaction(
            id=10,
            external_id=10,
            source_type='yclients',
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=500,
            sold_item_type='service',
            company_id=1,
        ),
        FinancialTransaction(
            id=11,
            external_id=11,
            source_type='yclients',
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=9999.0,
            record_id=500,
            sold_item_type='service',
            company_id=2,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['data']['revenue']['total'] == 1000.0


@pytest.mark.asyncio
async def test_dashboard_summary_revenue_and_change(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 10)))
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 12, 20),
            datetime=datetime(2024, 12, 20, 12, 0, 0),
            create_date=datetime(2024, 12, 19, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Cut', cost=1200.0, first_cost=1500.0, amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=10, service_title='Cut', cost=700.0, first_cost=900.0, amount=1, company_id=1),
        FinancialTransaction(id=1, date=datetime(2025, 1, 10, 12, 0, 0), amount=1000.0, record_id=1, visit_id=1, sold_item_id=10, sold_item_type='service', company_id=1),
        FinancialTransaction(id=2, date=datetime(2024, 12, 20, 12, 0, 0), amount=500.0, record_id=2, visit_id=2, sold_item_id=10, sold_item_type='service', company_id=1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['revenue']['total'] == 1000.0
    assert data['revenue']['change_pct'] == 100.0
    assert data['appointments_breakdown']['source_status'] == 'local'
    assert data['appointments_breakdown']['total'] == 1
    assert data['appointments_breakdown']['completed'] == 1
    assert data['appointments_breakdown']['cancelled'] == 0
    assert data['appointments_breakdown']['incomplete'] == 0
    assert data['appointments_breakdown']['attended'] == 1


@pytest.mark.asyncio
async def test_dashboard_summary_split_revenue_and_average_checks(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([
        Client(id=1, name='Client 1', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 10)),
        Client(id=2, name='Client 2', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 11)),
        Service(id=10, title='Стрижка', company_id=1),
        Service(id=11, title='Воск', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 12),
            datetime=datetime(2025, 1, 12, 12, 0, 0),
            create_date=datetime(2025, 1, 11, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Стрижка', cost=1000.0, first_cost=1000.0, amount=1, company_id=1),
        Transaction(id=2, appointment_id=1, service_id=11, service_title='Воск', cost=500.0, first_cost=500.0, amount=1, company_id=1),
        Transaction(id=3, appointment_id=2, service_id=10, service_title='Стрижка', cost=1500.0, first_cost=1500.0, amount=1, company_id=1),
        Transaction(id=4, appointment_id=1, service_id=11, service_title='Воск', cost=700.0, first_cost=700.0, amount=1, company_id=1),
        FinancialTransaction(id=1, date=datetime(2025, 1, 10, 12, 0, 0), amount=1000.0, record_id=1, visit_id=1, sold_item_id=10, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 1, 10, 12, 0, 0), amount=500.0, record_id=1, visit_id=1, sold_item_id=11, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 1, 11, 12, 0, 0), amount=1500.0, record_id=2, visit_id=2, sold_item_id=10, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=4, date=datetime(2025, 1, 10, 12, 0, 0), amount=700.0, record_id=1, visit_id=1, sold_item_id=11, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=5, date=datetime(2025, 1, 11, 12, 0, 0), amount=600.0, record_id=2, visit_id=2, sold_item_id=1, sold_item_type='goods_transaction', master_id=1, company_id=1),
        GoodTransaction(
            id=1,
            document_id=1,
            type_id=1,
            amount=1.0,
            cost=600.0,
            master_id=1,
            company_id=1,
            date=datetime(2025, 1, 11, 12, 0, 0),
        ),
        ServiceLabel(
            service_id=11,
            company_id=1,
            is_extra=True,
            source='google_sheet:services',
            updated_at=datetime(2025, 1, 1, 0, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['revenue']['total'] == 4300.0
    assert data['revenue']['service_revenue'] == 3700.0
    assert data['revenue']['goods_revenue'] == 600.0
    assert data['revenue']['extra_service_revenue'] == 1200.0
    assert data['revenue']['appointments'] == 3
    assert data['revenue']['service_count'] == 4.0
    assert data['revenue']['goods_count'] == 1.0
    assert data['revenue']['extra_service_count'] == 2.0
    assert data['revenue']['extra_service_appointments'] == 1
    assert data['revenue']['unique_clients'] == 2
    assert data['revenue']['extra_service_clients'] == 1
    assert data['average_check']['total'] == pytest.approx(1433.3333333333333)
    assert data['average_check']['services'] == pytest.approx(1233.3333333333333)
    assert data['average_check']['goods'] == 600.0
    assert data['average_check']['extra_services'] == 600.0
    assert data['visit_metrics']['extra_services_per_appointment_pct'] == pytest.approx(66.66666666666666)
    assert data['visit_metrics']['unique_clients'] == 2
    assert data['visit_metrics']['visits_per_client'] == 1.5
    assert data['visit_metrics']['extra_service_clients'] == 1
    assert data['visit_metrics']['extra_service_clients_pct'] == 50.0
    frequency = data['visit_metrics']['client_visit_frequency']
    assert frequency['total_clients'] == 2
    assert frequency['one_visit']['count'] == 1
    assert frequency['one_visit']['pct'] == 50.0
    assert frequency['two_to_three_visits']['count'] == 1
    assert frequency['two_to_three_visits']['pct'] == 50.0
    assert frequency['four_plus_visits']['count'] == 0
    assert frequency['four_plus_visits']['pct'] == 0.0


@pytest.mark.asyncio
async def test_dashboard_summary_counts_new_and_repeat_clients(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([
        Client(id=1, name='New Client', company_id=1),
        Client(id=2, name='Repeat Client', company_id=1),
        Client(id=3, name='Cancelled Client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2024, 12, 20), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2025, 1, 11), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=3, date=date(2025, 1, 12), attendance=-1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    visit_metrics = response.json()['data']['visit_metrics']
    assert visit_metrics['unique_clients'] == 2
    assert visit_metrics['new_clients'] == 1
    assert visit_metrics['new_clients_pct'] == 50.0
    assert visit_metrics['repeat_clients'] == 1
    assert visit_metrics['repeat_clients_pct'] == 50.0


@pytest.mark.asyncio
async def test_dashboard_summary_staff_filter_counts_new_clients_by_business_scope(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Selected Master', position='Барбер', company_id=1),
        Staff(id=2, name='Previous Master', position='Барбер', company_id=1),
    ])
    async_session.add_all([
        Client(id=1, name='Existing Business Client', company_id=1),
        Client(id=2, name='New Business Client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=2, client_id=1, date=date(2024, 12, 20), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 11), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2025, 1, 12), attendance=1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/widget/summary',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'staff_id': '1',
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    visit_metrics = response.json()['data']['visit_metrics']
    assert visit_metrics['unique_clients'] == 2
    assert visit_metrics['new_clients'] == 1
    assert visit_metrics['repeat_clients'] == 1


@pytest.mark.asyncio
async def test_dashboard_summary_client_visit_frequency_respects_branch_and_date_filters(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon 1', group_id=1),
        Company(id=2, title='Salon 2', group_id=1),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
    ])
    async_session.add_all([
        Client(id=1, name='Client 1', company_id=1),
        Client(id=2, name='Client 2', company_id=1),
        Client(id=3, name='Client 3', company_id=1),
        Client(id=4, name='Canceled Client', company_id=1),
        Client(id=5, name='Branch 2 Client', company_id=2),
    ])
    await async_session.flush()

    appointments = [
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=1, date=date(2025, 2, 10), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2025, 1, 11), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=2, date=date(2025, 1, 12), attendance=1),
        Appointment(id=5, company_id=1, staff_id=1, client_id=3, date=date(2025, 1, 13), attendance=1),
        Appointment(id=6, company_id=1, staff_id=1, client_id=3, date=date(2025, 1, 14), attendance=1),
        Appointment(id=7, company_id=1, staff_id=1, client_id=3, date=date(2025, 1, 15), attendance=1),
        Appointment(id=8, company_id=1, staff_id=1, client_id=3, date=date(2025, 1, 16), attendance=1),
        Appointment(id=9, company_id=1, staff_id=1, client_id=4, date=date(2025, 1, 17), attendance=-1),
        Appointment(id=10, company_id=2, staff_id=2, client_id=5, date=date(2025, 1, 18), attendance=1),
    ]
    async_session.add_all(appointments)
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        company_1_response = await client.get(
            '/dashboard/widget/summary',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
            },
        )
        company_2_response = await client.get(
            '/dashboard/widget/summary',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 2,
            },
        )
    app.dependency_overrides.clear()

    assert company_1_response.status_code == 200
    company_1_frequency = company_1_response.json()['data']['visit_metrics']['client_visit_frequency']
    assert company_1_frequency['total_clients'] == 3
    assert company_1_frequency['one_visit']['count'] == 1
    assert company_1_frequency['one_visit']['pct'] == pytest.approx(100 / 3)
    assert company_1_frequency['two_to_three_visits']['count'] == 1
    assert company_1_frequency['two_to_three_visits']['pct'] == pytest.approx(100 / 3)
    assert company_1_frequency['four_plus_visits']['count'] == 1
    assert company_1_frequency['four_plus_visits']['pct'] == pytest.approx(100 / 3)

    assert company_2_response.status_code == 200
    company_2_frequency = company_2_response.json()['data']['visit_metrics']['client_visit_frequency']
    assert company_2_frequency['total_clients'] == 1
    assert company_2_frequency['one_visit']['count'] == 1
    assert company_2_frequency['one_visit']['pct'] == 100.0
    assert company_2_frequency['two_to_three_visits']['count'] == 0
    assert company_2_frequency['four_plus_visits']['count'] == 0


@pytest.mark.parametrize(
    ('preset', 'start', 'end', 'expected_start', 'expected_end'),
    [
        # every Overview preset runs "since the start of X until today"
        ('today', date(2026, 9, 5), date(2026, 9, 5), date(2026, 8, 5), date(2026, 8, 5)),
        ('week', date(2026, 8, 31), date(2026, 9, 5), date(2026, 8, 24), date(2026, 8, 29)),
        ('month', date(2026, 9, 1), date(2026, 9, 5), date(2026, 8, 1), date(2026, 8, 5)),
        ('quarter', date(2026, 7, 1), date(2026, 9, 5), date(2026, 4, 1), date(2026, 6, 5)),
        ('year', date(2026, 1, 1), date(2026, 9, 5), date(2025, 1, 1), date(2025, 9, 5)),
        # a whole month is measured against the whole previous one, in both directions:
        # carrying the day number alone would hand February a baseline of 1-28 January
        # and report the three dropped days as growth
        ('month', date(2026, 3, 1), date(2026, 3, 31), date(2026, 2, 1), date(2026, 2, 28)),
        ('month', date(2026, 2, 1), date(2026, 2, 28), date(2026, 1, 1), date(2026, 1, 31)),
        ('month', date(2026, 6, 1), date(2026, 6, 30), date(2026, 5, 1), date(2026, 5, 31)),
        ('month', date(2026, 1, 1), date(2026, 1, 31), date(2025, 12, 1), date(2025, 12, 31)),
        ('quarter', date(2026, 4, 1), date(2026, 6, 30), date(2026, 1, 1), date(2026, 3, 31)),
        ('year', date(2026, 1, 1), date(2026, 12, 31), date(2025, 1, 1), date(2025, 12, 31)),
        # a single day keeps its day number: month-end there is a coincidence, not a span
        ('today', date(2026, 2, 28), date(2026, 2, 28), date(2026, 1, 28), date(2026, 1, 28)),
        ('month', date(2028, 3, 1), date(2028, 3, 31), date(2028, 2, 1), date(2028, 2, 29)),
        ('today', date(2026, 3, 31), date(2026, 3, 31), date(2026, 2, 28), date(2026, 2, 28)),
        ('month', date(2026, 1, 1), date(2026, 1, 5), date(2025, 12, 1), date(2025, 12, 5)),
        # dates typed by hand keep the plain window of equal length immediately before, so
        # the reports page cannot render one delta against these dates and another against
        # the window its own compare field pre-fills
        (None, date(2026, 9, 1), date(2026, 9, 5), date(2026, 8, 27), date(2026, 8, 31)),
        (None, date(2026, 11, 1), date(2026, 11, 30), date(2026, 10, 2), date(2026, 10, 31)),
        (None, date(2026, 8, 20), date(2026, 9, 10), date(2026, 7, 29), date(2026, 8, 19)),
        (None, date(2026, 3, 25), date(2026, 3, 31), date(2026, 3, 18), date(2026, 3, 24)),
        (None, date(2026, 3, 29), date(2026, 3, 31), date(2026, 3, 26), date(2026, 3, 28)),
        (None, date(2026, 5, 25), date(2026, 5, 31), date(2026, 5, 18), date(2026, 5, 24)),
        (None, date(2026, 8, 10), date(2026, 8, 14), date(2026, 8, 5), date(2026, 8, 9)),
        # a preset that does not describe the window it arrived with cannot make the
        # baseline overlap the period, which would compare the period against itself
        ('today', date(2026, 1, 1), date(2026, 12, 31), date(2025, 1, 1), date(2025, 12, 31)),
    ],
)
def test_previous_period_steps_back_by_the_presets_own_unit(
    preset, start, end, expected_start, expected_end,
):
    period = dashboard_service.DateRange(start=start, end=end)
    previous = period.previous_period(preset)
    assert (previous.start, previous.end) == (expected_start, expected_end)
    assert previous.end < start, 'the baseline must not overlap the period it is measured against'
    if preset not in {'month', 'quarter', 'year'}:
        # only calendar-anchored presets may legitimately differ in length, because the
        # month or quarter they step onto is genuinely shorter
        assert previous.days == period.days, 'the baseline must not be shorter than the period'
    whole_months = start.day == 1 and end.day == monthrange(end.year, end.month)[1]
    if whole_months and preset in {'month', 'quarter', 'year'}:
        assert previous.start.day == 1
        assert previous.end.day == monthrange(previous.end.year, previous.end.month)[1], (
            'a window of whole months must be measured against whole months'
        )


@pytest.mark.asyncio
async def test_dashboard_summary_compares_month_to_date_with_the_same_dates_a_month_back(
    async_session,
):
    """1-5 September weighs against 1-5 August, not against the tail of August."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client', company_id=1))
    await async_session.flush()

    async_session.add_all([
        # the window the new baseline points at: two visits
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2026, 8, 3), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=1, date=date(2026, 8, 4), attendance=1),
        # the end of August, which the old baseline used: eight visits
        *(
            Appointment(
                id=10 + offset, company_id=1, staff_id=1, client_id=1,
                date=date(2026, 8, 27) + timedelta(days=offset // 2), attendance=1,
            )
            for offset in range(8)
        ),
        # the period itself: four visits
        *(
            Appointment(
                id=30 + offset, company_id=1, staff_id=1, client_id=1,
                date=date(2026, 9, 1) + timedelta(days=offset), attendance=1,
            )
            for offset in range(4)
        ),
    ])
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2026, 9, 1),
        date(2026, 9, 5),
        company_id=1,
        include_appointments_breakdown=False,
        factual_at=datetime(2026, 9, 5, 12),
        period_preset='month',
    )
    assert summary['previous_period'] == {'start': '2026-08-01', 'end': '2026-08-05'}
    # four visits against the two of 1-5 August, not against the eight of 27-31 August
    assert summary['revenue']['appointments'] == 4
    assert summary['revenue']['appointments_change_pct'] == 100.0


@pytest.mark.asyncio
async def test_new_vs_returning_report_uses_the_same_baseline_as_the_overview_card(async_session):
    """The Overview's new/repeat cards link here, so both must measure the same way."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([Client(id=n, name=f'Client {n}', company_id=1) for n in range(1, 6)])
    await async_session.flush()

    # The week preset's baseline (24-29 Aug) and the plain preceding window (25-30 Aug)
    # differ by a single day, so 24 August is what separates the two rules.
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2026, 8, 24), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2026, 8, 27), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=3, date=date(2026, 9, 1), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=4, date=date(2026, 9, 2), attendance=1),
        Appointment(id=5, company_id=1, staff_id=1, client_id=5, date=date(2026, 9, 3), attendance=1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    period = {'start_date': '2026-08-31', 'end_date': '2026-09-05', 'period_preset': 'week'}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        overview = await client.get('/dashboard/widget/summary', params=period)
        report = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'new_vs_returning_cross', 'granularity': 'month', **period},
        )
    app.dependency_overrides.clear()

    assert overview.status_code == 200
    assert report.status_code == 200
    visits = overview.json()['data']['visit_metrics']
    segments = {row['segment']: row for row in report.json()['data']['raw']['segments']}
    assert segments['Новые']['clients'] == visits['new_clients']
    assert segments['Новые']['clients_change_pct'] == visits['new_clients_change_pct']
    assert segments['Повторные']['clients_change_pct'] == visits['repeat_clients_change_pct']
    # and the shared baseline is the preset's, not the plain preceding window
    assert overview.json()['data']['previous_period'] == {'start': '2026-08-24', 'end': '2026-08-29'}


@pytest.mark.asyncio
async def test_overview_and_clients_report_bucket_visits_the_same_way(async_session):
    """AGENTS.md makes this a both-paths rule: two implementations, one answer."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([Client(id=n, name=f'Client {n}', company_id=1) for n in range(1, 4)])
    await async_session.flush()

    appointments = [
        # one visit ever
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2026, 3, 5), attendance=1),
        # two: one before the period, one inside it
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2025, 11, 4), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2026, 3, 6), attendance=1),
    ]
    # four: three before the period, one inside it
    appointments += [
        Appointment(
            id=10 + offset, company_id=1, staff_id=1, client_id=3,
            date=date(2025, 8, 1) + timedelta(days=30 * offset), attendance=1,
        )
        for offset in range(3)
    ]
    appointments.append(
        Appointment(id=20, company_id=1, staff_id=1, client_id=3, date=date(2026, 3, 7), attendance=1)
    )
    async_session.add_all(appointments)
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    window = {'start_date': '2026-03-01', 'end_date': '2026-03-31'}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        overview = await client.get('/dashboard/widget/summary', params=window)
        report = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'top_clients_pareto', 'granularity': 'month', **window},
        )
    app.dependency_overrides.clear()

    assert overview.status_code == 200
    assert report.status_code == 200
    frequency = overview.json()['data']['visit_metrics']['client_visit_frequency']
    buckets = {row['bucket']: row['clients'] for row in report.json()['data']['raw']['visit_frequency']}
    assert (frequency['one_visit']['count'], frequency['two_to_three_visits']['count'],
            frequency['four_plus_visits']['count']) == (1, 1, 1)
    assert buckets['1 визит'] == frequency['one_visit']['count']
    assert buckets['2-3 визита'] == frequency['two_to_three_visits']['count']
    assert buckets['4+ визита'] == frequency['four_plus_visits']['count']


@pytest.mark.asyncio
async def test_bundle_and_summary_read_the_period_preset_the_same_way(async_session):
    """The bundle is what the Overview actually calls, and an empty preset means absent."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    await async_session.flush()
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    window = {'start_date': '2026-09-01', 'end_date': '2026-09-05'}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        bundle = await client.get('/dashboard/bundle', params={**window, 'period_preset': 'month'})
        summary = await client.get(
            '/dashboard/widget/summary', params={**window, 'period_preset': 'month'}
        )
        # Query() must stay inside the Annotated, or a bare `period_preset=` 422s the view
        blank = await client.get('/dashboard/widget/summary', params={**window, 'period_preset': ''})
        absent = await client.get('/dashboard/widget/summary', params=window)
        typo = await client.get('/dashboard/widget/summary', params={**window, 'period_preset': 'MONTH'})
    app.dependency_overrides.clear()

    assert bundle.status_code == 200
    assert summary.status_code == 200
    month_baseline = {'start': '2026-08-01', 'end': '2026-08-05'}
    assert bundle.json()['data']['summary']['previous_period'] == month_baseline
    assert summary.json()['data']['previous_period'] == month_baseline

    assert blank.status_code == 200
    assert absent.status_code == 200
    assert blank.json()['data']['previous_period'] == absent.json()['data']['previous_period']
    assert blank.json()['data']['previous_period'] == {'start': '2026-08-27', 'end': '2026-08-31'}
    assert typo.status_code == 422


@pytest.mark.asyncio
async def test_explicit_compare_window_overrides_the_preset_in_reports(async_session):
    """One screen, one metric: the table's delta and the comparison block must agree."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([Client(id=n, name=f'Client {n}', company_id=1) for n in range(1, 6)])
    await async_session.flush()

    async_session.add_all([
        # only in the preset's baseline (24-29 Aug), not in the window ticked below
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2026, 8, 24), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2026, 8, 27), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=3, date=date(2026, 9, 1), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=4, date=date(2026, 9, 2), attendance=1),
        Appointment(id=5, company_id=1, staff_id=1, client_id=5, date=date(2026, 9, 3), attendance=1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    params = {
        'report_id': 'new_vs_returning_cross',
        'granularity': 'month',
        'start_date': '2026-08-31',
        'end_date': '2026-09-05',
        'period_preset': 'week',
    }
    compare = {'compare_start_date': '2026-08-27', 'compare_end_date': '2026-08-30'}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        without_compare = await client.get('/dashboard/reports/data', params=params)
        with_compare = await client.get('/dashboard/reports/data', params={**params, **compare})
        plain = await client.get(
            '/dashboard/reports/data',
            params={**{k: v for k, v in params.items() if k != 'period_preset'}, **compare},
        )
    app.dependency_overrides.clear()

    assert without_compare.status_code == 200
    assert with_compare.status_code == 200
    delta = lambda r: {  # noqa: E731
        row['segment']: row['clients_change_pct'] for row in r.json()['data']['raw']['segments']
    }
    # the preset still rules when nothing else was asked for...
    assert delta(without_compare)['Новые'] == 50.0
    # ...but an explicit window takes over, so the table cannot contradict the block beside it
    assert delta(with_compare) == delta(plain)
    assert delta(with_compare)['Новые'] != delta(without_compare)['Новые']


@pytest.mark.asyncio
async def test_dashboard_summary_client_visit_frequency_counts_branch_history(async_session):
    """Buckets follow the whole branch history, so a regular never reads as a first-timer."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add_all([
        Client(id=1, name='First timer', company_id=1),
        Client(id=2, name='Returning', company_id=1),
        Client(id=3, name='Regular', company_id=1),
        Client(id=4, name='Absent in period', company_id=1),
    ])
    await async_session.flush()

    async_session.add_all([
        # one visit ever, inside the period
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 3, 5), attendance=1),
        # one earlier visit plus one in the period
        Appointment(id=2, company_id=1, staff_id=1, client_id=2, date=date(2024, 11, 4), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=2, date=date(2025, 3, 6), attendance=1),
        # three earlier visits plus one in the period
        Appointment(id=4, company_id=1, staff_id=1, client_id=3, date=date(2024, 8, 1), attendance=1),
        Appointment(id=5, company_id=1, staff_id=1, client_id=3, date=date(2024, 9, 2), attendance=1),
        Appointment(id=6, company_id=1, staff_id=1, client_id=3, date=date(2024, 10, 3), attendance=1),
        Appointment(id=7, company_id=1, staff_id=1, client_id=3, date=date(2025, 3, 7), attendance=1),
        # later visits must not promote the first-timer out of the one-visit bucket
        Appointment(id=8, company_id=1, staff_id=1, client_id=1, date=date(2025, 4, 9), attendance=1),
        # history alone does not put a client into the period's base
        Appointment(id=9, company_id=1, staff_id=1, client_id=4, date=date(2024, 12, 12), attendance=1),
    ])
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 3, 1),
        date(2025, 3, 31),
        company_id=1,
        include_appointments_breakdown=False,
    )
    frequency = summary['visit_metrics']['client_visit_frequency']
    assert frequency['total_clients'] == 3
    assert frequency['one_visit']['count'] == 1
    assert frequency['two_to_three_visits']['count'] == 1
    assert frequency['four_plus_visits']['count'] == 1
    buckets = ('one_visit', 'two_to_three_visits', 'four_plus_visits')
    assert sum(frequency[key]['count'] for key in buckets) == frequency['total_clients']
    assert sum(frequency[key]['pct'] for key in buckets) == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_average_check_uses_cash_income_and_business_denominator(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Client 1', company_id=1),
        Client(id=2, name='Client 2', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        AccountCatalog(
            company_id=1, account_id=1, title='Наличные', type=1,
            updated_at=datetime(2025, 1, 1),
        ),
        AccountCatalog(
            company_id=1, account_id=2, title='Бонусы', type=3,
            updated_at=datetime(2025, 1, 1),
        ),
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 5), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 6), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=None, date=date(2025, 1, 7), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=None, date=date(2025, 1, 8), attendance=1),
        Appointment(id=5, company_id=1, staff_id=1, client_id=2, date=date(2025, 1, 9), attendance=0),
        Appointment(id=6, company_id=1, staff_id=1, client_id=2, date=date(2025, 2, 1), attendance=1),
        Appointment(id=7, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 14), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(
            id=1, company_id=1, account_id=1, record_id=1, master_id=1,
            date=datetime(2025, 1, 5, 12), amount=100, sold_item_type='service',
        ),
        FinancialTransaction(
            id=2, company_id=1, account_id=1, record_id=2, master_id=1,
            date=datetime(2025, 1, 6, 12), amount=200, sold_item_type='service',
        ),
        FinancialTransaction(
            id=3, company_id=1, account_id=1, record_id=5, master_id=1,
            date=datetime(2025, 1, 9, 12), amount=500, sold_item_type='service',
        ),
        FinancialTransaction(
            id=4, company_id=1, account_id=1, record_id=1, master_id=1,
            date=datetime(2025, 1, 10, 12), amount=-50, sold_item_type='service',
        ),
        FinancialTransaction(
            id=5, company_id=1, account_id=2, record_id=1, master_id=1,
            date=datetime(2025, 1, 10, 12), amount=900, sold_item_type='service',
        ),
        FinancialTransaction(
            id=6, company_id=1, account_id=1, record_id=6, master_id=1,
            date=datetime(2025, 1, 15, 12), amount=400, sold_item_type='service',
        ),
        FinancialTransaction(
            id=7, company_id=1, account_id=1, master_id=1,
            date=datetime(2025, 1, 11, 12), amount=300,
            sold_item_type='goods_transaction', document_id=50,
        ),
        FinancialTransaction(
            id=8, company_id=1, account_id=1, client_id=1,
            date=datetime(2025, 1, 12, 12), amount=250,
            expense_title='Пополнение личного счета',
        ),
        FinancialTransaction(
            id=9, company_id=1, account_id=1,
            date=datetime(2025, 1, 13, 12), amount=75,
            expense_title='Прочий приход',
        ),
        GoodTransaction(
            id=1, company_id=1, master_id=1, document_id=50, type_id=1,
            good_id=1, amount=-1, date=datetime(2025, 1, 11, 12),
        ),
        GoodTransaction(
            id=2, company_id=1, master_id=1, document_id=50, type_id=1,
            good_id=2, amount=-2, date=datetime(2025, 1, 11, 12),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        partial = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        async_session.add(
            SyncSourceState(
                company_id=1,
                source='financial_transactions_detail',
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                synced_at=datetime(2025, 2, 1),
            )
        )
        await async_session.commit()
        ready = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert partial.status_code == 200
    partial_avg = partial.json()['data']['average_check']
    assert partial_avg['source_status'] == 'partial'
    assert partial_avg['missing_components'] == ['personal_account_topups']

    avg = ready.json()['data']['average_check']
    assert avg['source_status'] == 'ready'
    assert avg['service_revenue'] == 700.0
    assert avg['goods_revenue'] == 300.0
    assert avg['topup_revenue'] == 250.0
    assert avg['completed_appointments'] == 5
    assert avg['unique_clients'] == 1
    assert avg['appointments_without_client'] == 2
    assert avg['goods_checks'] == 1
    assert avg['numerator'] == 1250.0
    assert avg['denominator'] == 5
    assert avg['total'] == 250.0
    assert avg['formula'] == 'income / completed_appointments'
    assert avg['unclassified_operations'] == 1


@pytest.mark.asyncio
async def test_summary_excludes_placeholder_admin_appointments_from_revenue_and_avg_check(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Staff(id=2, name='Администратор Ривьера', position='Администратор', company_id=1),
        Client(id=1, name='Client 1', company_id=1),
        Client(id=2, name='Client 2', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 5), attendance=1),
        Appointment(id=2, company_id=1, staff_id=2, client_id=2, date=date(2025, 1, 5), attendance=1),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(
            id=1, company_id=1, record_id=1, master_id=1,
            date=datetime(2025, 1, 5, 12), amount=1000, sold_item_type='service',
        ),
        FinancialTransaction(
            id=2, company_id=1, record_id=2, master_id=2,
            date=datetime(2025, 1, 5, 13), amount=9000, sold_item_type='service',
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        summary_response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        daily_response = await client.get(
            '/dashboard/widget/revenue_daily',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert summary_response.status_code == 200
    summary = summary_response.json()['data']
    assert summary['revenue']['total'] == 1000.0
    assert summary['revenue']['appointments'] == 1
    assert summary['average_check']['numerator'] == 1000.0
    assert summary['average_check']['denominator'] == 1
    assert summary['average_check']['total'] == 1000.0

    assert daily_response.status_code == 200
    daily = daily_response.json()['data']
    assert daily == [{
        'date': '2025-01-05',
        'revenue': 1000.0,
        'service_revenue': 1000.0,
        'goods_revenue': 0.0,
        'topup_revenue': 0.0,
        'appointments': 1,
        'opz_qty': 0,
        'opz_pct': 0.0,
    }]


@pytest.mark.asyncio
async def test_plan_fact_network_avg_check_uses_completed_appointments_across_branches(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon 1', group_id=1),
        Company(id=2, title='Salon 2', group_id=1),
        Staff(id=1, name='Master 1', position='Барбер', company_id=1),
        Staff(id=2, name='Master 2', position='Барбер', company_id=2),
        Client(id=1, name='Shared client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1, company_id=1, staff_id=1, client_id=1,
            date=date(2025, 1, 10), attendance=1,
        ),
        Appointment(
            id=2, company_id=2, staff_id=2, client_id=1,
            date=date(2025, 1, 11), attendance=1,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        FinancialTransaction(
            id=1, company_id=1, master_id=1, record_id=1,
            date=datetime(2025, 1, 10, 12), amount=500,
            sold_item_type='service',
        ),
        FinancialTransaction(
            id=2, company_id=2, master_id=2, record_id=2,
            date=datetime(2025, 1, 11, 12), amount=1000,
            sold_item_type='service',
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
    )
    network = next(group for group in result['groups'] if group['scope'] == 'network')
    cells = {cell['code']: cell for cell in network['metrics']}
    assert cells['revenue']['fact'] == 1500.0
    assert cells['avg_check_total']['fact'] == 750.0


@pytest.mark.asyncio
async def test_dashboard_top_services_merges_same_service_name_across_branches(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add(Staff(id=1, name='Master 1', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Master 2', position='Барбер', company_id=2))
    async_session.add(Client(id=1, name='Client 1', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 10)))
    async_session.add(Client(id=2, name='Client 2', company_id=2, visits_count=1, last_visit_date=date(2025, 1, 11)))
    async_session.add_all([
        Service(id=10, title='Black Mask', company_id=1),
        Service(id=20, title='Black Mask', company_id=2),
        Service(id=30, title='Комплексное мытьё головы', company_id=1),
        Service(id=40, title='Комплексное мытье головы', company_id=2),
        Service(id=50, title='Стрижка', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=2,
            staff_id=2,
            client_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Black Mask', cost=100.0, first_cost=100.0, amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=20, service_title='Black Mask', cost=200.0, first_cost=200.0, amount=2, company_id=2),
        Transaction(id=3, appointment_id=1, service_id=30, service_title='Комплексное мытьё головы', cost=50.0, first_cost=50.0, amount=1, company_id=1),
        Transaction(id=4, appointment_id=2, service_id=40, service_title='Комплексное мытье головы', cost=75.0, first_cost=75.0, amount=1, company_id=2),
        Transaction(id=5, appointment_id=1, service_id=50, service_title='Стрижка', cost=80.0, first_cost=80.0, amount=1, company_id=1),
        FinancialTransaction(id=1, date=datetime(2025, 1, 10, 12, 0, 0), amount=100.0, record_id=1, visit_id=1, sold_item_id=10, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 1, 11, 12, 0, 0), amount=400.0, record_id=2, visit_id=2, sold_item_id=20, sold_item_type='service', master_id=2, company_id=2),
        FinancialTransaction(id=3, date=datetime(2025, 1, 10, 12, 0, 0), amount=50.0, record_id=1, visit_id=1, sold_item_id=30, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=4, date=datetime(2025, 1, 11, 12, 0, 0), amount=75.0, record_id=2, visit_id=2, sold_item_id=40, sold_item_type='service', master_id=2, company_id=2),
        FinancialTransaction(id=5, date=datetime(2025, 1, 10, 12, 0, 0), amount=80.0, record_id=1, visit_id=1, sold_item_id=50, sold_item_type='service', master_id=1, company_id=1),
        ServiceLabel(service_id=10, company_id=1, is_extra=True, source='google_sheet:services', updated_at=datetime(2025, 1, 1, 0, 0, 0)),
        ServiceLabel(service_id=20, company_id=2, is_extra=True, source='google_sheet:services', updated_at=datetime(2025, 1, 1, 0, 0, 0)),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/top_services',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        r_extra = await client.get(
            '/dashboard/widget/extra_services',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    rows = r.json()['data']
    assert len(rows) == 3

    black_mask = next(row for row in rows if row['title'] == 'Black Mask')
    assert black_mask['sold'] == 3
    assert black_mask['revenue'] == 500.0
    assert black_mask['service_count'] == 2
    assert black_mask['branch_count'] == 2

    wash = next(row for row in rows if row['title'].replace('ё', 'е') == 'Комплексное мытье головы')
    assert wash['sold'] == 2
    assert wash['revenue'] == 125.0
    assert wash['service_count'] == 2
    assert wash['branch_count'] == 2

    assert r_extra.status_code == 200
    extra_rows = r_extra.json()['data']
    assert len(extra_rows) == 1
    assert extra_rows[0]['title'] == 'Black Mask'
    assert extra_rows[0]['sold'] == 3
    assert extra_rows[0]['revenue'] == 500.0
    assert extra_rows[0]['service_count'] == 2
    assert extra_rows[0]['branch_count'] == 2


@pytest.mark.asyncio
async def test_dashboard_bundle_returns_all_extra_services_without_default_limit(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client', company_id=1))
    await async_session.flush()
    async_session.add(
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        )
    )
    for index in range(55):
        service_id = 1000 + index
        async_session.add(Service(id=service_id, title=f'Extra {index}', category_title='Уход', company_id=1))
        async_session.add(
            ServiceLabel(
                service_id=service_id,
                company_id=1,
                is_extra=True,
                source='test',
                updated_at=datetime(2025, 1, 1, 0, 0, 0),
            )
        )
        async_session.add(
            Transaction(
                id=index + 1,
                appointment_id=1,
                service_id=service_id,
                service_title=f'Extra {index}',
                cost=100.0 + index,
                first_cost=100.0 + index,
                amount=1,
                company_id=1,
            )
        )
        async_session.add(
            FinancialTransaction(
                id=index + 1,
                date=datetime(2025, 1, 10, 12, 0, 0),
                amount=100.0 + index,
                record_id=1,
                visit_id=1,
                sold_item_id=service_id,
                sold_item_type='service',
                master_id=1,
                company_id=1,
            )
        )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        bundle = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        limited = await client.get(
            '/dashboard/widget/extra_services',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'limit': 10},
        )
    app.dependency_overrides.clear()

    assert bundle.status_code == 200
    assert len(bundle.json()['data']['extra_services']) == 55
    assert limited.status_code == 200
    assert len(limited.json()['data']) == 10


@pytest.mark.asyncio
async def test_extra_service_labels_are_scoped_to_branch_in_calculations(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add(Staff(id=1, name='Master 1', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Master 2', position='Барбер', company_id=2))
    async_session.add(Client(id=1, name='Client 1', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 10)))
    async_session.add(Client(id=2, name='Client 2', company_id=2, visits_count=1, last_visit_date=date(2025, 1, 11)))
    async_session.add(Service(id=10, title='Branch-only extra', category_title='Уход', company_id=1))
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=2,
            staff_id=2,
            client_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Branch-only extra', cost=100.0, first_cost=100.0, amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=10, service_title='Branch-only extra', cost=200.0, first_cost=200.0, amount=1, company_id=2),
        FinancialTransaction(id=1, date=datetime(2025, 1, 10, 12, 0, 0), amount=100.0, record_id=1, visit_id=1, sold_item_id=10, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 1, 11, 12, 0, 0), amount=200.0, record_id=2, visit_id=2, sold_item_id=10, sold_item_type='service', master_id=2, company_id=2),
        ServiceLabel(service_id=10, company_id=1, is_extra=True, source='google_sheet:services', updated_at=datetime(2025, 1, 1, 0, 0, 0)),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        all_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        branch_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        other_branch_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 2},
        )
        extra_services = await client.get(
            '/dashboard/widget/extra_services',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    all_data = all_summary.json()['data']
    branch_data = branch_summary.json()['data']
    other_branch_data = other_branch_summary.json()['data']

    assert all_summary.status_code == 200
    assert branch_summary.status_code == 200
    assert other_branch_summary.status_code == 200
    assert all_data['revenue']['service_revenue'] == 300.0
    assert all_data['revenue']['extra_service_revenue'] == 100.0
    assert all_data['revenue']['extra_service_count'] == 1.0
    assert branch_data['revenue']['extra_service_revenue'] == 100.0
    assert branch_data['revenue']['extra_service_count'] == 1.0
    assert other_branch_data['revenue']['extra_service_revenue'] == 0.0
    assert other_branch_data['revenue']['extra_service_count'] == 0.0

    assert extra_services.status_code == 200
    extra_rows = extra_services.json()['data']
    assert len(extra_rows) == 1
    assert extra_rows[0]['sold'] == 1
    assert extra_rows[0]['revenue'] == 100.0
    assert extra_rows[0]['branch_count'] == 1


@pytest.mark.asyncio
async def test_dashboard_branches_respects_portal_allowlist(async_session, monkeypatch):
    import dashboard_service

    async_session.add(Group(id=1, title='G'))
    async_session.add(Company(id=1, title='A', group_id=1))
    async_session.add(Company(id=2, title='B', group_id=1))
    await async_session.commit()

    async def fake_ids(_db):
        return [2]

    monkeypatch.setattr(dashboard_service, 'branch_company_ids', fake_ids)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get('/dashboard/branches')
    app.dependency_overrides.clear()

    assert r.status_code == 200
    rows = r.json()['data']
    assert len(rows) == 1
    assert rows[0]['id'] == 2


@pytest.mark.asyncio
async def test_dashboard_branch_scope_applies_to_plan_settings_write(async_session, monkeypatch):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    now = datetime(2025, 1, 1, 0, 0, 0)
    async_session.add(Group(id=1, title='G'))
    async_session.add_all([
        Company(id=1, title='Allowed', group_id=1),
        Company(id=2, title='Forbidden', group_id=1),
        Staff(id=200, name='Forbidden staff', position='Администратор', company_id=2),
        PortalAccount(id=1, label='Tenant', created_at=now),
        PortalBranch(portal_account_id=1, company_id=1),
        PortalUser(
            id=700,
            portal_account_id=1,
            email='owner-scope@example.com',
            password_hash=hash_password('OwnerScope123!'),
            role='owner',
            is_active=True,
            email_verified_at=now,
            created_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(700, 'owner')
    headers = {'Authorization': f'Bearer {token}'}
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        branches = await client.get('/dashboard/branches', headers=headers)
        allowed_save = await client.post(
            '/dashboard/plan/settings',
            headers=headers,
            json={'month': '2025-05', 'branches': [{'company_id': 1, 'wax_pct': 10}], 'staff': []},
        )
        forbidden_save = await client.post(
            '/dashboard/plan/settings',
            headers=headers,
            json={'month': '2025-05', 'branches': [{'company_id': 2, 'wax_pct': 10}], 'staff': []},
        )
        forbidden_staff_save = await client.post(
            '/dashboard/plan/settings',
            headers=headers,
            json={
                'month': '2025-05',
                'branches': [{'company_id': 1, 'wax_pct': 10}],
                'staff': [{
                    'company_id': 2,
                    'staff_id': 200,
                    'staff_category': 'administrator',
                    'extra_services_qty': 5,
                }],
            },
        )
    app.dependency_overrides.clear()

    assert branches.status_code == 200
    assert [row['id'] for row in branches.json()['data']] == [1]
    assert allowed_save.status_code == 200
    assert forbidden_save.status_code == 400
    assert forbidden_save.json()['detail'] == 'company is not allowed: 2'
    assert forbidden_staff_save.status_code == 400
    assert forbidden_staff_save.json()['detail'] == 'staff company is not included in branches: 2'

    forbidden_setting = await async_session.scalar(
        select(PlanBranchSetting).where(PlanBranchSetting.company_id == 2)
    )
    assert forbidden_setting is None
    forbidden_staff_input = await async_session.scalar(
        select(PlanStaffInput).where(PlanStaffInput.company_id == 2)
    )
    assert forbidden_staff_input is None


@pytest.mark.asyncio
async def test_dashboard_staff_filter_excludes_waitlist_and_fired_staff(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add(Staff(id=10, name='Active', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=20, name='Fired', position='Барбер', company_id=1, fired=1))
    async_session.add(Staff(id=30, name='Лист ожидания', position='Системный', company_id=1, fired=0))
    async_session.add(Staff(id=40, name='Admin', position='Администратор', company_id=2, fired=0))
    async_session.add(Staff(id=50, name='Администратор Ривьера', position='Администратор', company_id=2, fired=0))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        all_staff = await client.get('/dashboard/staff')
        branch_staff = await client.get('/dashboard/staff', params={'company_id': 1})
    app.dependency_overrides.clear()

    assert all_staff.status_code == 200
    assert [row['name'] for row in all_staff.json()['data']] == ['Active', 'Admin']
    assert branch_staff.status_code == 200
    assert [row['name'] for row in branch_staff.json()['data']] == ['Active']


@pytest.mark.asyncio
async def test_dashboard_bundle_filters_by_staff(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master 1', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Master 2', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client 1', company_id=1))
    async_session.add(Client(id=2, name='Client 2', company_id=1))
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=2,
            client_id=2,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 14, 0, 0),
            create_date=datetime(2025, 1, 9, 14, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 10),
            datetime=datetime(2025, 2, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 18, 0, 0),
            seance_length=3600,
            attendance=0,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Cut 1', cost=1000.0, first_cost=1000.0, amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=20, service_title='Cut 2', cost=2000.0, first_cost=2000.0, amount=1, company_id=1),
        FinancialTransaction(id=1, date=datetime(2025, 1, 10, 12, 0, 0), amount=1000.0, record_id=1, visit_id=1, sold_item_id=10, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 1, 10, 14, 0, 0), amount=2000.0, record_id=2, visit_id=2, sold_item_id=20, sold_item_type='service', master_id=2, company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 1, 10, 13, 0, 0), amount=300.0, record_id=1, visit_id=1, sold_item_id=1, sold_item_type='goods_transaction', master_id=None, company_id=1),
        FinancialTransaction(id=4, date=datetime(2025, 1, 10, 15, 0, 0), amount=700.0, record_id=2, visit_id=2, sold_item_id=2, sold_item_type='goods_transaction', master_id=None, company_id=1),
        GoodTransaction(
            id=1,
            document_id=1,
            type_id=1,
            amount=-1.0,
            cost=300.0,
            master_id=1,
            company_id=1,
            date=datetime(2025, 1, 10, 13, 0, 0),
        ),
        GoodTransaction(
            id=2,
            document_id=2,
            type_id=1,
            amount=-1.0,
            cost=700.0,
            master_id=2,
            company_id=1,
            date=datetime(2025, 1, 10, 15, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/bundle',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'staff_id': 1,
            },
        )
        branch_response = await client.get(
            '/dashboard/bundle',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
            },
        )
        second_staff_response = await client.get(
            '/dashboard/bundle',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'staff_id': 2,
            },
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['summary']['revenue']['total'] == 1300.0
    assert data['summary']['revenue']['appointments'] == 1
    assert data['summary']['visit_metrics']['opz_qty'] == 1.0
    assert data['summary']['visit_metrics']['opz_pct'] == 100.0
    assert data['revenue_daily'] == [
        {
            'date': '2025-01-10',
            'revenue': 1300.0,
                'service_revenue': 1000.0,
                'goods_revenue': 300.0,
                'topup_revenue': 0.0,
                'appointments': 1,
                'opz_qty': 1,
                'opz_pct': 100.0,
            }
        ]
    assert [row['title'] for row in data['top_services']] == ['Cut 1']
    branch_goods_revenue = branch_response.json()['data']['summary']['revenue']['goods_revenue']
    second_staff_goods_revenue = second_staff_response.json()['data']['summary']['revenue']['goods_revenue']
    assert data['summary']['revenue']['goods_revenue'] == 300.0
    assert second_staff_goods_revenue == 700.0
    assert data['summary']['revenue']['goods_revenue'] + second_staff_goods_revenue == branch_goods_revenue


@pytest.mark.asyncio
async def test_goods_revenue_uses_stock_operation_seller_not_appointment_master(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Service master', position='Барбер', company_id=1),
        Staff(id=2, name='Seller', position='Администратор', company_id=1, user_id=500),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            external_id=101,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12),
            attendance=1,
        ),
        Appointment(
            id=2,
            external_id=102,
            company_id=1,
            staff_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12),
            attendance=1,
        ),
        GoodTransaction(
            id=10,
            external_id=501,
            company_id=1,
            type_id=1,
            amount=-1,
            cost=3600,
            master_id=2,
            date=datetime(2025, 1, 10, 12),
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            record_id=101,
            sold_item_id=501,
            sold_item_type='goods_transaction',
            amount=3600,
            date=datetime(2025, 1, 10, 12),
        ),
        # A payment without its stock operation stays in the branch total but must
        # not be credited to the appointment master.
        FinancialTransaction(
            id=2,
            company_id=1,
            record_id=102,
            sold_item_id=999,
            sold_item_type='goods_transaction',
            amount=700,
            date=datetime(2025, 1, 11, 12),
        ),
    ])
    await async_session.commit()

    service_master = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=1,
    )
    seller = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=2,
    )
    branch = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
    )
    plan_fact = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        include_all_staff_in_leaderboards=True,
    )
    seller_group = next(group for group in plan_fact['groups'] if group['staff_id'] == 2)
    seller_metrics = {cell['code']: cell for cell in seller_group['metrics']}

    assert service_master['revenue']['goods_revenue'] == 0.0
    assert seller['revenue']['goods_revenue'] == 3600.0
    assert seller['revenue']['total'] == 3600.0
    assert branch['revenue']['goods_revenue'] == 4300.0
    assert seller_metrics['revenue']['fact'] == 3600.0


@pytest.mark.asyncio
async def test_dashboard_plan_fact_uses_plan_and_fact_formulas(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 10)))
    async_session.add_all([
        Service(id=10, title='воск', company_id=1),
        Service(id=11, title='камуфляж', company_id=1),
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
        ServiceLabel(
            service_id=11,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 10),
            datetime=datetime(2025, 2, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 18, 0, 0),
            seance_length=3600,
            attendance=0,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 12),
            datetime=datetime(2025, 1, 12, 12, 0, 0),
            create_date=datetime(2025, 1, 11, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
    ])
    await async_session.flush()

    async_session.add_all([
        Transaction(
            id=1,
            appointment_id=1,
            service_id=10,
            service_title='воск',
            cost=1000.0,
            first_cost=1000.0,
            amount=1,
            company_id=1,
        ),
        Transaction(
            id=2,
            appointment_id=1,
            service_id=11,
            service_title='камуфляж',
            cost=500.0,
            first_cost=500.0,
            amount=2,
            company_id=1,
        ),
        Transaction(
            id=3,
            appointment_id=3,
            service_id=12,
            service_title='стрижка',
            cost=500.0,
            first_cost=500.0,
            amount=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=1,
            visit_id=1,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=2,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=1,
            visit_id=1,
            sold_item_id=11,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=3,
            date=datetime(2025, 1, 12, 12, 0, 0),
            amount=500.0,
            record_id=3,
            visit_id=3,
            sold_item_id=12,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=4,
            date=datetime(2025, 1, 11, 12, 0, 0),
            amount=1500.0,
            record_id=1,
            visit_id=1,
            sold_item_id=1,
            sold_item_type='goods_transaction',
            master_id=1,
            company_id=1,
        ),
        GoodTransaction(
            id=1,
            document_id=1,
            type_id=1,
            amount=-3.0,
            cost=1500.0,
            master_id=1,
            company_id=1,
            date=datetime(2025, 1, 11, 12, 0, 0),
        ),
    ])

    now = datetime(2025, 1, 1, 0, 0, 0)
    for code, value in {
        'revenue': 7000.0,
        'clients': 2.0,
        'wax_qty': 2.0,
        'camouflage_qty': 2.0,
        'cosmo_qty': 4.0,
        'cosmo_sum': 2000.0,
        'opz_qty': 2.0,
    }.items():
        async_session.add(
            PlanMetric(
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                company_id=1,
                metric_code=code,
                value=value,
                updated_at=now,
            )
        )
        async_session.add(
            PlanMetric(
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                company_id=1,
                staff_id=1,
                staff_category='barber',
                metric_code=code,
                value=value,
                updated_at=now,
            )
        )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        r_staff = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        r_selected_staff = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'staff_id': 1},
        )
        r_selected_staff_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'staff_id': 1},
        )
        r_partial = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-15', 'end_date': '2025-01-20'},
        )
        r_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['plan_period'] == {'start': '2025-01-01', 'end': '2025-01-31'}
    assert data['view_scope'] == 'branch'
    assert data['groups'][0]['title'] == 'Сеть'
    branch_group = next(group for group in data['groups'] if group['scope'] == 'branch')
    assert branch_group['title'] == 'Salon'

    cells = {cell['code']: cell for cell in branch_group['metrics']}
    assert cells['revenue']['fact'] == 4000.0
    assert cells['revenue']['completion_pct'] == pytest.approx(57.14, abs=0.01)
    assert cells['avg_check_total']['fact'] == 2000.0
    assert cells['clients']['fact'] == 2.0
    assert cells['wax_qty']['fact'] == 1.0
    assert cells['camouflage_qty']['fact'] == 2.0
    assert cells['cosmo_qty']['fact'] == 3.0
    assert cells['cosmo_sum']['fact'] == 1500.0
    assert cells['reviews_qty']['plan'] is None
    assert cells['reviews_qty']['fact'] == 0.0
    assert cells['opz_qty']['fact'] == 1.0
    assert cells['opz_pct']['fact'] == 50.0
    assert cells['extra_services_qty']['fact'] == 3.0
    assert cells['extra_services_pct']['fact'] == 150.0
    summary_avg = r_summary.json()['data']['average_check']['total']
    assert cells['avg_check_total']['fact'] == summary_avg

    assert r_staff.status_code == 200
    staff_data = r_staff.json()['data']
    assert staff_data['view_scope'] == 'staff'
    assert staff_data['parent_group']['title'] == 'Salon'
    assert staff_data['groups'][0]['title'] == 'Master'
    assert staff_data['groups'][0]['category'] == 'barber'
    staff_cells = {cell['code']: cell for cell in staff_data['groups'][0]['metrics']}
    assert staff_cells['revenue']['plan'] == 7000.0
    assert staff_cells['revenue']['fact'] == 4000.0

    assert r_selected_staff.status_code == 200
    selected_staff_data = r_selected_staff.json()['data']
    assert selected_staff_data['view_scope'] == 'staff'
    assert selected_staff_data['branch']['title'] == 'Salon'
    assert selected_staff_data['selected_staff']['name'] == 'Master'
    assert [group['title'] for group in selected_staff_data['groups']] == ['Master']
    assert selected_staff_data['selected_staff_plan']['title'] == 'Master'
    selected_plan_rows = {
        row['code']: row
        for row in selected_staff_data['selected_staff_plan']['metrics']
    }
    assert selected_plan_rows['revenue']['plan'] == 7000.0
    assert selected_plan_rows['revenue']['fact'] == 4000.0
    assert selected_plan_rows['revenue']['completion_pct'] == pytest.approx(57.14, abs=0.01)
    assert selected_plan_rows['revenue']['status'] == 'bad'
    assert selected_plan_rows['clients']['plan'] == 2.0
    assert selected_plan_rows['clients']['fact'] == 2.0
    assert selected_plan_rows['clients']['label'] == 'Завершённые визиты'
    selected_staff_summary = r_selected_staff_summary.json()['data']
    assert selected_plan_rows['clients']['fact'] == selected_staff_summary['revenue']['appointments']
    assert selected_plan_rows['opz_qty']['fact'] == selected_staff_summary['visit_metrics']['opz_qty'] == 1.0
    assert selected_plan_rows['opz_pct']['fact'] == selected_staff_summary['visit_metrics']['opz_pct'] == 50.0

    assert r_partial.status_code == 200
    partial_data = r_partial.json()['data']
    assert partial_data['period'] == {'start': '2025-01-15', 'end': '2025-01-20'}
    assert partial_data['plan_period'] == {'start': '2025-01-01', 'end': '2025-01-31'}
    partial_branch_group = next(group for group in partial_data['groups'] if group['scope'] == 'branch')
    partial_cells = {cell['code']: cell for cell in partial_branch_group['metrics']}
    assert partial_cells['revenue']['plan'] == 7000.0
    assert partial_cells['revenue']['fact'] == 0.0


@pytest.mark.asyncio
async def test_plan_fact_recognizes_current_face_and_head_care_titles(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='clients',
            value=1.0,
            updated_at=datetime(2025, 1, 1),
        )
    )
    await async_session.flush()

    service_titles = [
        'СПА для лица (VOLCANO)',
        'СПА для лица глубокое очищение (Mr. Q)',
        'ДЕТОКС УХОД за бородой и кожей лица',
        'ПРЕМИУМ уход за кожей головы и волосами',
        'Комплексное мытьё головы',
    ]
    for index, title in enumerate(service_titles, start=1):
        async_session.add(
            Appointment(
                id=index,
                company_id=1,
                staff_id=1,
                date=date(2025, 1, 10),
                datetime=datetime(2025, 1, 10, 10 + index, 0, 0),
                attendance=1,
            )
        )
        async_session.add(
            Transaction(
                id=index,
                appointment_id=index,
                service_id=index,
                service_title=title,
                amount=1,
                company_id=1,
            )
        )
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=1,
    )
    cells = {
        cell['code']: cell
        for cell in result['selected_staff_plan']['metrics']
    }
    assert cells['face_care_qty']['fact'] == 3.0
    assert cells['head_care_qty']['fact'] == 2.0


@pytest.mark.asyncio
async def test_plan_fact_returns_staff_leaderboards_and_goods_kpis_by_scope(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Alpha', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Bravo', position='Барбер', company_id=1, fired=0),
        Staff(id=3, name='Charlie', position='Барбер', company_id=2, fired=0),
        Staff(id=4, name='Delta Admin', position='Администратор', company_id=1, fired=0, user_id=900),
    ])
    async_session.add_all([
        Service(id=10, title='воск', company_id=1),
        Service(id=11, title='камуфляж', company_id=1),
        Service(id=12, title='Black Mask', company_id=1),
        Service(id=13, title='уход за головой', company_id=2),
        *[
            ServiceLabel(
                service_id=service_id,
                company_id=company_id,
                is_extra=True,
                source='dashboard',
                updated_at=datetime(2025, 1, 1),
            )
            for company_id, service_id in ((1, 10), (1, 11), (1, 12), (2, 13))
        ],
    ])
    async_session.add_all([
        Client(id=1, name='Client 1', company_id=1),
        Client(id=2, name='Client 2', company_id=1),
        Client(id=3, name='Client 3', company_id=1),
        Client(id=4, name='Client 4', company_id=2),
        Client(id=5, name='Client 5', company_id=2),
        Client(id=6, name='Client 6', company_id=1),
    ])
    await async_session.flush()

    appointments = [
        (1, 1, 1, 1, date(2025, 1, 10), None),
        (2, 1, 1, 2, date(2025, 1, 11), None),
        (3, 1, 2, 3, date(2025, 1, 12), None),
        (4, 2, 3, 4, date(2025, 1, 13), None),
        (5, 2, 3, 5, date(2025, 1, 14), None),
        (6, 1, 4, 6, date(2025, 1, 15), 900),
    ]
    async_session.add_all([
        Appointment(
            id=appointment_id,
            company_id=company_id,
            staff_id=staff_id,
            client_id=client_id,
            created_user_id=created_user_id,
            date=appointment_date,
            datetime=datetime(2025, 1, appointment_date.day, 12, 0, 0),
            create_date=datetime(2025, 1, appointment_date.day - 1, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        )
        for appointment_id, company_id, staff_id, client_id, appointment_date, created_user_id in appointments
    ])
    await async_session.flush()

    transaction_rows = [
        (1, 1, 1, 10, 'воск', 1000.0, 1),
        (2, 2, 1, 11, 'камуфляж', 2000.0, 2),
        (3, 3, 1, 12, 'Black Mask', 5000.0, 3),
        (4, 4, 2, 13, 'уход за головой', 1500.0, 4),
        (5, 5, 2, 14, 'Стрижка', 2500.0, 5),
        (6, 6, 1, 15, 'Стрижка', 4000.0, 1),
    ]
    async_session.add_all([
        Transaction(
            id=txn_id,
            appointment_id=appointment_id,
            service_id=service_id,
            service_title=title,
            cost=amount,
            first_cost=amount,
            amount=qty,
            company_id=company_id,
        )
        for txn_id, appointment_id, company_id, service_id, title, amount, qty in transaction_rows
    ])
    async_session.add_all([
        FinancialTransaction(
            id=txn_id,
            date=datetime(2025, 1, 10 + txn_id, 12, 0, 0),
            amount=amount,
            record_id=appointment_id,
            visit_id=appointment_id,
            sold_item_id=service_id,
            sold_item_type='service',
            master_id=appointments[appointment_id - 1][2],
            company_id=company_id,
        )
        for txn_id, appointment_id, company_id, service_id, _title, amount, _qty in transaction_rows
    ])
    async_session.add(
        GoodTransaction(
            id=1,
            document_id=1,
            type_id=1,
            amount=-1.0,
            cost=1000.0,
            master_id=4,
            company_id=1,
            date=datetime(2025, 1, 16, 12, 0, 0),
        )
    )

    now = datetime(2025, 1, 1, 0, 0, 0)
    staff_plans = {
        1: {
            'company_id': 1,
            'revenue': 3000.0,
            'clients': 2.0,
            'wax_qty': 2.0,
            'camouflage_qty': 3.0,
            'face_care_qty': 0.0,
            'head_care_qty': 0.0,
        },
        2: {
            'company_id': 1,
            'revenue': 6000.0,
            'clients': 1.0,
            'wax_qty': 0.0,
            'camouflage_qty': 0.0,
            'face_care_qty': 4.0,
            'head_care_qty': 0.0,
        },
        3: {
            'company_id': 2,
            'revenue': 5000.0,
            'clients': 2.0,
            'wax_qty': 0.0,
            'camouflage_qty': 0.0,
            'face_care_qty': 0.0,
            'head_care_qty': 5.0,
        },
    }
    for staff_id, values in staff_plans.items():
        company_id = values['company_id']
        for code, value in values.items():
            if code == 'company_id':
                continue
            async_session.add(
                PlanMetric(
                    period_start=date(2025, 1, 1),
                    period_end=date(2025, 1, 31),
                    company_id=company_id,
                    staff_id=staff_id,
                    staff_category='barber',
                    metric_code=code,
                    value=value,
                    updated_at=now,
                )
            )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        network_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        branch_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        report_response = await client.get(
            '/dashboard/reports/data',
            params={'report_id': 'staff_leaderboard', 'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert network_response.status_code == 200
    network_data = network_response.json()['data']
    network_boards = network_data['staff_leaderboards']
    assert network_boards['revenue_top'] == network_boards['revenue_barber']
    assert [row['staff'] for row in network_boards['revenue_barber']] == [
        'Bravo',
        'Charlie',
        'Alpha',
    ]
    assert [row['staff'] for row in network_boards['revenue_admin']] == ['Delta Admin']
    assert network_boards['revenue_admin'][0]['value'] == 4000.0
    assert network_boards['cosmo_admin'][0]['pct'] == 25.0
    assert [row['staff'] for row in network_boards['avg_check_top']] == [
        'Bravo',
        'Charlie',
        'Alpha',
    ]
    # The plan/fact widget skips the extra-service revenue query (report-only): qty is present, sum is 0.
    extra_top = network_boards['extra_services']
    assert [(row['staff'], row['qty']) for row in extra_top] == [
        ('Charlie', 4.0),
        ('Alpha', 3.0),
        ('Bravo', 3.0),
    ]
    # The ratings report enables the revenue query, so the money column is populated.
    assert report_response.status_code == 200
    report_data = report_response.json()['data']
    report_revenue_barber = next(table for table in report_data['tables'] if table['id'] == 'revenue_barber')
    report_revenue_admin = next(table for table in report_data['tables'] if table['id'] == 'revenue_admin')
    assert [row['staff'] for row in report_revenue_barber['rows']] == ['Bravo', 'Charlie', 'Alpha']
    assert report_revenue_admin['rows'] == [
        {
            'staff': 'Delta Admin',
            'staff_id': 4,
            'company_id': 1,
            'company_title': 'Salon 1',
            'value': 4000.0,
        }
    ]
    report_cosmo_admin = next(table for table in report_data['tables'] if table['id'] == 'cosmo_admin')
    assert report_cosmo_admin['rows'][0]['pct'] == 25.0
    report_extra = next(
        table for table in report_data['tables'] if table['id'] == 'extra_services'
    )
    assert [row['staff'] for row in report_extra['rows']] == ['Bravo', 'Charlie', 'Alpha']
    assert [(row['staff'], row['qty'], row['sum']) for row in report_extra['ranking']['rows_by_metric']['qty']] == [
        ('Charlie', 4.0, 1500.0),
        ('Alpha', 3.0, 3000.0),
        ('Bravo', 3.0, 5000.0),
    ]
    network_goods = {row['code']: row for row in network_data['goods_kpi_execution']}
    assert set(network_goods) == {'wax_qty', 'camouflage_qty', 'face_care_qty', 'head_care_qty'}
    assert network_goods['wax_qty']['plan'] == 2.0
    assert network_goods['wax_qty']['fact'] == 1.0
    assert network_goods['wax_qty']['completion_pct'] == 50.0
    assert network_goods['face_care_qty']['plan'] == 4.0
    assert network_goods['face_care_qty']['fact'] == 3.0
    assert network_goods['head_care_qty']['plan'] == 5.0
    assert network_goods['head_care_qty']['fact'] == 4.0

    assert branch_response.status_code == 200
    branch_data = branch_response.json()['data']
    branch_boards = branch_data['staff_leaderboards']
    assert branch_boards['revenue_top'] == branch_boards['revenue_barber']
    assert [row['staff'] for row in branch_boards['revenue_barber']] == ['Bravo', 'Alpha']
    assert [row['staff'] for row in branch_boards['revenue_admin']] == ['Delta Admin']
    branch_goods = {row['code']: row for row in branch_data['goods_kpi_execution']}
    assert branch_goods['wax_qty']['plan'] == 2.0
    assert branch_goods['wax_qty']['fact'] == 1.0
    assert branch_goods['head_care_qty']['plan'] == 0.0
    assert branch_goods['head_care_qty']['fact'] == 0.0


@pytest.mark.asyncio
async def test_dashboard_plan_fact_lists_staff_plans_for_each_branch(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add(Staff(id=1, name='Master 1', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=2, name='Admin 2', position='Администратор', company_id=2, fired=0, user_id=500))
    async_session.add(Staff(id=3, name='No Plan', position='Барбер', company_id=2, fired=0))
    now = datetime(2025, 1, 1, 0, 0, 0)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='revenue',
            value=1000.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=2,
            staff_id=2,
            staff_category='administrator',
            metric_code='revenue',
            value=2000.0,
            updated_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        r_company1 = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        r_company2 = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 2},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['view_scope'] == 'branch'
    assert 'branch_sections' not in data
    assert [group['title'] for group in data['groups']] == ['Сеть', 'Salon 1', 'Salon 2']

    assert r_company1.status_code == 200
    company1_groups = r_company1.json()['data']['groups']
    assert [group['title'] for group in company1_groups] == ['Master 1']
    assert company1_groups[0]['category'] == 'barber'

    assert r_company2.status_code == 200
    company2_groups = r_company2.json()['data']['groups']
    assert [group['title'] for group in company2_groups] == ['Admin 2']
    assert company2_groups[0]['category'] == 'administrator'


@pytest.mark.asyncio
async def test_manual_review_facts_feed_plan_fact_for_administrators(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0, user_id=500))
    now = datetime(2025, 1, 1, 0, 0, 0)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            metric_code='reviews_qty',
            value=10.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            metric_code='reviews_qty',
            value=10.0,
            updated_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        save_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'month': '2025-01',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': 7}],
            },
        )
        editor_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        plan_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        network_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        mid_month_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-13', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert save_response.status_code == 200
    assert editor_response.status_code == 200
    editor_rows = editor_response.json()['data']['rows']
    assert [(row['staff_id'], row['value']) for row in editor_rows] == [(2, 7.0)]

    saved_rows = (
        await async_session.execute(
                select(ManualFactMetric).where(
                    ManualFactMetric.period_start == date(2025, 1, 1),
                    ManualFactMetric.period_end == date(2025, 1, 31),
                ManualFactMetric.company_id == 1,
                ManualFactMetric.staff_id == 2,
                ManualFactMetric.metric_code == 'reviews_qty',
            )
        )
    ).scalars().all()
    assert len(saved_rows) == 1
    assert saved_rows[0].value == 7.0

    assert plan_response.status_code == 200
    plan_data = plan_response.json()['data']
    parent_cells = {cell['code']: cell for cell in plan_data['parent_group']['metrics']}
    assert parent_cells['reviews_qty']['plan'] == 10.0
    assert parent_cells['reviews_qty']['fact'] == 7.0

    admin_group = next(group for group in plan_data['groups'] if group['category'] == 'administrator')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert admin_cells['reviews_qty']['plan'] == 10.0
    assert admin_cells['reviews_qty']['fact'] == 7.0
    assert admin_cells['reviews_qty']['completion_pct'] == 70.0

    assert network_response.status_code == 200
    network_groups = network_response.json()['data']['groups']
    network_cells = {cell['code']: cell for cell in network_groups[0]['metrics']}
    branch_cells = {cell['code']: cell for cell in network_groups[1]['metrics']}
    assert network_cells['reviews_qty']['fact'] == 7.0
    assert branch_cells['reviews_qty']['fact'] == 7.0

    # The month-to-date period the dashboard opens with must not zero the monthly fact.
    assert mid_month_response.status_code == 200
    mid_month_data = mid_month_response.json()['data']
    mid_month_parent = {cell['code']: cell for cell in mid_month_data['parent_group']['metrics']}
    mid_month_admin = next(
        group for group in mid_month_data['groups'] if group['category'] == 'administrator'
    )
    mid_month_admin_cells = {cell['code']: cell for cell in mid_month_admin['metrics']}
    assert mid_month_parent['reviews_qty']['fact'] == 7.0
    assert mid_month_admin_cells['reviews_qty']['fact'] == 7.0
    assert mid_month_admin_cells['reviews_qty']['completion_pct'] == 70.0


@pytest.mark.asyncio
async def test_manual_opz_facts_add_to_calculated_opz(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Barber', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0, user_id=500),
        Client(id=1, name='Returning client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 10),
            attendance=1,
        ),
        # Booked on the way out — one calculated OPZ, created by the administrator.
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 1),
            create_date=datetime(2025, 1, 10, 12),
            created_user_id=500,
            attendance=0,
        ),
    ])
    now = datetime(2025, 1, 1, 0, 0, 0)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            metric_code='opz_qty',
            value=10.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            metric_code='opz_qty',
            value=10.0,
            updated_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        baseline_plan = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        baseline_editor = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        save_response = await client.post(
            '/dashboard/plan/opz_fact',
            json={
                'month': '2025-01',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': 3}],
            },
        )
        editor_response = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        plan_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        network_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        summary_response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        mid_month_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-13', 'company_id': 1},
        )
        mid_month_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-13', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    # Without a manual value the editor and Plan/fact show the calculated OPZ alone.
    assert baseline_editor.status_code == 200
    baseline_rows = baseline_editor.json()['data']['rows']
    assert [(row['staff_id'], row['current_value'], row['value'], row['total_value']) for row in baseline_rows] == [
        (2, 1.0, 0.0, 1.0)
    ]
    baseline_cells = {
        cell['code']: cell
        for cell in baseline_plan.json()['data']['parent_group']['metrics']
    }
    assert baseline_cells['opz_qty']['fact'] == 1.0

    assert save_response.status_code == 200
    assert editor_response.status_code == 200
    editor_data = editor_response.json()['data']
    assert [(row['staff_id'], row['current_value'], row['value'], row['total_value']) for row in editor_data['rows']] == [
        (2, 1.0, 3.0, 4.0)
    ]
    assert editor_data['current_total'] == 1.0
    assert editor_data['manual_total'] == 3.0
    # `total_value` keeps the meaning it has for the reviews editor — the manual sum — and
    # the combined number this editor renders has its own field.
    assert editor_data['total_value'] == 3.0
    assert editor_data['combined_total'] == 4.0

    saved_rows = (
        await async_session.execute(
            select(ManualFactMetric).where(
                ManualFactMetric.period_start == date(2025, 1, 1),
                ManualFactMetric.period_end == date(2025, 1, 31),
                ManualFactMetric.company_id == 1,
                ManualFactMetric.staff_id == 2,
                ManualFactMetric.metric_code == 'opz_qty',
            )
        )
    ).scalars().all()
    assert [row.value for row in saved_rows] == [3.0]

    assert plan_response.status_code == 200
    plan_data = plan_response.json()['data']
    branch_cells = {cell['code']: cell for cell in plan_data['parent_group']['metrics']}
    assert branch_cells['opz_qty']['fact'] == 4.0
    # The percentage has to follow the summed numerator, not the calculated part alone.
    branch_clients = branch_cells['clients']['fact'] or 0.0
    assert branch_cells['opz_pct']['fact'] == pytest.approx(
        100.0 * 4.0 / branch_clients if branch_clients else 0.0
    )

    admin_group = next(group for group in plan_data['groups'] if group['category'] == 'administrator')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert admin_cells['opz_qty']['fact'] == 4.0
    assert admin_cells['opz_qty']['completion_pct'] == 40.0

    assert network_response.status_code == 200
    network_groups = network_response.json()['data']['groups']
    network_cells = {cell['code']: cell for cell in network_groups[0]['metrics']}
    assert network_cells['opz_qty']['fact'] == 4.0

    assert summary_response.status_code == 200
    summary_data = summary_response.json()['data']
    assert summary_data['visit_metrics']['opz_qty'] == 4.0
    # The overview derives its own percentage, so the summed numerator has to reach it too.
    assert summary_data['visit_metrics']['opz_pct'] == pytest.approx(
        100.0 * 4.0 / summary_data['revenue']['appointments']
    )

    # A past window shorter than the month gets the calculated part only: a monthly value
    # cannot be split, and importing all of it into 13 days would overstate the fact.
    assert mid_month_response.status_code == 200
    mid_month_cells = {
        cell['code']: cell
        for cell in mid_month_response.json()['data']['parent_group']['metrics']
    }
    assert mid_month_cells['opz_qty']['fact'] == 1.0
    assert mid_month_summary.json()['data']['visit_metrics']['opz_qty'] == 1.0


@pytest.mark.asyncio
async def test_manual_opz_facts_cleared_value_restores_calculated_fact(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Barber', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0, user_id=500),
        Client(id=1, name='Returning client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 1),
            create_date=datetime(2025, 1, 10, 12),
            created_user_id=500,
            attendance=0,
        ),
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=5.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        negative_response = await client.post(
            '/dashboard/plan/opz_fact',
            json={
                'month': '2025-01',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': -1}],
            },
        )
        invalid_month_response = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01-01', 'company_id': 1},
        )
        # A non-finite number passes `value < 0`, so it needs its own guard to stay a 400.
        not_a_number_response = await client.post(
            '/dashboard/plan/opz_fact',
            content='{"month": "2025-01", "company_id": 1, "items": '
                    '[{"company_id": 1, "staff_id": 2, "value": NaN}]}',
            headers={'Content-Type': 'application/json'},
        )
        cleared_response = await client.post(
            '/dashboard/plan/opz_fact',
            json={
                'month': '2025-01',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': None}],
            },
        )
        summary_response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert negative_response.status_code == 400
    assert invalid_month_response.status_code == 400
    assert not_a_number_response.status_code == 400
    assert cleared_response.status_code == 200
    cleared_row = cleared_response.json()['data']['rows'][0]
    assert (cleared_row['current_value'], cleared_row['value'], cleared_row['total_value']) == (1.0, 0.0, 1.0)
    assert summary_response.json()['data']['visit_metrics']['opz_qty'] == 1.0

    remaining = (
        await async_session.execute(
            select(ManualFactMetric).where(ManualFactMetric.metric_code == 'opz_qty')
        )
    ).scalars().all()
    assert remaining == []


@pytest.mark.asyncio
async def test_manual_opz_facts_reach_year_facts(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0, user_id=500),
        ManualFactMetric(
            period_start=date(2025, 3, 1),
            period_end=date(2025, 3, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=4.0,
            updated_at=datetime(2025, 4, 1, 10, 0, 0),
        ),
        ManualFactMetric(
            period_start=date(2024, 3, 1),
            period_end=date(2024, 3, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=2.0,
            updated_at=datetime(2024, 4, 1, 10, 0, 0),
        ),
    ])
    await async_session.commit()

    facts = await dashboard_service.fetch_opz_year_facts(
        async_session,
        date(2024, 1, 1),
        date(2025, 12, 31),
        1,
        None,
    )
    assert facts['counts'] == {2024: 2.0, 2025: 4.0}


@pytest.mark.asyncio
async def test_manual_opz_editor_column_matches_plan_fact_admin_row(async_session):
    """The baseline the editor offers must be the number it will be added to.

    Plan/fact drops events anchored on a barber the branch no longer lists, so an editor
    that counted every branch event would show an administrator a larger baseline than
    their own row.
    """
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Left barber', position='Барбер', company_id=1, fired=1),
        Staff(id=2, name='Barber', position='Барбер', company_id=1, fired=0),
        Staff(id=3, name='Admin', position='Администратор', company_id=1, fired=0, user_id=500),
        Client(id=1, name='Client of the fired barber', company_id=1),
        Client(id=2, name='Client of the active barber', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 1),
            create_date=datetime(2025, 1, 10, 12),
            created_user_id=500,
            attendance=0,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=2,
            client_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 10),
            attendance=1,
        ),
        Appointment(
            id=4,
            company_id=1,
            staff_id=2,
            client_id=2,
            date=date(2025, 2, 2),
            create_date=datetime(2025, 1, 11, 12),
            created_user_id=500,
            attendance=0,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        editor_response = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        plan_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    editor_rows = editor_response.json()['data']['rows']
    assert [(row['staff_id'], row['current_value']) for row in editor_rows] == [(3, 1.0)]

    admin_group = next(
        group for group in plan_response.json()['data']['groups']
        if group['category'] == 'administrator'
    )
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert admin_cells['opz_qty']['fact'] == editor_rows[0]['current_value']


@pytest.mark.asyncio
async def test_manual_opz_facts_can_filter_by_staff(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=2, name='Admin one', position='Администратор', company_id=1, fired=0),
        Staff(id=3, name='Admin two', position='Администратор', company_id=1, fired=0),
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=3,
            metric_code='opz_qty',
            value=2.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        all_staff = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        single_staff = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1, 'staff_id': 3},
        )
        foreign_staff_save = await client.post(
            '/dashboard/plan/opz_fact',
            json={
                'month': '2025-01',
                'company_id': 1,
                'staff_id': 3,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': 1}],
            },
        )
    app.dependency_overrides.clear()

    assert {row['staff_id'] for row in all_staff.json()['data']['rows']} == {2, 3}
    filtered = single_staff.json()['data']
    assert [(row['staff_id'], row['value']) for row in filtered['rows']] == [(3, 2.0)]
    assert filtered['manual_total'] == 2.0
    # A row outside the selected staff member must not slip through the payload.
    assert foreign_staff_save.status_code == 400


@pytest.mark.asyncio
async def test_manual_opz_lookup_short_circuits_without_stored_values(async_session):
    """The overview asks twice per request, so an empty top-up must stay one query."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.commit()

    statement_count = 0

    def count_statements(*_args):
        nonlocal statement_count
        statement_count += 1

    event.listen(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
    try:
        empty_total = await dashboard_service._manual_opz_scope_total(
            async_session,
            date(2025, 1, 1),
            date(2025, 1, 31),
            [1],
        )
    finally:
        event.remove(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)

    assert empty_total == 0.0
    assert statement_count == 1


@pytest.mark.asyncio
async def test_manual_opz_scope_lookup_touches_only_branches_holding_a_value(async_session):
    """The overview asks twice per request; a stored value must not wake every branch."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon one', group_id=1),
        Company(id=2, title='Salon two', group_id=1),
        Company(id=3, title='Salon three', group_id=1),
    ])
    async_session.add_all([
        Staff(id=2, name='Admin one', position='Администратор', company_id=1, fired=0),
        Staff(id=3, name='Admin two', position='Администратор', company_id=2, fired=0),
        Staff(id=4, name='Admin three', position='Администратор', company_id=3, fired=0),
    ])
    await async_session.flush()
    async_session.add(ManualFactMetric(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        company_id=1,
        staff_id=2,
        metric_code='opz_qty',
        value=4.0,
        updated_at=datetime(2025, 1, 20, 10, 0, 0),
    ))
    await async_session.commit()

    counts = {}

    def measure(label, company_ids):
        statements = 0

        def count_statements(*_args):
            nonlocal statements
            statements += 1

        async def run():
            nonlocal statements
            event.listen(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
            try:
                value = await dashboard_service._manual_opz_scope_total(
                    async_session, date(2025, 1, 1), date(2025, 1, 31), company_ids
                )
            finally:
                event.remove(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
            counts[label] = (value, statements)

        return run()

    await measure('one branch', [1])
    await measure('three branches', [1, 2, 3])

    assert counts['one branch'][0] == counts['three branches'][0] == 4.0
    # Branches 2 and 3 hold nothing, so widening the scope must not cost extra queries.
    assert counts['three branches'][1] <= counts['one branch'][1]


@pytest.mark.asyncio
async def test_manual_opz_ignores_months_when_the_staff_was_not_an_administrator(async_session):
    """A stored value only counts for the months the person actually was an administrator."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Barber then', position='Барбер', company_id=1, fired=0))
    await async_session.flush()
    async_session.add(ManualFactMetric(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        company_id=1,
        staff_id=2,
        metric_code='opz_qty',
        value=9.0,
        updated_at=datetime(2025, 1, 20, 10, 0, 0),
    ))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        summary_response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert summary_response.json()['data']['visit_metrics']['opz_qty'] == 0.0

    year_facts = await dashboard_service.fetch_opz_year_facts(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        1,
        None,
    )
    assert year_facts['counts'] == {}


@pytest.mark.asyncio
async def test_manual_opz_counts_only_whole_or_running_months(async_session, monkeypatch):
    """A monthly value is indivisible, so only whole months and the running month count.

    The overview ships "today" and "week" presets; without this rule a single day would
    carry the entire month of top-ups, and a week across a month boundary two of them.
    """
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.flush()
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=3.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
        ManualFactMetric(
            period_start=date(2025, 2, 1),
            period_end=date(2025, 2, 28),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=2.0,
            updated_at=datetime(2025, 2, 20, 10, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    metrics_by_period = {}

    async def opz_qty(start_date, end_date):
        app.dependency_overrides[api.get_async_db] = override_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            summary = await client.get(
                '/dashboard/widget/summary',
                params={'start_date': start_date, 'end_date': end_date, 'company_id': 1},
            )
            plan = await client.get(
                '/dashboard/widget/plan_fact',
                params={'start_date': start_date, 'end_date': end_date, 'company_id': 1},
            )
        app.dependency_overrides.clear()
        plan_cells = {
            cell['code']: cell
            for cell in plan.json()['data']['parent_group']['metrics']
        }
        visit_metrics = summary.json()['data']['visit_metrics']
        metrics_by_period[(start_date, end_date)] = visit_metrics
        summary_value = visit_metrics['opz_qty']
        # The overview and Plan/fact must never disagree about the same period.
        assert plan_cells['opz_qty']['fact'] == summary_value
        return summary_value

    assert await opz_qty('2025-01-05', '2025-01-12') == 0.0
    assert await opz_qty('2025-01-28', '2025-02-03') == 0.0
    assert await opz_qty('2025-01-01', '2025-01-31') == 3.0
    assert await opz_qty('2025-01-01', '2025-02-28') == 5.0

    # The month-to-date view the dashboard opens with keeps the running month whole:
    # a monthly plan would otherwise stand against a fact that is missing on purpose.
    frozen_now = datetime(2025, 1, 20, 12, 0)
    monkeypatch.setattr(dashboard_service, 'factual_now', lambda: frozen_now)
    assert await opz_qty('2025-01-01', '2025-01-20') == 3.0
    assert await opz_qty('2025-01-05', '2025-01-20') == 0.0

    # Early in a month the running month IS a couple of days, and a period that reaches
    # today from before the 1st still covers this month from its start.
    monkeypatch.setattr(dashboard_service, 'factual_now', lambda: datetime(2025, 2, 2, 12, 0))
    assert await opz_qty('2025-02-02', '2025-02-02') == 0.0
    assert await opz_qty('2025-02-01', '2025-02-02') == 2.0
    assert await opz_qty('2025-01-27', '2025-02-02') == 2.0

    # The previous window of a month-to-date period is the tail of the previous month and
    # would count nothing on its own, so the delta weighs month against month: February's
    # 2 against January's 3, not against zero.
    month_to_date = metrics_by_period[('2025-02-01', '2025-02-02')]
    assert month_to_date['opz_qty'] == 2.0
    assert month_to_date['opz_qty_change_pct'] == pytest.approx(-33.33)


@pytest.mark.asyncio
async def test_manual_opz_daily_series_stays_calculated(async_session):
    """The chart under the tile cannot carry a monthly value, so it must not try."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Barber', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0, user_id=500),
        Client(id=1, name='Returning client', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 1),
            create_date=datetime(2025, 1, 10, 12),
            created_user_id=500,
            attendance=0,
        ),
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=7.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        daily_response = await client.get(
            '/dashboard/widget/revenue_daily',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        summary_response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    daily_rows = daily_response.json()['data']
    assert sum(row['opz_qty'] for row in daily_rows) == 1.0
    assert summary_response.json()['data']['visit_metrics']['opz_qty'] == 8.0


@pytest.mark.asyncio
async def test_manual_opz_fact_write_is_scoped_to_the_user_branches_and_role(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon', group_id=1),
        Company(id=2, title='Other salon', group_id=1),
    ])
    async_session.add_all([
        Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0),
        Staff(id=3, name='Other admin', position='Администратор', company_id=2, fired=0),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    contexts = {
        'branch_admin': AccessContext.from_user(
            user_id=10,
            role='branch_admin',
            portal_account_id=1,
            company_ids=[1],
        ),
        'manager': AccessContext.from_user(
            user_id=11,
            role='manager',
            portal_account_id=1,
            company_ids=[1],
        ),
    }
    active = {'role': 'branch_admin'}

    async def override_access():
        return contexts[active['role']]

    app.dependency_overrides[api.get_async_db] = override_db
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = override_access
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        foreign_branch = await client.post(
            '/dashboard/plan/opz_fact',
            json={
                'month': '2025-01',
                'company_id': 2,
                'items': [{'company_id': 2, 'staff_id': 3, 'value': 1}],
            },
        )
        active['role'] = 'manager'
        manager_read = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        manager_write = await client.post(
            '/dashboard/plan/opz_fact',
            json={
                'month': '2025-01',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': 1}],
            },
        )
    app.dependency_overrides.clear()

    # A branch outside the user scope must not be writable through the payload.
    assert foreign_branch.status_code in (400, 403)
    assert manager_read.status_code == 403
    assert manager_write.status_code == 403

    stored = (
        await async_session.execute(
            select(ManualFactMetric).where(ManualFactMetric.metric_code == 'opz_qty')
        )
    ).scalars().all()
    assert stored == []


@pytest.mark.asyncio
async def test_manual_opz_editor_totals_count_only_what_reports_count(async_session):
    """A value stored for a month the person was not an administrator stays editable.

    It must not be promised in the header total either — nothing else counts it.
    """
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0),
        Staff(id=3, name='Barber', position='Барбер', company_id=1, fired=0),
        Staff(id=4, name='Barber without a value', position='Барбер', company_id=1, fired=0),
    ])
    await async_session.flush()
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=4.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=3,
            metric_code='opz_qty',
            value=6.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=4,
            metric_code='opz_qty',
            value=0.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        editor_response = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        summary_response = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    editor = editor_response.json()['data']
    rows = {row['staff_id']: row for row in editor['rows']}
    assert rows[2]['counted'] is True
    assert rows[3]['counted'] is False
    # The barber row keeps its stored value on screen so it can be cleared, but its
    # "total actual" cannot promise a number the reports drop.
    assert rows[3]['value'] == 6.0
    assert rows[3]['total_value'] == 0.0
    # The flag does not wait for a value: a row that could never count says so up front.
    assert rows[4]['value'] == 0.0
    assert rows[4]['counted'] is False
    assert editor['manual_total'] == 4.0
    assert editor['combined_total'] == 4.0
    assert editor['combined_total'] == summary_response.json()['data']['visit_metrics']['opz_qty']


@pytest.mark.asyncio
async def test_manual_opz_overview_staff_filter_stays_on_one_attribution(async_session):
    """A filtered overview attributes OPZ to the barber, so it must not add the top-up."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.flush()
    async_session.add(ManualFactMetric(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 31),
        company_id=1,
        staff_id=2,
        metric_code='opz_qty',
        value=5.0,
        updated_at=datetime(2025, 1, 20, 10, 0, 0),
    ))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        branch_summary = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        staff_summary = await client.get(
            '/dashboard/widget/summary',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'staff_id': 2,
            },
        )
    app.dependency_overrides.clear()

    assert branch_summary.json()['data']['visit_metrics']['opz_qty'] == 5.0
    staff_metrics = staff_summary.json()['data']['visit_metrics']
    assert staff_metrics['opz_qty'] == 0.0
    assert staff_metrics['opz_pct'] == 0.0

    # The year facts answer a staff filter the same way, for the same reason.
    branch_year = await dashboard_service.fetch_opz_year_facts(
        async_session, date(2025, 1, 1), date(2025, 12, 31), 1, None
    )
    staff_year = await dashboard_service.fetch_opz_year_facts(
        async_session, date(2025, 1, 1), date(2025, 12, 31), 1, 2
    )
    assert branch_year['counts'] == {2025: 5.0}
    assert staff_year['counts'] == {}


@pytest.mark.asyncio
async def test_manual_opz_scope_lookup_cost_is_bounded_when_every_branch_holds_a_value(async_session):
    """The overview pays this twice per request, so the loaded path needs a ceiling too."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Salon one', group_id=1),
        Company(id=2, title='Salon two', group_id=1),
        Company(id=3, title='Salon three', group_id=1),
    ])
    async_session.add_all([
        Staff(id=2, name='Admin one', position='Администратор', company_id=1, fired=0),
        Staff(id=3, name='Admin two', position='Администратор', company_id=2, fired=0),
        Staff(id=4, name='Admin three', position='Администратор', company_id=3, fired=0),
    ])
    await async_session.flush()
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=company_id,
            staff_id=staff_id,
            metric_code='opz_qty',
            value=2.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        )
        for company_id, staff_id in ((1, 2), (2, 3), (3, 4))
    ])
    await async_session.commit()

    statement_count = 0

    def count_statements(*_args):
        nonlocal statement_count
        statement_count += 1

    event.listen(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
    try:
        total = await dashboard_service._manual_opz_scope_total(
            async_session, date(2025, 1, 1), date(2025, 1, 31), [1, 2, 3]
        )
    finally:
        event.remove(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)

    assert total == 6.0
    # 1 probe + 1 staff query + 2 per branch that holds a value + 1 rows query.
    assert statement_count <= 9

    # Growth must stay linear in the branches that hold a value, not worse.
    async_session.add_all([
        Company(id=4, title='Salon four', group_id=1),
        Company(id=5, title='Salon five', group_id=1),
        Company(id=6, title='Salon six', group_id=1),
    ])
    async_session.add_all([
        Staff(id=5, name='Admin four', position='Администратор', company_id=4, fired=0),
        Staff(id=6, name='Admin five', position='Администратор', company_id=5, fired=0),
        Staff(id=7, name='Admin six', position='Администратор', company_id=6, fired=0),
    ])
    await async_session.flush()
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=company_id,
            staff_id=staff_id,
            metric_code='opz_qty',
            value=2.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        )
        for company_id, staff_id in ((4, 5), (5, 6), (6, 7))
    ])
    await async_session.commit()

    doubled_count = 0

    def count_doubled(*_args):
        nonlocal doubled_count
        doubled_count += 1

    event.listen(async_session.bind.sync_engine, 'before_cursor_execute', count_doubled)
    try:
        doubled_total = await dashboard_service._manual_opz_scope_total(
            async_session, date(2025, 1, 1), date(2025, 1, 31), [1, 2, 3, 4, 5, 6]
        )
    finally:
        event.remove(async_session.bind.sync_engine, 'before_cursor_execute', count_doubled)

    assert doubled_total == 12.0
    assert doubled_count <= statement_count + 2 * 3


@pytest.mark.asyncio
async def test_manual_opz_delta_compares_like_periods(async_session, monkeypatch):
    """A flat top-up every month must read as no growth, whatever the period length."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.flush()
    async_session.add_all([
        ManualFactMetric(
            period_start=date(year, month, 1),
            period_end=date(year, month, monthrange(year, month)[1]),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=10.0,
            updated_at=datetime(year, month, 20, 10, 0, 0),
        )
        for year in (2024, 2025)
        for month in range(1, 13)
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    async def metrics(start_date, end_date):
        app.dependency_overrides[api.get_async_db] = override_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            response = await client.get(
                '/dashboard/widget/summary',
                params={'start_date': start_date, 'end_date': end_date, 'company_id': 1},
            )
        app.dependency_overrides.clear()
        return response.json()['data']['visit_metrics']

    one_month = await metrics('2025-12-01', '2025-12-31')
    assert (one_month['opz_qty'], one_month['opz_qty_change_pct']) == (10.0, 0.0)

    quarter = await metrics('2025-10-01', '2025-12-31')
    assert (quarter['opz_qty'], quarter['opz_qty_change_pct']) == (30.0, 0.0)

    # Manual months are indivisible, so a baseline that starts mid-month counts nothing.
    # These selections carry no preset, and a window spanning several months still steps
    # back by days, so the manual-fact baseline computes its own whole months instead.
    for short_month, days in (('02', 28), ('04', 30), ('09', 30), ('11', 30)):
        selected = await metrics(f'2025-{short_month}-01', f'2025-{short_month}-{days}')
        assert (selected['opz_qty'], selected['opz_qty_change_pct']) == (10.0, 0.0)

    # A rolling window that crosses a month boundary counts the running month, so its
    # baseline is the month before it — one month against one month, not against nothing.
    monkeypatch.setattr(dashboard_service, 'factual_now', lambda: datetime(2025, 12, 2, 12, 0))
    crossing_week = await metrics('2025-11-26', '2025-12-02')
    assert (crossing_week['opz_qty'], crossing_week['opz_qty_change_pct']) == (10.0, 0.0)

    month_to_date = await metrics('2025-12-01', '2025-12-02')
    assert (month_to_date['opz_qty'], month_to_date['opz_qty_change_pct']) == (10.0, 0.0)

    # A window that ends mid-month gets the same treatment as one that ends on the last
    # day: quarter-to-date is weighed against the three months before it, not against two.
    monkeypatch.setattr(dashboard_service, 'factual_now', lambda: datetime(2025, 12, 15, 12, 0))
    quarter_to_date = await metrics('2025-10-01', '2025-12-15')
    assert (quarter_to_date['opz_qty'], quarter_to_date['opz_qty_change_pct']) == (30.0, 0.0)
    year_to_date = await metrics('2025-01-01', '2025-12-15')
    assert (year_to_date['opz_qty'], year_to_date['opz_qty_change_pct']) == (120.0, 0.0)
    rolling_month = await metrics('2025-11-16', '2025-12-15')
    assert (rolling_month['opz_qty'], rolling_month['opz_qty_change_pct']) == (10.0, 0.0)

    year = await metrics('2025-01-01', '2025-12-31')
    assert (year['opz_qty'], year['opz_qty_change_pct']) == (120.0, 0.0)


@pytest.mark.asyncio
async def test_manual_opz_delta_uses_the_matching_previous_months(async_session):
    """A quarter is compared with the quarter before it, not with its own months."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.flush()
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, month, 1),
            period_end=date(2025, month, monthrange(2025, month)[1]),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=10.0,
            updated_at=datetime(2025, month, 20, 10, 0, 0),
        )
        for month in (10, 11, 12)
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    async def metrics(start_date, end_date):
        app.dependency_overrides[api.get_async_db] = override_db
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            response = await client.get(
                '/dashboard/widget/summary',
                params={'start_date': start_date, 'end_date': end_date, 'company_id': 1},
            )
        app.dependency_overrides.clear()
        return response.json()['data']['visit_metrics']

    # The quarter that holds every top-up is compared with the empty quarter before it.
    fourth_quarter = await metrics('2025-10-01', '2025-12-31')
    assert (fourth_quarter['opz_qty'], fourth_quarter['opz_qty_change_pct']) == (30.0, 100.0)

    # An empty quarter stays empty — the following one must not leak into its base.
    third_quarter = await metrics('2025-07-01', '2025-09-30')
    assert third_quarter['opz_qty'] == 0.0

    year = await metrics('2025-01-01', '2025-12-31')
    assert (year['opz_qty'], year['opz_qty_change_pct']) == (30.0, 100.0)


@pytest.mark.asyncio
async def test_manual_opz_respects_the_branch_reporting_start(async_session):
    """A month before the branch opened counts nowhere, and the editor says so up front."""
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 6, 1)))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.flush()
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=5.0,
            updated_at=datetime(2025, 1, 20, 10, 0, 0),
        ),
        ManualFactMetric(
            period_start=date(2025, 7, 1),
            period_end=date(2025, 7, 31),
            company_id=1,
            staff_id=2,
            metric_code='opz_qty',
            value=4.0,
            updated_at=datetime(2025, 7, 20, 10, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        before_opening = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        after_opening = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-07-01', 'end_date': '2025-07-31', 'company_id': 1},
        )
        editor_before = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-01', 'company_id': 1},
        )
        editor_after = await client.get(
            '/dashboard/plan/opz_fact',
            params={'month': '2025-07', 'company_id': 1},
        )
        plan_before = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert before_opening.json()['data']['visit_metrics']['opz_qty'] == 0.0
    assert after_opening.json()['data']['visit_metrics']['opz_qty'] == 4.0

    before_rows = editor_before.json()['data']
    assert before_rows['rows'][0]['counted'] is False
    assert before_rows['manual_total'] == 0.0
    assert editor_after.json()['data']['manual_total'] == 4.0

    plan_cells = {
        cell['code']: cell
        for cell in plan_before.json()['data']['parent_group']['metrics']
    }
    assert plan_cells['opz_qty']['fact'] == 0.0


@pytest.mark.asyncio
async def test_plan_settings_save_drops_the_cached_administrator_roles(async_session):
    """Roles are memoised per request and derived from the plan, so a save must drop them.

    Nothing today reads them again inside a saving request; the point of the test is that
    the day something does, it reads the roles the save just wrote.
    """
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.commit()

    await dashboard_service._administrator_role_periods_by_company(
        async_session, date(2025, 1, 1), date(2025, 1, 31), [1]
    )
    assert async_session.info.get('administrator_role_periods')

    await dashboard_service.save_plan_settings(
        async_session,
        '2025-01',
        [{'company_id': 1, 'wax_pct': 10}],
        [{
            'company_id': 1,
            'staff_id': 2,
            'staff_category': 'administrator',
            'reviews_qty': 3,
        }],
    )

    assert not async_session.info.get('administrator_role_periods')


@pytest.mark.asyncio
async def test_manual_review_facts_can_filter_by_staff(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin 1', position='Администратор', company_id=1, fired=0))
    async_session.add(Staff(id=3, name='Admin 2', position='Администратор', company_id=1, fired=0))
    async_session.add(ManualFactMetric(
        period_start=date(2025, 1, 1),
        period_end=date(2025, 1, 1),
        company_id=1,
        staff_id=2,
        metric_code='reviews_qty',
        value=4.0,
        source='dashboard',
        updated_at=datetime(2025, 1, 2, 0, 0, 0),
    ))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        filtered_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={
                'month': '2025-01',
                'company_id': 1,
                'staff_id': 2,
            },
        )
        save_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'month': '2025-01',
                'company_id': 1,
                'staff_id': 3,
                'items': [{'company_id': 1, 'staff_id': 3, 'value': 9}],
            },
        )
    app.dependency_overrides.clear()

    assert filtered_response.status_code == 200
    filtered_rows = filtered_response.json()['data']['rows']
    assert [(row['staff_id'], row['value']) for row in filtered_rows] == [(2, 4.0)]

    assert save_response.status_code == 200
    saved_rows = save_response.json()['data']['rows']
    assert [(row['staff_id'], row['value']) for row in saved_rows] == [(3, 9.0)]


@pytest.mark.asyncio
async def test_manual_review_facts_use_one_value_per_month(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, 6, day),
            period_end=date(2025, 6, day),
            company_id=1,
            staff_id=2,
            metric_code='reviews_qty',
            value=float(day),
            source='legacy',
            updated_at=datetime(2025, 6, day, 0, 0, 0),
        )
        for day in range(1, 5)
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        legacy_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-06', 'company_id': 1},
        )
        save_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'month': '2025-06',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': 12}],
            },
        )
        full_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-06', 'company_id': 1},
        )
        other_month_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-07', 'company_id': 1},
        )
        partial_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-03', 'end_date': '2025-06-04', 'company_id': 1},
        )
        network_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-06-05'},
        )
        next_month_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-07-01', 'end_date': '2025-07-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert legacy_response.json()['data']['rows'][0]['value'] == 10.0
    assert save_response.status_code == 200

    full_data = full_response.json()['data']
    assert full_data['month'] == '2025-06'
    assert full_data['total_value'] == 12.0
    assert full_data['rows'][0]['value'] == 12.0
    assert other_month_response.json()['data']['rows'][0]['value'] == 0.0

    # Any period inside the month sees the whole month, and only that month.
    partial_data = partial_response.json()['data']
    parent_cells = {cell['code']: cell for cell in partial_data['parent_group']['metrics']}
    admin_group = next(group for group in partial_data['groups'] if group['category'] == 'administrator')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert parent_cells['reviews_qty']['fact'] == 12.0
    assert admin_cells['reviews_qty']['fact'] == 12.0

    network_cells = {cell['code']: cell for cell in network_response.json()['data']['groups'][0]['metrics']}
    assert network_cells['reviews_qty']['fact'] == 12.0

    next_month_cells = {
        cell['code']: cell
        for cell in next_month_response.json()['data']['parent_group']['metrics']
    }
    assert next_month_cells['reviews_qty']['fact'] == 0.0

    # Saving collapses the legacy per-day rows into a single month-anchored row.
    stored_rows = (
        await async_session.execute(
            select(ManualFactMetric).where(
                ManualFactMetric.company_id == 1,
                ManualFactMetric.staff_id == 2,
                ManualFactMetric.metric_code == 'reviews_qty',
            )
        )
    ).scalars().all()
    assert [(row.period_start, row.period_end, row.value) for row in stored_rows] == [
        (date(2025, 6, 1), date(2025, 6, 30), 12.0)
    ]


@pytest.mark.asyncio
async def test_manual_review_facts_period_validation(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        invalid_month_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-06-01', 'company_id': 1},
        )
        negative_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'month': '2025-06',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': -1}],
            },
        )
        zero_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'month': '2025-06',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': 0}],
            },
        )
    app.dependency_overrides.clear()

    assert invalid_month_response.status_code == 400
    assert negative_response.status_code == 400
    assert zero_response.status_code == 200
    zero_row = zero_response.json()['data']['rows'][0]
    assert zero_row['value'] == 0.0
    assert 'values' not in zero_row


@pytest.mark.asyncio
async def test_manual_review_facts_follow_the_plan_staff_category(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    # Position says barber, the plan says administrator — Plan/fact goes by the plan, so the
    # reviews editor has to list the same person.
    async_session.add(Staff(id=5, name='Planned Admin', position='Барбер', company_id=1, fired=0))
    async_session.add(PlanMetric(
        period_start=date(2025, 3, 1),
        period_end=date(2025, 3, 31),
        company_id=1,
        staff_id=5,
        staff_category='administrator',
        metric_code='reviews_qty',
        value=8.0,
        updated_at=datetime(2025, 3, 1, 0, 0, 0),
    ))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        editor_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-03', 'company_id': 1},
        )
        save_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'month': '2025-03',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 5, 'value': 6}],
            },
        )
        plan_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-03-01', 'end_date': '2025-03-20', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert editor_response.status_code == 200
    editor_rows = editor_response.json()['data']['rows']
    assert [(row['staff_id'], row['is_active']) for row in editor_rows] == [(5, True)]
    assert save_response.status_code == 200

    plan_data = plan_response.json()['data']
    staff_group = next(group for group in plan_data['groups'] if group['staff_id'] == 5)
    staff_cells = {cell['code']: cell for cell in staff_group['metrics']}
    branch_cells = {cell['code']: cell for cell in plan_data['parent_group']['metrics']}
    assert staff_group['category'] == 'administrator'
    assert staff_cells['reviews_qty']['fact'] == 6.0
    assert branch_cells['reviews_qty']['fact'] == 6.0


@pytest.mark.asyncio
async def test_manual_review_facts_keep_branch_total_equal_to_staff_rows(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    async_session.add(Staff(id=3, name='Ex Admin', position='Барбер', company_id=1, fired=0))
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, 4, 1),
            period_end=date(2025, 4, 30),
            company_id=1,
            staff_id=staff_id,
            metric_code='reviews_qty',
            value=value,
            source='dashboard',
            updated_at=datetime(2025, 4, 30, 0, 0, 0),
        )
        for staff_id, value in ((2, 5.0), (3, 4.0))
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        editor_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-04', 'company_id': 1},
        )
        plan_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-04-01', 'end_date': '2025-04-30', 'company_id': 1},
        )
        clear_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'month': '2025-04',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 3, 'value': None}],
            },
        )
    app.dependency_overrides.clear()

    # A value left behind by someone who is no longer an administrator stays visible and
    # editable, but does not inflate the branch total the staff rows have to add up to.
    editor_rows = {row['staff_id']: row for row in editor_response.json()['data']['rows']}
    assert editor_rows[3]['is_active'] is False
    assert editor_rows[3]['value'] == 4.0
    assert editor_rows[2]['is_active'] is True

    plan_data = plan_response.json()['data']
    branch_cells = {cell['code']: cell for cell in plan_data['parent_group']['metrics']}
    staff_facts = sum(
        cell['fact']
        for group in plan_data['groups']
        for cell in group['metrics']
        if cell['code'] == 'reviews_qty'
    )
    assert branch_cells['reviews_qty']['fact'] == 5.0
    assert staff_facts == 5.0

    assert clear_response.status_code == 200
    assert [row['staff_id'] for row in clear_response.json()['data']['rows']] == [2]


@pytest.mark.asyncio
async def test_manual_review_facts_respect_reporting_start_date(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(
        id=1,
        title='Salon',
        group_id=1,
        reporting_start_date=date(2025, 5, 1),
    ))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    async_session.add_all([
        ManualFactMetric(
            period_start=date(2025, month, 1),
            period_end=date(2025, month, monthrange(2025, month)[1]),
            company_id=1,
            staff_id=2,
            metric_code='reviews_qty',
            value=value,
            source='dashboard',
            updated_at=datetime(2025, month, 1, 0, 0, 0),
        )
        for month, value in ((4, 9.0), (5, 3.0))
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        before_start_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-04-01', 'end_date': '2025-04-30', 'company_id': 1},
        )
        after_start_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-05-01', 'end_date': '2025-05-31', 'company_id': 1},
        )
        editor_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'month': '2025-04', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    before_cells = {
        cell['code']: cell
        for cell in before_start_response.json()['data']['parent_group']['metrics']
    }
    after_cells = {
        cell['code']: cell
        for cell in after_start_response.json()['data']['parent_group']['metrics']
    }
    assert before_cells['reviews_qty']['fact'] == 0.0
    assert after_cells['reviews_qty']['fact'] == 3.0
    # The editor still shows the uncounted value so it can be corrected.
    assert editor_response.json()['data']['rows'][0]['value'] == 9.0


@pytest.mark.asyncio
async def test_plan_settings_empty_month_lists_branches_and_staff(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Barber', position='Барбер', company_id=1, fired=0, user_id=100))
    async_session.add(Staff(id=20, name='Admin', position='Администратор', company_id=1, fired=0, user_id=200))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/dashboard/plan/settings', params={'month': '2025-05'})
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['period'] == {'start': '2025-05-01', 'end': '2025-05-31'}
    assert data['last_saved_at'] is None
    assert [(row['company_id'], row['wax_pct']) for row in data['branches']] == [(1, None)]
    assert [(row['staff_id'], row['staff_category'], row['clients']) for row in data['staff']] == [
        (20, 'administrator', None),
        (10, 'barber', None),
    ]


@pytest.mark.asyncio
async def test_plan_settings_last_saved_at_is_scoped_to_allowed_branches(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Allowed', group_id=1),
        Company(id=2, title='Foreign', group_id=1),
        Staff(id=10, name='Allowed staff', position='Барбер', company_id=1),
        Staff(id=20, name='Foreign staff', position='Барбер', company_id=2),
        PlanBranchSetting(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=1,
            wax_pct=0.1,
            updated_at=datetime(2025, 5, 2),
        ),
        PlanBranchSetting(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=2,
            wax_pct=0.2,
            updated_at=datetime(2025, 5, 20),
        ),
        PlanStaffInput(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=2,
            staff_id=20,
            staff_category='barber',
            clients=50,
            updated_at=datetime(2025, 5, 25),
        ),
    ])
    await async_session.commit()

    data = await dashboard_service.fetch_plan_settings(
        async_session,
        '2025-05',
        allowed_company_ids=[1],
        force_allowed=True,
    )

    assert data['last_saved_at'] == '2025-05-02T00:00:00'
    assert [row['company_id'] for row in data['branches']] == [1]
    assert [row['staff_id'] for row in data['staff']] == [10]


@pytest.mark.asyncio
async def test_plan_settings_copy_from_month_does_not_write(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Barber', position='Барбер', company_id=1, fired=0, user_id=100))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        save_response = await client.post(
            '/dashboard/plan/settings',
            json={
                'month': '2025-05',
                'branches': [{'company_id': 1, 'wax_pct': '20', 'cosmo_price': 2000}],
                'staff': [{
                    'company_id': 1,
                    'staff_id': 10,
                    'staff_category': 'barber',
                    'clients': 100,
                    'avg_check_total': 3000,
                }],
            },
        )
        copy_response = await client.get(
            '/dashboard/plan/settings',
            params={'month': '2025-06', 'copy_from': '2025-05'},
        )
    app.dependency_overrides.clear()

    assert save_response.status_code == 200
    assert copy_response.status_code == 200
    data = copy_response.json()['data']
    assert data['month'] == '2025-06'
    assert data['copy_from'] == '2025-05'
    assert data['last_saved_at'] is None
    assert data['branches'][0]['wax_pct'] == 20.0
    assert data['staff'][0]['clients'] == 100.0

    june_branch_settings = (
        await async_session.execute(
            select(PlanBranchSetting).where(PlanBranchSetting.period_start == date(2025, 6, 1))
        )
    ).scalars().all()
    june_staff_inputs = (
        await async_session.execute(
            select(PlanStaffInput).where(PlanStaffInput.period_start == date(2025, 6, 1))
        )
    ).scalars().all()
    assert june_branch_settings == []
    assert june_staff_inputs == []


@pytest.mark.asyncio
async def test_plan_settings_copy_excludes_fired_staff_and_recalculates(async_session):
    august_start = date(2025, 8, 1)
    august_end = date(2025, 8, 31)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=10, name='Active barber', position='Барбер', company_id=1, fired=0),
        Staff(id=20, name='Former admin', position='Администратор', company_id=1, fired=1),
        PlanBranchSetting(
            period_start=august_start,
            period_end=august_end,
            company_id=1,
            wax_pct=0.2,
            updated_at=datetime(2025, 8, 1),
        ),
        PlanStaffInput(
            period_start=august_start,
            period_end=august_end,
            company_id=1,
            staff_id=10,
            staff_category='barber',
            clients=100,
            avg_check_total=3000,
            updated_at=datetime(2025, 8, 1),
        ),
        PlanStaffInput(
            period_start=august_start,
            period_end=august_end,
            company_id=1,
            staff_id=20,
            staff_category='administrator',
            reviews_qty=12,
            updated_at=datetime(2025, 8, 1),
        ),
        PlanStaffInput(
            period_start=date(2025, 9, 1),
            period_end=date(2025, 9, 30),
            company_id=1,
            staff_id=20,
            staff_category='administrator',
            reviews_qty=50,
            updated_at=datetime(2025, 9, 1),
        ),
        PlanMetric(
            period_start=date(2025, 9, 1),
            period_end=date(2025, 9, 30),
            company_id=1,
            staff_id=20,
            staff_category='administrator',
            metric_code='reviews_qty',
            value=50,
            source='legacy_sheet',
            updated_at=datetime(2025, 9, 1),
        ),
        PlanMetric(
            period_start=date(2025, 9, 1),
            period_end=date(2025, 9, 30),
            company_id=1,
            staff_id=None,
            metric_code='reviews_qty',
            value=50,
            source='legacy_sheet',
            updated_at=datetime(2025, 9, 1),
        ),
        PlanMetric(
            period_start=date(2025, 9, 1),
            period_end=date(2025, 9, 30),
            company_id=1,
            staff_id=10,
            staff_category='barber',
            metric_code='clients',
            value=999,
            source='legacy_sheet',
            updated_at=datetime(2025, 9, 1),
        ),
        PlanMetric(
            period_start=date(2025, 9, 1),
            period_end=date(2025, 9, 30),
            company_id=1,
            staff_id=None,
            metric_code='clients',
            value=999,
            source='legacy_sheet',
            updated_at=datetime(2025, 9, 1),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        copy_response = await client.get(
            '/dashboard/plan/settings',
            params={'month': '2025-09', 'copy_from': '2025-08'},
        )
        copied = copy_response.json()['data']
        save_response = await client.post(
            '/dashboard/plan/settings',
            json={
                'month': copied['month'],
                'copy_from': copied['copy_from'],
                'branches': copied['branches'],
                'staff': copied['staff'],
            },
        )
    app.dependency_overrides.clear()

    assert copy_response.status_code == 200
    assert [row['staff_id'] for row in copied['staff']] == [10]
    preview = {cell['code']: cell['value'] for cell in copied['branches'][0]['preview']}
    assert preview['clients'] == 100
    assert preview['revenue'] == 300000.0
    assert preview['wax_qty'] == 20
    assert 'reviews_qty' not in preview
    assert save_response.status_code == 200

    september_staff_ids = (
        await async_session.execute(
            select(PlanStaffInput.staff_id).where(
                PlanStaffInput.period_start == date(2025, 9, 1),
            )
        )
    ).scalars().all()
    assert september_staff_ids == [10]
    september_metrics = (
        await async_session.execute(
            select(PlanMetric.staff_id, PlanMetric.metric_code, PlanMetric.value).where(
                PlanMetric.period_start == date(2025, 9, 1),
                PlanMetric.metric_code.in_({'clients', 'reviews_qty'}),
            )
        )
    ).all()
    assert {
        (row.staff_id, row.metric_code): row.value
        for row in september_metrics
    } == {
        (None, 'clients'): 100.0,
        (10, 'clients'): 100.0,
    }


@pytest.mark.asyncio
async def test_plan_settings_save_generates_historical_plan_metrics(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Barber', position='Барбер', company_id=1, fired=0, user_id=100))
    async_session.add(Staff(id=20, name='Admin', position='Администратор', company_id=1, fired=0, user_id=200))
    await async_session.commit()

    def settings_payload(month, wax_pct, reviews_qty):
        return {
            'month': month,
            'branches': [{
                'company_id': 1,
                'wax_pct': wax_pct,
                'head_care_pct': 10,
                'face_care_pct': 5,
                'camouflage_pct': 5,
                'cosmo_pct': 10,
                'opz_pct': 25,
                'cosmo_price': 2000,
            }],
            'staff': [
                {
                    'company_id': 1,
                    'staff_id': 10,
                    'staff_category': 'barber',
                    'clients': 100,
                    'avg_check_total': 3000,
                },
                {
                    'company_id': 1,
                    'staff_id': 20,
                    'staff_category': 'administrator',
                    'clients': 90,
                    'reviews_qty': reviews_qty,
                    'cosmo_qty': 4,
                    'extra_services_qty': 12,
                    'extra_services_pct': 35,
                },
            ],
        }

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        may_save = await client.post('/dashboard/plan/settings', json=settings_payload('2025-05', '20', 12))
        june_save = await client.post('/dashboard/plan/settings', json=settings_payload('2025-06', 30, 20))
        may_plan = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-05-10', 'end_date': '2025-05-20', 'company_id': 1},
        )
        june_plan = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-06-15', 'company_id': 1},
        )
        june_resave = await client.post('/dashboard/plan/settings', json=settings_payload('2025-06', 40, 25))
        june_plan_after_resave = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-06-15', 'company_id': 1},
        )
        may_plan_after_resave = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-05-10', 'end_date': '2025-05-20', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert may_save.status_code == 200
    assert june_save.status_code == 200
    assert june_resave.status_code == 200

    may_data = may_plan.json()['data']
    assert may_data['plan_period'] == {'start': '2025-05-01', 'end': '2025-05-31'}
    may_parent_cells = {cell['code']: cell for cell in may_data['parent_group']['metrics']}
    assert may_parent_cells['clients']['plan'] == 100.0
    assert may_parent_cells['wax_qty']['plan'] == 20.0
    assert may_parent_cells['extra_services_qty']['plan'] == 40.0
    assert may_parent_cells['extra_services_pct']['plan'] == 40.0
    assert may_parent_cells['reviews_qty']['plan'] == 12.0
    assert may_parent_cells['cosmo_qty']['plan'] == 10.0

    may_admin = next(group for group in may_data['groups'] if group['category'] == 'administrator')
    may_admin_cells = {cell['code']: cell for cell in may_admin['metrics']}
    assert may_admin_cells['clients']['plan'] == 90.0
    assert may_admin_cells['reviews_qty']['plan'] == 12.0
    assert may_admin_cells['cosmo_qty']['plan'] == 4.0
    assert may_admin_cells['extra_services_qty']['plan'] == 12.0
    assert may_admin_cells['extra_services_pct']['plan'] == 35.0

    june_cells = {cell['code']: cell for cell in june_plan.json()['data']['parent_group']['metrics']}
    assert june_cells['wax_qty']['plan'] == 30.0
    assert june_cells['reviews_qty']['plan'] == 20.0

    june_after_cells = {
        cell['code']: cell
        for cell in june_plan_after_resave.json()['data']['parent_group']['metrics']
    }
    assert june_after_cells['wax_qty']['plan'] == 40.0
    assert june_after_cells['reviews_qty']['plan'] == 25.0

    may_after_cells = {
        cell['code']: cell
        for cell in may_plan_after_resave.json()['data']['parent_group']['metrics']
    }
    assert may_after_cells['wax_qty']['plan'] == 20.0
    assert may_after_cells['reviews_qty']['plan'] == 12.0


@pytest.mark.asyncio
async def test_plan_settings_preserve_period_plan_for_fired_administrator(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=20, name='Former admin', position='Администратор', company_id=1, fired=1),
        PlanMetric(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=1,
            staff_id=20,
            staff_category='administrator',
            metric_code='extra_services_qty',
            value=12,
            source=dashboard_service.PLAN_SETTINGS_SOURCE,
            updated_at=datetime(2025, 5, 1),
        ),
        PlanMetric(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=1,
            staff_id=20,
            staff_category='administrator',
            metric_code='extra_services_pct',
            value=35,
            source='legacy_sheet',
            updated_at=datetime(2025, 5, 1),
        ),
        *[
            PlanMetric(
                period_start=date(2025, 5, 1),
                period_end=date(2025, 5, 31),
                company_id=1,
                staff_id=staff_id,
                staff_category='administrator' if staff_id is not None else None,
                metric_code=metric_code,
                value=value,
                source='google_sheet',
                updated_at=datetime(2025, 5, 1),
            )
            for staff_id in (None, 20)
            for metric_code, value in (
                ('revenue', 1001),
                ('wax_qty', 2),
                ('camouflage_qty', 3),
                ('face_care_qty', 4),
                ('head_care_qty', 5),
                ('cosmo_qty', 6),
                ('cosmo_sum', 600),
                ('opz_qty', 7),
            )
        ],
    ])
    await async_session.commit()

    settings = await dashboard_service.fetch_plan_settings(async_session, '2025-05')
    former = next(row for row in settings['staff'] if row['staff_id'] == 20)
    assert former['is_active'] is False
    assert former['extra_services_qty'] == 12.0

    await dashboard_service.save_plan_settings(
        async_session,
        '2025-05',
        [{'company_id': 1}],
        [],
    )

    preserved_input = await async_session.scalar(
        select(PlanStaffInput).where(
            PlanStaffInput.period_start == date(2025, 5, 1),
            PlanStaffInput.staff_id == 20,
        )
    )
    preserved_metric = await async_session.scalar(
        select(PlanMetric).where(
            PlanMetric.period_start == date(2025, 5, 1),
            PlanMetric.staff_id == 20,
            PlanMetric.metric_code == 'extra_services_qty',
        )
    )
    assert preserved_input.extra_services_qty == 12.0
    assert preserved_metric.value == 12.0
    legacy_rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 5, 1),
                PlanMetric.metric_code.in_({
                    'revenue',
                    'wax_qty',
                    'camouflage_qty',
                    'face_care_qty',
                    'head_care_qty',
                    'cosmo_qty',
                    'cosmo_sum',
                    'opz_qty',
                }),
            )
        )
    ).scalars().all()
    preserved_by_scope = {
        (row.staff_id, row.metric_code): row.value
        for row in legacy_rows
    }
    assert len(preserved_by_scope) == 16
    assert preserved_by_scope[(None, 'revenue')] == 1001.0
    assert preserved_by_scope[(20, 'opz_qty')] == 7.0
    assert preserved_by_scope[(20, 'cosmo_sum')] == 600.0


@pytest.mark.asyncio
async def test_plan_settings_percent_api_uses_one_to_hundred(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    await async_session.commit()

    async def override_db():
        yield async_session

    def payload(value):
        return {
            'month': '2025-05',
            'branches': [{'company_id': 1, 'wax_pct': value}],
            'staff': [],
        }

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        zero = await client.post('/dashboard/plan/settings', json=payload(0))
        too_large = await client.post('/dashboard/plan/settings', json=payload(101))
        saved = await client.post('/dashboard/plan/settings', json=payload(7.5))
    app.dependency_overrides.clear()

    assert zero.status_code == 400
    assert too_large.status_code == 400
    assert saved.status_code == 200
    assert saved.json()['data']['branches'][0]['wax_pct'] == 7.5

    setting = await async_session.scalar(
        select(PlanBranchSetting).where(
            PlanBranchSetting.period_start == date(2025, 5, 1),
            PlanBranchSetting.company_id == 1,
        )
    )
    assert setting.wax_pct == pytest.approx(0.075)


@pytest.mark.asyncio
async def test_admin_extra_service_plans_are_independent_and_allow_zero_percent(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=20, name='Admin', position='Администратор', company_id=1, fired=0),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    def payload(percent):
        return {
            'month': '2025-05',
            'branches': [{'company_id': 1}],
            'staff': [{
                'company_id': 1,
                'staff_id': 20,
                'staff_category': 'administrator',
                'extra_services_qty': 15,
                'extra_services_pct': percent,
            }],
        }

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        saved = await client.post('/dashboard/plan/settings', json=payload(0))
        rejected = await client.post('/dashboard/plan/settings', json=payload(101))
    app.dependency_overrides.clear()

    assert saved.status_code == 200
    assert rejected.status_code == 400
    admin = saved.json()['data']['staff'][0]
    assert admin['extra_services_qty'] == 15.0
    assert admin['extra_services_pct'] == 0.0


@pytest.mark.asyncio
async def test_plan_settings_preserve_category_only_role_snapshots(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=20, name='Admin', position='Администратор', company_id=1, fired=0),
        Staff(id=30, name='Former admin', position='Барбер', company_id=1, fired=1),
        PlanStaffInput(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=1,
            staff_id=30,
            staff_category='administrator',
            updated_at=datetime(2025, 5, 1),
        ),
    ])
    await async_session.commit()

    await dashboard_service.save_plan_settings(
        async_session,
        '2025-05',
        [{'company_id': 1}],
        [{
            'company_id': 1,
            'staff_id': 20,
            'staff_category': 'administrator',
        }],
    )

    rows = (
        await async_session.execute(
            select(PlanStaffInput).where(
                PlanStaffInput.period_start == date(2025, 5, 1),
                PlanStaffInput.company_id == 1,
            )
        )
    ).scalars().all()
    assert {
        int(row.staff_id): row.staff_category
        for row in rows
    } == {20: 'administrator', 30: 'administrator'}
    assert all(row.clients is None for row in rows)


@pytest.mark.asyncio
async def test_plan_settings_edit_replaces_derived_legacy_metrics(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=10, name='Barber', position='Барбер', company_id=1),
        *[
            PlanMetric(
                period_start=date(2025, 5, 1),
                period_end=date(2025, 5, 31),
                company_id=1,
                staff_id=10,
                staff_category='barber',
                metric_code=metric_code,
                value=value,
                source='legacy_sheet',
                updated_at=datetime(2025, 5, 1),
            )
            for metric_code, value in (
                ('clients', 100),
                ('avg_check_total', 3000),
                ('revenue', 12345),
            )
        ],
        PlanMetric(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=1,
            staff_id=None,
            metric_code='revenue',
            value=12345,
            source='legacy_sheet',
            updated_at=datetime(2025, 5, 1),
        ),
    ])
    await async_session.commit()

    await dashboard_service.save_plan_settings(
        async_session,
        '2025-05',
        [{'company_id': 1}],
        [{
            'company_id': 1,
            'staff_id': 10,
            'staff_category': 'barber',
            'clients': 120,
            'avg_check_total': 3000,
        }],
    )

    revenue_rows = (
        await async_session.execute(
            select(PlanMetric.staff_id, PlanMetric.value, PlanMetric.source).where(
                PlanMetric.period_start == date(2025, 5, 1),
                PlanMetric.company_id == 1,
                PlanMetric.metric_code == 'revenue',
            )
        )
    ).all()
    assert {
        row.staff_id: (row.value, row.source)
        for row in revenue_rows
    } == {
        None: (360000.0, dashboard_service.PLAN_SETTINGS_SOURCE),
        10: (360000.0, dashboard_service.PLAN_SETTINGS_SOURCE),
    }


@pytest.mark.asyncio
async def test_plan_fact_does_not_fallback_to_previous_plan_period(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(
        PlanMetric(
            period_start=date(2025, 5, 1),
            period_end=date(2025, 5, 31),
            company_id=1,
            staff_id=None,
            staff_category=None,
            metric_code='revenue',
            value=500000.0,
            updated_at=datetime(2025, 5, 1, 0, 0, 0),
        )
    )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-10', 'end_date': '2025-06-20', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['plan_period'] == {'start': '2025-06-10', 'end': '2025-06-20'}
    cells = {cell['code']: cell for cell in data['parent_group']['metrics']}
    assert cells['revenue']['plan'] is None
    assert cells['revenue']['remaining'] is None
    assert cells['revenue']['completion_pct'] is None


@pytest.mark.asyncio
async def test_admin_opz_attributes_to_creator(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, user_id=500))
    async_session.add(Client(id=1, name='C', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 10)))
    now = datetime(2025, 1, 1)
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1, company_id=1, staff_id=1, client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600, attendance=1, created_user_id=999,
        ),
        Appointment(
            id=2, company_id=1, staff_id=1, client_id=1,
            date=date(2025, 2, 10),
            datetime=datetime(2025, 2, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 18, 0, 0),
            seance_length=3600, attendance=0, created_user_id=500,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='opz_qty',
            value=1.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            metric_code='opz_qty',
            value=1.0,
            updated_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    groups = r.json()['data']['groups']
    admin_group = next(g for g in groups if g['category'] == 'administrator')
    barber_group = next(g for g in groups if g['category'] == 'barber')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    barber_cells = {cell['code']: cell for cell in barber_group['metrics']}
    assert admin_cells['opz_qty']['fact'] == 1.0
    assert barber_cells['opz_qty']['fact'] == 1.0


@pytest.mark.asyncio
async def test_admin_opz_distributes_unknown_creator_by_schedule(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Admin A', position='Администратор', company_id=1, user_id=500))
    async_session.add(Staff(id=3, name='Admin B', position='Администратор', company_id=1, user_id=501))
    async_session.add(Client(id=1, name='C', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 10)))
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1, company_id=1, staff_id=1, client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 9, 12, 0, 0),
            seance_length=3600, attendance=1, created_user_id=999,
        ),
        Appointment(
            id=2, company_id=1, staff_id=1, client_id=1,
            date=date(2025, 2, 10),
            datetime=datetime(2025, 2, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 18, 0, 0),
            seance_length=3600, attendance=0, created_user_id=999,
        ),
        StaffSchedule(staff_id=2, company_id=1, date=date(2025, 1, 10),
                      slot_from=time(10, 0), slot_to=time(14, 0)),
        StaffSchedule(staff_id=3, company_id=1, date=date(2025, 1, 10),
                      slot_from=time(14, 0), slot_to=time(22, 0)),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='opz_qty',
            value=1.0,
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        admin_a_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'staff_id': 2},
        )
        admin_b_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'staff_id': 3},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    groups = r.json()['data']['groups']
    barber_group = next(g for g in groups if g['category'] == 'barber')
    admin_opz_by_staff = {
        group['staff_id']: next(m for m in group['metrics'] if m['code'] == 'opz_qty')['fact']
        for group in groups
        if group['category'] == 'administrator'
    }
    admin_opz_pct_by_staff = {
        group['staff_id']: next(m for m in group['metrics'] if m['code'] == 'opz_pct')['fact']
        for group in groups
        if group['category'] == 'administrator'
    }
    barber_opz = next(m for m in barber_group['metrics'] if m['code'] == 'opz_qty')['fact']
    assert barber_opz == 1.0
    assert admin_opz_by_staff == {2: 0.0, 3: 1.0}
    assert sum(admin_opz_by_staff.values()) == barber_opz

    selected_admin_a = admin_a_response.json()['data']['selected_staff_plan']
    selected_admin_b = admin_b_response.json()['data']['selected_staff_plan']
    selected_admin_a_cells = {row['code']: row for row in selected_admin_a['metrics']}
    selected_admin_b_cells = {row['code']: row for row in selected_admin_b['metrics']}
    assert selected_admin_a_cells['opz_qty']['fact'] == admin_opz_by_staff[2] == 0.0
    assert selected_admin_b_cells['opz_qty']['fact'] == admin_opz_by_staff[3] == 1.0
    assert selected_admin_a_cells['opz_pct']['fact'] == admin_opz_pct_by_staff[2]
    assert selected_admin_b_cells['opz_pct']['fact'] == admin_opz_pct_by_staff[3]


@pytest.mark.asyncio
async def test_admin_event_without_time_uses_only_same_day_schedule(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=2, name='Previous admin', position='Администратор', company_id=1),
        Staff(id=3, name='Current admin', position='Администратор', company_id=1),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 9),
            slot_from=time(10),
            slot_to=time(22),
        ),
        StaffSchedule(
            staff_id=3,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(22),
        ),
    ])
    await async_session.commit()

    counts = await dashboard_service._admin_event_counts(
        async_session,
        date(2025, 1, 10),
        date(2025, 1, 10),
        1,
        [2, 3],
        {2: None, 3: None},
        [
            dashboard_service.AdminAssignmentEvent(
                event_id=1,
                event_date=date(2025, 1, 10),
                event_moment=None,
                created_user_id=None,
            )
        ],
    )

    assert counts == {2: 0, 3: 1}


@pytest.mark.asyncio
async def test_admin_extra_services_use_moscow_shifts_and_credit_overlaps(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(id=2, name='Admin A', position='Администратор', company_id=1),
        Staff(id=3, name='Admin B', position='Администратор', company_id=1),
        Staff(id=4, name='Admin without shift', position='Администратор', company_id=1),
        Service(id=10, title='Care', company_id=1),
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 7, 30),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 30),
            attendance=1,
        ),
        Transaction(
            id=1,
            appointment_id=1,
            company_id=1,
            service_id=10,
            service_title='Care',
            amount=2,
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(12),
        ),
        # A duplicate upstream slot must not double the administrator fact.
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(12),
        ),
        StaffSchedule(
            staff_id=3,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(16),
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=datetime(2025, 1, 31),
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
    )

    branch_cells = {cell['code']: cell for cell in result['parent_group']['metrics']}
    admin_cells = {
        group['staff_id']: {cell['code']: cell for cell in group['metrics']}
        for group in result['groups']
        if group['category'] == 'administrator'
    }
    assert branch_cells['extra_services_qty']['fact'] == 2.0
    assert branch_cells['extra_services_pct']['fact'] == 100.0
    assert admin_cells[2]['extra_services_qty']['fact'] == 2.0
    assert admin_cells[2]['extra_services_pct']['fact'] == 200.0
    assert admin_cells[3]['extra_services_qty']['fact'] == 2.0
    assert admin_cells[3]['extra_services_pct']['fact'] == 100.0
    assert admin_cells[4]['extra_services_qty']['fact'] == 0.0
    assert admin_cells[4]['extra_services_pct']['fact'] == 0.0


@pytest.mark.asyncio
async def test_admin_extra_services_include_early_moscow_visit_before_utc_day_rollover(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            timezone='Europe/Moscow',
            reporting_start_date=date(2025, 1, 2),
        ),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
        Service(id=10, title='Care', company_id=1),
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 2),
            slot_from=time(0),
            slot_to=time(2),
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 2),
            synced_at=datetime(2025, 1, 2),
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 2),
            datetime=datetime(2025, 1, 1, 21, 15),
            attendance=1,
        ),
        Transaction(
            id=1,
            appointment_id=1,
            company_id=1,
            service_id=10,
            service_title='Care',
            amount=1,
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 2),
        date(2025, 1, 2),
        company_id=1,
        factual_at=datetime(2025, 1, 1, 21, 30),
    )

    branch_cells = {cell['code']: cell for cell in result['parent_group']['metrics']}
    admin = next(group for group in result['groups'] if group['staff_id'] == 2)
    admin_cells = {cell['code']: cell for cell in admin['metrics']}
    assert branch_cells['extra_services_qty']['fact'] == 1.0
    assert admin_cells['extra_services_qty']['fact'] == 1.0


@pytest.mark.asyncio
async def test_administrator_service_scope_is_explicitly_separated_from_personal_reports(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2025, 2, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            timezone='Europe/Moscow',
            reporting_start_date=date(2025, 1, 1),
        ),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
        Service(id=10, title='Extra', company_id=1),
        Service(id=11, title='Regular', company_id=1),
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(12),
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=datetime(2025, 1, 31),
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 7, 30),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 30),
            attendance=1,
        ),
        Transaction(
            id=1,
            appointment_id=1,
            company_id=1,
            service_id=10,
            service_title='Extra',
            amount=2,
        ),
        Transaction(
            id=2,
            appointment_id=1,
            company_id=1,
            service_id=11,
            service_title='Regular',
            amount=1,
        ),
        Transaction(
            id=3,
            appointment_id=2,
            company_id=1,
            service_id=10,
            service_title='Extra',
            amount=5,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2025, 1, 10, 7, 30),
            amount=200,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_id=11,
            sold_item_type='service',
            date=datetime(2025, 1, 10, 7, 31),
            amount=100,
        ),
        FinancialTransaction(
            id=3,
            company_id=1,
            master_id=1,
            record_id=2,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2025, 1, 10, 12, 30),
            amount=500,
        ),
    ])
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 10),
        date(2025, 1, 10),
        company_id=1,
        staff_id=2,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    service_report = await dashboard_reports.fetch_report_data(
        async_session,
        'service_combos',
        date(2025, 1, 10),
        date(2025, 1, 10),
        company_id=1,
        staff_id=2,
    )

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        bundle_response = await client.get(
            '/dashboard/bundle',
            params={
                'start_date': '2025-01-10',
                'end_date': '2025-01-10',
                'company_id': 1,
                'staff_id': 2,
            },
        )
        extra_response = await client.get(
            '/dashboard/widget/extra_services',
            params={
                'start_date': '2025-01-10',
                'end_date': '2025-01-10',
                'company_id': 1,
                'staff_id': 2,
            },
        )
    app.dependency_overrides.clear()

    assert summary['source_status'] == 'ready'
    assert summary['service_attribution']['mode'] == 'administrator_schedule'
    assert summary['service_attribution']['appointment_count'] == 1
    assert summary['service_attribution']['unique_client_count'] == 1
    assert summary['revenue']['extra_service_count'] == 2.0
    assert summary['revenue']['extra_service_revenue'] == 200.0
    assert summary['visit_metrics']['extra_services_per_appointment_pct'] == 200.0
    assert summary['visit_metrics']['extra_service_clients_pct'] == 100.0

    report_cards = {card['label']: card['value'] for card in service_report['cards']}
    report_extra = next(
        table for table in service_report['tables'] if table['id'] == 'extra_services'
    )
    assert service_report['source_status'] == 'ready'
    assert service_report['calculation_scope']['mode'] == 'personal'
    assert service_report['raw']['service_attribution']['mode'] == 'master'
    assert report_cards['Услуг оказано'] == 0.0
    assert report_cards['Выручка услуг'] == 0.0
    assert report_extra['rows'] == []

    assert bundle_response.status_code == 200
    bundle = bundle_response.json()['data']
    assert bundle['source_status'] == 'ready'
    assert bundle['summary']['revenue']['extra_service_count'] == 2.0
    assert {row['title'] for row in bundle['top_services']} == {'Extra', 'Regular'}
    assert bundle['extra_services'][0]['sold'] == 2
    assert extra_response.status_code == 200
    assert extra_response.json()['source_status'] == 'ready'
    assert extra_response.json()['mode'] == 'master'
    assert extra_response.json()['data'] == []


@pytest.mark.asyncio
async def test_administrator_service_scope_fails_closed_without_schedule_coverage(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2025, 2, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            reporting_start_date=date(2025, 1, 1),
        ),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
        Service(id=10, title='Extra', company_id=1),
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 9),
            attendance=1,
        ),
        Transaction(
            id=1,
            appointment_id=1,
            company_id=1,
            service_id=10,
            service_title='Extra',
            amount=1,
        ),
    ])
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=2,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    report = await dashboard_reports.fetch_report_data(
        async_session,
        'service_combos',
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=2,
    )

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        bundle_response = await client.get(
            '/dashboard/bundle',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'staff_id': 2,
            },
        )
        extra_response = await client.get(
            '/dashboard/widget/extra_services',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'staff_id': 2,
            },
        )
        top_response = await client.get(
            '/dashboard/widget/top_services',
            params={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'staff_id': 2,
            },
        )
    app.dependency_overrides.clear()

    assert summary['source_status'] == 'partial'
    assert summary['missing_sources'] == [dashboard_service.STAFF_SCHEDULE_SOURCE]
    assert summary['revenue']['extra_service_count'] is None
    assert summary['visit_metrics']['extra_services_per_appointment_pct'] is None
    assert report['source_status'] == 'ready'
    assert report['calculation_scope']['mode'] == 'personal'
    assert report['missing_sources'] == []
    assert report['raw']['services'] == []
    assert report['raw']['extra_services'] == []
    assert bundle_response.json()['data']['source_status'] == 'partial'
    assert bundle_response.json()['data']['extra_services'] == []
    assert extra_response.json()['source_status'] == 'ready'
    assert extra_response.json()['mode'] == 'master'
    assert extra_response.json()['missing_sources'] == []
    assert extra_response.json()['data'] == []
    assert top_response.json()['source_status'] == 'ready'
    assert top_response.json()['mode'] == 'master'
    assert top_response.json()['data'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('january_category', 'february_category', 'current_position', 'expected_qty'),
    [
        ('administrator', 'barber', 'Администратор', 7),
        ('barber', 'administrator', 'Барбер', 9),
    ],
)
async def test_administrator_service_scope_follows_monthly_role_transitions(
    async_session,
    january_category,
    february_category,
    current_position,
    expected_qty,
):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            timezone='Europe/Moscow',
            reporting_start_date=date(2025, 1, 1),
        ),
        Staff(id=1, name='Other barber', position='Барбер', company_id=1),
        Staff(id=2, name='Role transition', position=current_position, company_id=1),
        Service(id=10, title='Extra', company_id=1),
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
        PlanStaffInput(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category=january_category,
            updated_at=datetime(2025, 1, 1),
        ),
        PlanStaffInput(
            period_start=date(2025, 2, 1),
            period_end=date(2025, 2, 28),
            company_id=1,
            staff_id=2,
            staff_category=february_category,
            updated_at=datetime(2025, 2, 1),
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(12),
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 2, 10),
            slot_from=time(10),
            slot_to=time(12),
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 2, 28),
            synced_at=datetime(2025, 2, 28),
        ),
    ])
    await async_session.flush()
    appointments = [
        (1, date(2025, 1, 10), 1, 1),
        (2, date(2025, 1, 10), 2, 2),
        (3, date(2025, 2, 10), 1, 3),
        (4, date(2025, 2, 10), 2, 4),
    ]
    for appointment_id, appointment_date, master_id, qty in appointments:
        async_session.add(Appointment(
            id=appointment_id,
            company_id=1,
            staff_id=master_id,
            date=appointment_date,
            datetime=datetime.combine(appointment_date, time(7, 30)),
            attendance=1,
        ))
        async_session.add(Transaction(
            id=appointment_id,
            appointment_id=appointment_id,
            company_id=1,
            service_id=10,
            service_title='Extra',
            amount=qty,
        ))
    await async_session.commit()

    rows = await dashboard_service.fetch_extra_services(
        async_session,
        date(2025, 1, 1),
        date(2025, 2, 28),
        company_id=1,
        staff_id=2,
        factual_at=datetime(2025, 3, 1),
        use_administrator_schedule=True,
    )
    plan_fact = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 2, 28),
        company_id=1,
        factual_at=datetime(2025, 3, 1),
    )

    assert rows[0]['sold'] == expected_qty
    staff_group = next(
        group for group in plan_fact['groups']
        if group['staff_id'] == 2
    )
    cells = {cell['code']: cell for cell in staff_group['metrics']}
    assert cells['extra_services_qty']['fact'] == expected_qty
    leaderboard_key = (
        'extra_services_admin'
        if staff_group['category'] == 'administrator'
        else 'extra_services_barber'
    )
    leaderboard = plan_fact['staff_leaderboards'][leaderboard_key]
    assert next(row for row in leaderboard if row['staff_id'] == 2)['qty'] == expected_qty
    other_key = (
        'extra_services_barber'
        if staff_group['category'] == 'administrator'
        else 'extra_services_admin'
    )
    assert all(row['staff_id'] != 2 for row in plan_fact['staff_leaderboards'][other_key])


@pytest.mark.asyncio
async def test_opz_uses_branch_local_date_for_year_and_role_transition(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            timezone='Europe/Moscow',
            reporting_start_date=date(2024, 12, 1),
        ),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(id=2, name='Role transition', position='Барбер', company_id=1),
        Client(
            id=1,
            name='Client',
            company_id=1,
            visits_count=1,
            last_visit_date=date(2024, 12, 31),
        ),
        PlanStaffInput(
            period_start=date(2024, 12, 1),
            period_end=date(2024, 12, 31),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            updated_at=datetime(2024, 12, 1),
        ),
        PlanStaffInput(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category='barber',
            updated_at=datetime(2025, 1, 1),
        ),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 12, 31),
            datetime=datetime(2024, 12, 31, 12),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 10),
            create_date=datetime(2024, 12, 31, 22, 30),
            attendance=0,
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 1),
            slot_from=time(0),
            slot_to=time(3),
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 11, 30),
            period_end=date(2025, 1, 31),
            synced_at=datetime(2025, 1, 31),
        ),
    ])
    await async_session.commit()

    events = await dashboard_service._opz_events(
        async_session,
        date(2024, 12, 1),
        date(2025, 1, 31),
        1,
        factual_at=datetime(2025, 2, 1),
    )
    year_facts = await dashboard_service.fetch_opz_year_facts(
        async_session,
        date(2024, 12, 1),
        date(2025, 1, 31),
        1,
        None,
        factual_at=datetime(2025, 2, 1),
    )
    plan_fact = await fetch_plan_fact(
        async_session,
        date(2024, 12, 1),
        date(2025, 1, 31),
        company_id=1,
        include_all_staff_in_leaderboards=True,
        factual_at=datetime(2025, 2, 1),
    )

    assert [event.event_date for event in events] == [date(2025, 1, 1)]
    assert year_facts['counts'] == {2025: 1.0}
    opz_by_staff = {
        group['staff_id']: next(
            cell['fact'] for cell in group['metrics']
            if cell['code'] == 'opz_qty'
        )
        for group in plan_fact['groups']
    }
    assert opz_by_staff == {1: 1.0, 2: 0.0}


@pytest.mark.asyncio
async def test_schedule_coverage_uses_the_callers_factual_snapshot(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            reporting_start_date=date(2025, 1, 1),
        ),
        Appointment(
            id=1,
            company_id=1,
            date=date(2025, 1, 20),
            datetime=None,
            attendance=1,
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=datetime(2025, 1, 31),
        ),
    ])
    await async_session.commit()

    info = await dashboard_service._staff_schedule_coverage_info(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        1,
        factual_at=datetime(2025, 1, 15),
    )

    assert info['ready'] is True
    assert info['invalid_event_timestamps'] is False


@pytest.mark.asyncio
async def test_administrator_service_scope_fails_closed_without_appointment_time(
    async_session,
):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            reporting_start_date=date(2025, 1, 1),
        ),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=None,
            attendance=1,
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(22),
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=datetime(2025, 1, 31),
        ),
    ])
    await async_session.commit()

    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=2,
        include_appointments_breakdown=False,
        factual_at=datetime(2025, 2, 1),
    )
    plan_fact = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        factual_at=datetime(2025, 2, 1),
    )

    admin = next(group for group in plan_fact['groups'] if group['staff_id'] == 2)
    cells = {cell['code']: cell for cell in admin['metrics']}
    assert summary['source_status'] == 'partial'
    assert summary['missing_sources'] == ['appointments_detail']
    assert summary['revenue']['extra_service_count'] is None
    assert cells['extra_services_qty']['fact'] is None
    assert any(
        item['code'] == 'appointment_timestamp_coverage'
        for item in plan_fact['diagnostics']
    )


@pytest.mark.asyncio
async def test_admin_extra_services_are_unavailable_without_schedule_coverage(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            reporting_start_date=date(2025, 1, 1),
        ),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
    )

    admin = next(group for group in result['groups'] if group['staff_id'] == 2)
    cells = {cell['code']: cell for cell in admin['metrics']}
    assert cells['extra_services_qty']['fact'] is None
    assert cells['extra_services_qty']['status'] == 'partial'
    assert cells['extra_services_pct']['fact'] is None
    assert cells['extra_services_pct']['status'] == 'partial'
    assert result['staff_leaderboards']['extra_services_admin_rankings']['qty'] == []
    assert result['staff_leaderboards']['_partial_reasons'] == ['staff_schedules']
    diagnostic = next(item for item in result['diagnostics'] if item['code'] == 'staff_schedule_coverage')
    assert diagnostic['required_start'] == '2024-12-31'
    assert diagnostic['required_end'] == '2025-01-31'
    assert diagnostic['covered_start'] is None
    assert diagnostic['covered_end'] is None


@pytest.mark.asyncio
async def test_schedule_coverage_uses_first_appointment_when_reporting_start_is_missing(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Appointment(
            id=1,
            company_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12),
            attendance=1,
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2025, 1, 9),
            period_end=date(2025, 1, 31),
            synced_at=datetime(2025, 1, 31),
        ),
    ])
    await async_session.commit()

    coverage = await dashboard_service._staff_schedule_coverage_info(
        async_session,
        date(2020, 1, 1),
        date(2025, 1, 31),
        1,
    )

    assert coverage['ready'] is True
    assert coverage['required_start'] == date(2025, 1, 9)


async def _seed_plan_fact_normalization_case(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Visible Barber', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, user_id=500))
    async_session.add(Staff(id=3, name='Hidden Barber', position='Барбер', company_id=1))
    async_session.add_all([
        Client(id=1, name='Visible Client', company_id=1),
        Client(id=2, name='Hidden Client', company_id=1),
    ])
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 1, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=500,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 2, 10),
            datetime=datetime(2025, 2, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 10, 18, 0, 0),
            seance_length=3600,
            attendance=0,
            created_user_id=500,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=3,
            client_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12, 0, 0),
            create_date=datetime(2025, 1, 1, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=500,
        ),
        Appointment(
            id=4,
            company_id=1,
            staff_id=3,
            client_id=2,
            date=date(2025, 2, 11),
            datetime=datetime(2025, 2, 11, 12, 0, 0),
            create_date=datetime(2025, 1, 11, 18, 0, 0),
            seance_length=3600,
            attendance=0,
            created_user_id=500,
        ),
        StaffSchedule(staff_id=2, company_id=1, date=date(2025, 1, 10),
                      slot_from=time(10, 0), slot_to=time(22, 0)),
        StaffSchedule(staff_id=2, company_id=1, date=date(2025, 1, 11),
                      slot_from=time(10, 0), slot_to=time(22, 0)),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='clients',
            value=1.0,
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.commit()


@pytest.mark.asyncio
async def test_plan_fact_branch_opz_matches_overview_even_with_hidden_staff(async_session):
    await _seed_plan_fact_normalization_case(async_session)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        overview = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    branch_cells = {cell['code']: cell for cell in data['parent_group']['metrics']}
    overview_metrics = overview.json()['data']['visit_metrics']

    assert branch_cells['clients']['fact'] == 2.0
    assert branch_cells['opz_qty']['fact'] == overview_metrics['opz_qty'] == 2.0
    assert branch_cells['opz_pct']['fact'] == overview_metrics['opz_pct'] == 100.0


@pytest.mark.asyncio
async def test_plan_fact_network_opz_matches_overview_even_with_hidden_staff(async_session):
    await _seed_plan_fact_normalization_case(async_session)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        branch_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        network_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        overview = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert branch_response.status_code == 200
    assert network_response.status_code == 200
    branch_cells = {cell['code']: cell for cell in branch_response.json()['data']['parent_group']['metrics']}
    network_groups = network_response.json()['data']['groups']
    network_cells = {cell['code']: cell for cell in network_groups[0]['metrics']}
    network_branch = next(group for group in network_groups if group['scope'] == 'branch')
    network_branch_cells = {cell['code']: cell for cell in network_branch['metrics']}
    overview_metrics = overview.json()['data']['visit_metrics']

    assert network_branch_cells['clients']['fact'] == branch_cells['clients']['fact'] == 2.0
    assert network_branch_cells['opz_qty']['fact'] == branch_cells['opz_qty']['fact'] == 2.0
    assert network_branch_cells['opz_pct']['fact'] == branch_cells['opz_pct']['fact'] == 100.0
    assert network_cells['clients']['fact'] == 2.0
    assert network_cells['opz_qty']['fact'] == overview_metrics['opz_qty'] == 2.0
    assert network_cells['opz_pct']['fact'] == overview_metrics['opz_pct'] == 100.0


@pytest.mark.asyncio
async def test_admin_without_user_id_preserves_staff_attribution_in_batch(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, user_id=None))
    async_session.add(Client(id=1, name='C', company_id=1))
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=2,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=1,
            sold_item_type='service',
            master_id=2,
            company_id=1,
        ),
        FinancialTransaction(
            id=2,
            date=datetime(2025, 1, 10, 12, 5, 0),
            amount=300.0,
            record_id=1,
            sold_item_type='goods_transaction',
            master_id=2,
            company_id=1,
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        include_all_staff_in_leaderboards=True,
    )

    admin_group = next(group for group in result['groups'] if group['staff_id'] == 2)
    cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert cells['revenue']['fact'] == 1300.0
    assert cells['avg_check_total']['fact'] == 1300.0
    assert cells['clients']['fact'] == 1.0


@pytest.mark.asyncio
async def test_unmatched_goods_payment_is_not_attributed_to_appointment_master(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=1, name='External match', position='Барбер', company_id=1),
        Staff(id=2, name='Local fallback', position='Барбер', company_id=1),
    ])
    await async_session.flush()

    # The payment has no matching stock operation, so neither possible appointment
    # master is evidence of who sold the product.
    async_session.add_all([
        Appointment(
            id=10,
            external_id=77,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            attendance=1,
        ),
        Appointment(
            id=77,
            external_id=None,
            company_id=1,
            staff_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12, 0, 0),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=300.0,
            record_id=77,
            sold_item_type='goods_transaction',
            master_id=None,
            company_id=1,
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        include_all_staff_in_leaderboards=True,
    )

    parent_cells = {cell['code']: cell for cell in result['parent_group']['metrics']}
    staff_group = next(group for group in result['groups'] if group['staff_id'] == 1)
    fallback_group = next(group for group in result['groups'] if group['staff_id'] == 2)
    staff_cells = {cell['code']: cell for cell in staff_group['metrics']}
    fallback_cells = {cell['code']: cell for cell in fallback_group['metrics']}
    external_rows = await dashboard_service.fetch_paid_goods_rows(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=1,
    )
    fallback_rows = await dashboard_service.fetch_paid_goods_rows(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=2,
    )
    assert parent_cells['revenue']['fact'] == 300.0
    assert staff_cells['revenue']['fact'] == 0.0
    assert fallback_cells['revenue']['fact'] == 0.0
    assert external_rows == []
    assert fallback_rows == []


@pytest.mark.asyncio
async def test_staff_scoped_plan_fact_omits_branch_average_check_leaderboard(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add(Appointment(
        id=1,
        company_id=1,
        staff_id=1,
        date=date(2025, 1, 10),
        datetime=datetime(2025, 1, 10, 12, 0, 0),
        attendance=1,
    ))
    now = datetime(2025, 1, 1)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=None,
            metric_code='avg_check_total',
            value=50.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='avg_check_total',
            value=100.0,
            updated_at=now,
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=1,
        include_all_staff_in_leaderboards=True,
    )

    boards = result['staff_leaderboards']
    assert boards['avg_check_plan_branch'] == []
    assert [row['staff_id'] for row in boards['avg_check_plan_staff']] == [1]


@pytest.mark.asyncio
async def test_admin_fact_keeps_personal_revenue_separate_from_responsibility_metrics(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, user_id=500))
    async_session.add(Client(id=1, name='C', company_id=1))
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=500,
        ),
        FinancialTransaction(
            id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
            amount=1000.0,
            record_id=1,
            visit_id=1,
            sold_item_id=10,
            sold_item_type='service',
            master_id=1,
            company_id=1,
        ),
        # A masterless direct payment with a dangling record id was excluded by
        # the pre-batch calculation and must not inflate every administrator.
        FinancialTransaction(
            id=2,
            date=datetime(2025, 1, 10, 13, 0, 0),
            amount=900.0,
            record_id=999,
            sold_item_type='goods_transaction',
            master_id=None,
            company_id=1,
        ),
        GoodTransaction(
            id=1,
            document_id=1,
            type_id=1,
            amount=-2.0,
            cost=300.0,
            master_id=2,
            company_id=1,
            date=datetime(2025, 1, 10, 12, 0, 0),
        ),
    ])

    now = datetime(2025, 1, 1)
    for code, value in {
        'revenue': 1.0,
        'clients': 1.0,
        'cosmo_qty': 1.0,
        'cosmo_sum': 1.0,
    }.items():
        async_session.add(
            PlanMetric(
                period_start=date(2025, 1, 1),
                period_end=date(2025, 1, 31),
                company_id=1,
                staff_id=2,
                staff_category='administrator',
                metric_code=code,
                value=value,
                updated_at=now,
            )
        )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    summary = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
        staff_id=2,
        include_appointments_breakdown=False,
    )
    admin_group = next(g for g in r.json()['data']['groups'] if g['category'] == 'administrator')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert admin_cells['revenue']['calculation_scope'] == 'personal'
    assert admin_cells['clients']['calculation_scope'] == 'administrator_records'
    assert admin_cells['revenue']['fact'] == summary['revenue']['total'] == 0.0
    assert admin_cells['avg_check_total']['fact'] == summary['average_check']['total'] == 0.0
    assert admin_cells['clients']['fact'] == 1.0
    assert admin_cells['cosmo_qty']['fact'] == 2.0
    assert admin_cells['cosmo_sum']['fact'] == 300.0


@pytest.mark.asyncio
async def test_plan_fact_admin_barber_clients_check_passes_when_creators_match(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, user_id=500))
    async_session.add(Client(id=1, name='C1', company_id=1))
    async_session.add(Client(id=2, name='C2', company_id=1))
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=500,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=500,
        ),
    ])
    now = datetime(2025, 1, 1)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='clients',
            value=2.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            metric_code='clients',
            value=2.0,
            updated_at=now,
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['diagnostics'] == []
    groups = data['groups']
    admin_group = next(g for g in groups if g['category'] == 'administrator')
    barber_group = next(g for g in groups if g['category'] == 'barber')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    barber_cells = {cell['code']: cell for cell in barber_group['metrics']}
    assert admin_cells['clients']['fact'] == barber_cells['clients']['fact'] == 2.0


@pytest.mark.asyncio
async def test_plan_fact_reports_admin_barber_clients_mismatch_diagnostics(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, user_id=500))
    async_session.add(Client(id=1, name='C1', company_id=1))
    async_session.add(Client(id=2, name='C2', company_id=1))
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=500,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2025, 1, 11),
            datetime=datetime(2025, 1, 11, 12, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=999,
        ),
    ])
    async_session.add_all([
        StaffSchedule(staff_id=2, company_id=1, date=date(2025, 1, 10),
                      slot_from=time(10, 0), slot_to=time(22, 0)),
        StaffSchedule(staff_id=2, company_id=1, date=date(2025, 1, 11),
                      slot_from=time(10, 0), slot_to=time(22, 0)),
    ])
    now = datetime(2025, 1, 1)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='clients',
            value=2.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            metric_code='clients',
            value=2.0,
            updated_at=now,
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['diagnostics'] == []

    groups = data['groups']
    admin_group = next(g for g in groups if g['category'] == 'administrator')
    admin_clients_cell = next(m for m in admin_group['metrics'] if m['code'] == 'clients')
    assert admin_clients_cell['fact'] == 2.0


@pytest.mark.asyncio
async def test_plan_fact_admin_clients_do_not_duplicate_overlapping_shifts(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add(Staff(id=2, name='Admin A', position='Администратор', company_id=1, user_id=500))
    async_session.add(Staff(id=3, name='Admin B', position='Администратор', company_id=1, user_id=501))
    async_session.add_all([
        Client(id=1, name='C1', company_id=1),
        Client(id=2, name='C2', company_id=1),
        Client(id=3, name='C3', company_id=1),
        Client(id=4, name='C4', company_id=1),
    ])
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=999,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 13, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=1,
            created_user_id=999,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            client_id=3,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 14, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=2,
            created_user_id=999,
        ),
        Appointment(
            id=4,
            company_id=1,
            staff_id=1,
            client_id=4,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 15, 0, 0),
            create_date=datetime(2025, 1, 5, 12, 0, 0),
            seance_length=3600,
            attendance=0,
            created_user_id=999,
        ),
        StaffSchedule(staff_id=2, company_id=1, date=date(2025, 1, 10),
                      slot_from=time(10, 0), slot_to=time(22, 0)),
        StaffSchedule(staff_id=3, company_id=1, date=date(2025, 1, 10),
                      slot_from=time(10, 0), slot_to=time(22, 0)),
    ])
    now = datetime(2025, 1, 1)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='clients',
            value=1.0,
            updated_at=now,
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['diagnostics'] == []

    groups = data['groups']
    barber_clients = sum(
        next(m for m in group['metrics'] if m['code'] == 'clients')['fact']
        for group in groups
        if group['category'] == 'barber'
    )
    admin_clients_by_staff = {
        group['staff_id']: next(m for m in group['metrics'] if m['code'] == 'clients')['fact']
        for group in groups
        if group['category'] == 'administrator'
    }
    assert barber_clients == 2.0
    assert sum(admin_clients_by_staff.values()) == barber_clients
    assert admin_clients_by_staff == {2: 1.0, 3: 1.0}


@pytest.mark.asyncio
async def test_plan_fact_excludes_fired_staff(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Active', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=2, name='Fired', position='Барбер', company_id=1, fired=1))
    async_session.add(Staff(id=3, name='лист ожидания', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=4, name='No Plan', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=5, name='Zero Plan', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=6, name='Not Working', position='Барбер', company_id=1, fired=0))
    now = datetime(2025, 1, 1)
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='revenue',
            value=1000.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=5,
            staff_category='barber',
            metric_code='revenue',
            value=0.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=6,
            staff_category='barber',
            metric_code='revenue',
            value=5000.0,
            updated_at=now,
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=6,
            staff_category='barber',
            metric_code='clients',
            value=0.0,
            updated_at=now,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    groups = r.json()['data']['groups']
    assert [group['title'] for group in groups] == ['Active']


@pytest.mark.asyncio
async def test_plan_fact_includes_fired_admin_with_period_schedule(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Barber', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Former admin', position='Администратор', company_id=1, fired=1),
        Service(id=10, title='Care', company_id=1),
        ServiceLabel(
            service_id=10,
            company_id=1,
            is_extra=True,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
        StaffSchedule(
            staff_id=2,
            company_id=1,
            date=date(2025, 1, 10),
            slot_from=time(10),
            slot_to=time(22),
        ),
        SyncSourceState(
            company_id=1,
            source=dashboard_service.STAFF_SCHEDULE_SOURCE,
            period_start=date(2024, 12, 31),
            period_end=date(2025, 1, 31),
            synced_at=datetime(2025, 1, 31),
        ),
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            metric_code='reviews_qty',
            value=3,
            source='dashboard',
            updated_at=datetime(2025, 1, 31),
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 7, 30),
            attendance=1,
        ),
        Transaction(
            id=1,
            appointment_id=1,
            company_id=1,
            service_id=10,
            service_title='Care',
            amount=1,
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
    )
    direct_result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        staff_id=2,
    )

    admin = next(group for group in result['groups'] if group['staff_id'] == 2)
    cells = {cell['code']: cell for cell in admin['metrics']}
    assert cells['extra_services_qty']['fact'] == 1.0
    assert cells['extra_services_pct']['fact'] == 100.0
    assert cells['reviews_qty']['fact'] == 3.0
    parent_cells = {cell['code']: cell for cell in result['parent_group']['metrics']}
    assert parent_cells['reviews_qty']['fact'] == 3.0
    assert direct_result['selected_staff']['id'] == 2
    direct_cells = {
        cell['code']: cell
        for cell in direct_result['selected_staff_plan']['metrics']
    }
    assert direct_cells['extra_services_qty']['fact'] == 1.0


@pytest.mark.asyncio
async def test_plan_fact_uses_standalone_input_category_for_former_admin(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(
            id=1,
            title='Salon',
            group_id=1,
            reporting_start_date=date(2025, 1, 1),
        ),
        Staff(id=2, name='Former admin', position='Барбер', company_id=1, fired=1),
        PlanStaffInput(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            extra_services_qty=8,
            extra_services_pct=25,
            updated_at=datetime(2025, 1, 1),
        ),
        ManualFactMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=2,
            metric_code='reviews_qty',
            value=3,
            source='dashboard',
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.commit()

    result = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 1, 31),
        company_id=1,
    )
    editor = await dashboard_service.fetch_manual_review_facts(
        async_session,
        '2025-01',
        company_id=1,
    )

    admin = next(group for group in result['groups'] if group['staff_id'] == 2)
    cells = {cell['code']: cell for cell in admin['metrics']}
    assert admin['category'] == 'administrator'
    assert cells['extra_services_qty']['plan'] == 8.0
    assert cells['extra_services_pct']['plan'] == 25.0
    assert cells['extra_services_qty']['fact'] is None
    parent_cells = {cell['code']: cell for cell in result['parent_group']['metrics']}
    assert parent_cells['reviews_qty']['fact'] == 3.0
    assert editor['total_value'] == 3.0
    assert editor['rows'][0]['staff_id'] == 2
    assert editor['rows'][0]['is_active'] is True
    assert any(
        item['code'] == 'staff_schedule_coverage'
        for item in result['diagnostics']
    )


@pytest.mark.asyncio
async def test_plan_fact_lists_staff_with_facts_when_staff_plans_are_missing(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Worked', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    async_session.add(Staff(id=3, name='Idle', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=4, name='Fired', position='Барбер', company_id=1, fired=1))
    async_session.add(
        Appointment(id=1, company_id=1, staff_id=1, date=date(2025, 1, 10), attendance=1)
    )
    now = datetime(2025, 1, 1)
    async_session.add(
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            metric_code='revenue',
            value=1000.0,
            updated_at=now,
        )
    )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
    app.dependency_overrides.clear()

    assert r.status_code == 200
    groups = r.json()['data']['groups']
    assert [group['title'] for group in groups] == ['Admin', 'Worked']
    barber_cells = {cell['code']: cell for cell in groups[1]['metrics']}
    assert barber_cells['revenue']['plan'] is None
    assert barber_cells['clients']['fact'] == 1.0


@pytest.mark.asyncio
async def test_opz_period_uses_future_appointment_create_date(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Barber', position='Барбер', company_id=1))
    async_session.add_all([
        Client(id=1, name='Dec Visit', company_id=1, visits_count=1, last_visit_date=date(2024, 12, 31)),
        Client(id=2, name='Jan Visit', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 31)),
        Client(id=3, name='Late Booking', company_id=1, visits_count=1, last_visit_date=date(2025, 1, 1)),
    ])
    await async_session.flush()

    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2024, 12, 31),
            datetime=datetime(2024, 12, 31, 12, 0, 0),
            create_date=datetime(2024, 12, 1, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 20),
            datetime=datetime(2025, 1, 20, 12, 0, 0),
            create_date=datetime(2025, 1, 1, 10, 0, 0),
            seance_length=3600,
            attendance=0,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2025, 1, 31),
            datetime=datetime(2025, 1, 31, 12, 0, 0),
            create_date=datetime(2025, 1, 1, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=4,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2025, 2, 20),
            datetime=datetime(2025, 2, 20, 12, 0, 0),
            create_date=datetime(2025, 2, 1, 10, 0, 0),
            seance_length=3600,
            attendance=0,
        ),
        Appointment(
            id=5,
            company_id=1,
            staff_id=1,
            client_id=3,
            date=date(2025, 1, 1),
            datetime=datetime(2025, 1, 1, 12, 0, 0),
            create_date=datetime(2024, 12, 1, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        ),
        Appointment(
            id=6,
            company_id=1,
            staff_id=1,
            client_id=3,
            date=date(2025, 1, 20),
            datetime=datetime(2025, 1, 20, 12, 0, 0),
            create_date=datetime(2025, 1, 3, 10, 0, 0),
            seance_length=3600,
            attendance=0,
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        jan = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
        feb = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-02-01', 'end_date': '2025-02-28'},
        )
        jan_bundle = await client.get(
            '/dashboard/bundle',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
        )
    app.dependency_overrides.clear()

    assert jan.status_code == 200
    assert feb.status_code == 200

    jan_branch = next(group for group in jan.json()['data']['groups'] if group['scope'] == 'branch')
    feb_branch = next(group for group in feb.json()['data']['groups'] if group['scope'] == 'branch')
    jan_cells = {cell['code']: cell for cell in jan_branch['metrics']}
    feb_cells = {cell['code']: cell for cell in feb_branch['metrics']}
    assert jan_cells['opz_qty']['fact'] == 1.0
    assert feb_cells['opz_qty']['fact'] == 1.0

    bundle = jan_bundle.json()['data']
    assert bundle['summary']['visit_metrics']['opz_qty'] == 1.0
    assert bundle['summary']['visit_metrics']['opz_pct'] == 50.0
    jan_first = next(row for row in bundle['revenue_daily'] if row['date'] == '2025-01-01')
    assert jan_first['appointments'] == 1
    assert jan_first['opz_qty'] == 1
    assert jan_first['opz_pct'] == 100.0


@pytest.mark.asyncio
async def test_dashboard_services_api_updates_extra_label_and_metrics(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client', company_id=1))
    async_session.add(
        ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Black Mask',
            price_min=500,
            duration=30,
            category_title='Care',
            updated_at=datetime(2025, 1, 1, 0, 0, 0),
        )
    )
    async_session.add(
        ServiceCatalog(
            company_id=1,
            service_id=20,
            title='Unused legacy service',
            category_title='Care',
            updated_at=datetime(2025, 1, 1, 0, 0, 0),
        )
    )
    async_session.add(
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            attendance=1,
        )
    )
    async_session.add(Transaction(id=1, appointment_id=1, company_id=1, service_id=10, service_title='Black Mask', amount=2))
    async_session.add(
        FinancialTransaction(
            id=1,
            company_id=1,
            record_id=1,
            date=datetime(2025, 1, 10, 12, 30, 0),
            amount=1000,
            sold_item_id=10,
            sold_item_type='service',
        )
    )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        listed = await client.get('/dashboard/services', params={'company_id': 1})
        assert listed.status_code == 200
        row = listed.json()['data']['rows'][0]
        assert [item['service_id'] for item in listed.json()['data']['rows']] == [10, 20]
        assert row['service_id'] == 10
        assert row['is_extra'] is False

        patched = await client.patch('/dashboard/services/1/10/labels', json={'is_extra': True})
        assert patched.status_code == 200

        summary = await client.get('/dashboard/bundle', params={
            'start_date': '2025-01-01',
            'end_date': '2025-01-31',
            'company_id': 1,
        })
        assert summary.status_code == 200
        revenue = summary.json()['data']['summary']['revenue']
        assert revenue['extra_service_count'] == 2.0
        assert revenue['extra_service_revenue'] == 1000.0

        unpatched = await client.patch('/dashboard/services/1/10/labels', json={'is_extra': False})
        assert unpatched.status_code == 200
        listed_again = await client.get('/dashboard/services', params={'company_id': 1})
        assert listed_again.json()['data']['rows'][0]['is_extra'] is False

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_dashboard_service_kpi_groups_and_single_assignment(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Master', position='Барбер', company_id=1))
    async_session.add(Client(id=1, name='Client', company_id=1))
    async_session.add(
        ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Black Mask',
            category_title='Care',
            updated_at=datetime(2025, 1, 1, 0, 0, 0),
        )
    )
    async_session.add(
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 12, 0, 0),
            attendance=1,
        )
    )
    async_session.add(Transaction(id=1, appointment_id=1, company_id=1, service_id=10, service_title='Black Mask', amount=1))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        first = await client.post('/dashboard/services/kpi_groups', json={'title': 'Уход лицо', 'code': 'face_care'})
        assert first.status_code == 200
        first_group = first.json()['data']

        second = await client.post('/dashboard/services/kpi_groups', json={'title': 'Уход голова', 'code': 'head_care'})
        assert second.status_code == 200
        second_group = second.json()['data']

        assigned = await client.patch('/dashboard/services/1/10/kpi_group', json={'group_id': first_group['id']})
        assert assigned.status_code == 200
        listed = await client.get('/dashboard/services', params={'company_id': 1, 'kpi_group_id': first_group['id']})
        assert [row['service_id'] for row in listed.json()['data']['rows']] == [10]

        reassigned = await client.patch('/dashboard/services/1/10/kpi_group', json={'group_id': second_group['id']})
        assert reassigned.status_code == 200
        assignments = (await async_session.execute(select(ServiceKpiAssignment))).scalars().all()
        assert [(row.company_id, row.service_id, row.group_id) for row in assignments] == [(1, 10, second_group['id'])]

        archived = await client.delete(f'/dashboard/services/kpi_groups/{first_group["id"]}')
        assert archived.status_code == 200
        assert archived.json()['data']['is_active'] is False

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_dashboard_service_batch_update_is_atomic_and_normalized(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        PortalAccount(id=1, label='Tenant', created_at=datetime(2025, 1, 1)),
        ServiceKpiGroup(
            id=1,
            portal_account_id=1,
            code='care',
            title='Care',
            is_active=True,
            sort_order=0,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        ),
        ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Black Mask',
            category_title='Care',
            updated_at=datetime(2025, 1, 1, 0, 0, 0),
        ),
        ServiceCatalog(
            company_id=1,
            service_id=20,
            title='Head Care',
            category_title='Care',
            updated_at=datetime(2025, 1, 1, 0, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        group_id = 1

        updated = await client.patch('/dashboard/services', json={
            'row_changes': [
                {'company_id': 1, 'service_id': 10, 'is_extra': True, 'kpi_group_id': group_id},
            ],
            'group_changes': [
                {'id': group_id, 'title': 'Face Care', 'description': None, 'sort_order': 5},
            ],
        })

        unassigned = await client.patch('/dashboard/services', json={
            'row_changes': [{'company_id': 1, 'service_id': 10, 'kpi_group_id': None}],
            'group_changes': [],
        })
        no_op = await client.patch('/dashboard/services', json={'row_changes': [], 'group_changes': []})

        rolled_back = await client.patch('/dashboard/services', json={
            'row_changes': [
                {'company_id': 1, 'service_id': 20, 'is_extra': True},
                {'company_id': 1, 'service_id': 999, 'is_extra': True},
            ],
            'group_changes': [],
        })

    app.dependency_overrides.clear()

    assert updated.status_code == 200, updated.text
    normalized_row = updated.json()['data']['rows'][0]
    assert {key: normalized_row[key] for key in (
        'company_id', 'service_id', 'is_extra', 'kpi_group_id'
    )} == {
        'company_id': 1,
        'service_id': 10,
        'is_extra': True,
        'kpi_group_id': group_id,
    }
    assert normalized_row['label_updated_at']
    assert normalized_row['kpi_assignment_updated_at']
    assert normalized_row['mutation_updated_at']
    normalized_group = updated.json()['data']['groups'][0]
    assert normalized_group['portal_account_id'] is not None
    assert {key: normalized_group[key] for key in (
        'id', 'code', 'title', 'description', 'is_active', 'sort_order'
    )} == {
        'id': group_id,
        'code': 'care',
        'title': 'Face Care',
        'description': '',
        'is_active': True,
        'sort_order': 5,
    }
    assert normalized_group['created_at']
    assert normalized_group['updated_at']
    assert unassigned.status_code == 200
    assert unassigned.json()['data']['rows'][0]['kpi_group_id'] is None
    assert unassigned.json()['data']['rows'][0]['kpi_assignment_updated_at'] is None
    assert unassigned.json()['data']['rows'][0]['mutation_updated_at']
    assert no_op.status_code == 200
    assert no_op.json()['data'] == {'rows': [], 'groups': []}
    assert rolled_back.status_code == 400
    assert rolled_back.json()['detail'] == 'unknown service for company'

    labels = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assignments = (await async_session.execute(select(ServiceKpiAssignment))).scalars().all()
    group = await async_session.get(ServiceKpiGroup, group_id)
    assert [(row.company_id, row.service_id, row.is_extra) for row in labels] == [(1, 10, True)]
    assert assignments == []
    assert group.title == 'Face Care'


@pytest.mark.asyncio
async def test_dashboard_service_batch_rolls_back_after_partial_apply(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        PortalAccount(id=1, label='Tenant', created_at=datetime(2025, 1, 1)),
        ServiceKpiGroup(
            id=1,
            portal_account_id=1,
            code='care',
            title='Care',
            is_active=True,
            sort_order=0,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        ),
        ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Black Mask',
            updated_at=datetime(2025, 1, 1),
        ),
        ServiceCatalog(
            company_id=1,
            service_id=20,
            title='Head Care',
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.commit()

    original_apply_label = dashboard_service._apply_service_label
    applied_labels = 0

    async def fail_after_second_label(db, catalog, *, is_extra, now):
        nonlocal applied_labels
        await original_apply_label(db, catalog, is_extra=is_extra, now=now)
        applied_labels += 1
        if applied_labels == 2:
            raise RuntimeError('forced failure after partial apply')

    monkeypatch.setattr(dashboard_service, '_apply_service_label', fail_after_second_label)

    with pytest.raises(RuntimeError, match='forced failure after partial apply'):
        await dashboard_service.save_service_management(
            async_session,
            row_changes=[
                {'company_id': 1, 'service_id': 10, 'is_extra': True},
                {'company_id': 1, 'service_id': 20, 'is_extra': True},
            ],
            group_changes=[{'id': 1, 'title': 'Changed'}],
            allowed_company_ids=[1],
            portal_account_id=1,
        )

    labels = (await async_session.execute(select(ServiceLabel))).scalars().all()
    legacy_services = (await async_session.execute(select(Service))).scalars().all()
    group = await async_session.get(ServiceKpiGroup, 1)
    assert applied_labels == 2
    assert labels == []
    assert legacy_services == []
    assert group.title == 'Care'


@pytest.mark.asyncio
async def test_dashboard_service_batch_validates_changes_and_branch_scope(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([
        Company(id=1, title='Allowed', group_id=1),
        Company(id=2, title='Foreign', group_id=1),
        PortalAccount(id=1, label='Tenant', created_at=datetime(2025, 1, 1)),
        PortalAccount(id=2, label='Other tenant', created_at=datetime(2025, 1, 1)),
        PortalBranch(portal_account_id=1, company_id=1),
        ServiceKpiGroup(
            id=1,
            portal_account_id=1,
            code='care',
            title='Care',
            is_active=True,
            sort_order=0,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        ),
        ServiceKpiGroup(
            id=99,
            portal_account_id=2,
            code='foreign',
            title='Foreign',
            is_active=True,
            sort_order=0,
            created_at=datetime(2025, 1, 1),
            updated_at=datetime(2025, 1, 1),
        ),
        ServiceCatalog(company_id=1, service_id=10, title='Allowed', updated_at=datetime(2025, 1, 1)),
        ServiceCatalog(company_id=2, service_id=20, title='Foreign', updated_at=datetime(2025, 1, 1)),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    async def override_access():
        return AccessContext.from_user(
            user_id=10,
            role='branch_admin',
            portal_account_id=1,
            company_ids=[1],
        )

    app.dependency_overrides[api.get_async_db] = override_db
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = override_access
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        foreign = await client.patch('/dashboard/services', json={
            'row_changes': [{'company_id': 2, 'service_id': 20, 'is_extra': True}],
            'group_changes': [],
        })
        foreign_group = await client.patch('/dashboard/services', json={
            'row_changes': [{'company_id': 1, 'service_id': 10, 'kpi_group_id': 99}],
            'group_changes': [],
        })
        foreign_group_change = await client.patch('/dashboard/services', json={
            'row_changes': [],
            'group_changes': [{'id': 99, 'title': 'Changed'}],
        })
        empty_row = await client.patch('/dashboard/services', json={
            'row_changes': [{'company_id': 1, 'service_id': 10}],
            'group_changes': [],
        })
        duplicate_rows = await client.patch('/dashboard/services', json={
            'row_changes': [
                {'company_id': 1, 'service_id': 10, 'is_extra': True},
                {'company_id': 1, 'service_id': 10, 'kpi_group_id': None},
            ],
            'group_changes': [],
        })
        null_label = await client.patch('/dashboard/services', json={
            'row_changes': [{'company_id': 1, 'service_id': 10, 'is_extra': None}],
            'group_changes': [],
        })
        empty_group = await client.patch('/dashboard/services', json={
            'row_changes': [],
            'group_changes': [{'id': 1}],
        })
        null_group_state = await client.patch('/dashboard/services', json={
            'row_changes': [],
            'group_changes': [{'id': 1, 'is_active': None}],
        })
        duplicate_groups = await client.patch('/dashboard/services', json={
            'row_changes': [],
            'group_changes': [
                {'id': 1, 'title': 'One'},
                {'id': 1, 'title': 'Two'},
            ],
        })
        inactive_assignment = await client.patch('/dashboard/services', json={
            'row_changes': [{'company_id': 1, 'service_id': 10, 'kpi_group_id': 1}],
            'group_changes': [{'id': 1, 'is_active': False}],
        })

    app.dependency_overrides.clear()

    assert foreign.status_code == 400
    assert foreign.json()['detail'] == 'company is not allowed'
    assert foreign_group.status_code == 400
    assert foreign_group.json()['detail'] == 'unknown KPI group'
    assert foreign_group_change.status_code == 400
    assert foreign_group_change.json()['detail'] == 'unknown KPI group'
    assert empty_row.status_code == 422
    assert duplicate_rows.status_code == 422
    assert null_label.status_code == 422
    assert empty_group.status_code == 422
    assert null_group_state.status_code == 422
    assert duplicate_groups.status_code == 422
    assert inactive_assignment.status_code == 400
    assert inactive_assignment.json()['detail'] == 'unknown active KPI group'


@pytest.mark.asyncio
async def test_service_report_cards_use_complete_overview_totals_beyond_top_25(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 10),
            attendance=1,
        ),
    ])
    expected_revenue = 0.0
    for index in range(30):
        service_id = 100 + index
        amount = float(index + 1)
        expected_revenue += amount
        async_session.add(Transaction(
            id=index + 1,
            appointment_id=1,
            service_id=service_id,
            service_title=f'Service {index:02}',
            amount=1,
            company_id=1,
        ))
        async_session.add(FinancialTransaction(
            id=index + 1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_id=service_id,
            sold_item_type='service',
            date=datetime(2026, 8, 1, 10),
            amount=amount,
        ))
    async_session.add(Transaction(
        id=31,
        appointment_id=1,
        service_id=999,
        service_title='Included package service',
        amount=1,
        company_id=1,
    ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'service_combos',
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
    )
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    complete_detail = await dashboard_service.fetch_top_services(
        async_session,
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
        limit=None,
        factual_at=factual_at,
    )
    cards = {card['label']: card['value'] for card in report['cards']}
    services_table = next(table for table in report['tables'] if table['id'] == 'services')

    assert cards['Выручка услуг'] == expected_revenue
    assert cards['Выручка услуг'] == overview['revenue']['service_revenue']
    assert cards['Услуг оказано'] == overview['revenue']['service_count'] == 31.0
    assert cards['Уникальных услуг'] == 31
    assert len(services_table['rows']) == 25
    free_service = next(
        row for row in complete_detail if row['title'] == 'Included package service'
    )
    assert free_service['sold'] == 1
    assert free_service['revenue'] == 0.0


@pytest.mark.asyncio
async def test_goods_report_uses_shared_factual_cutoff_for_units_and_revenue(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        GoodTransaction(
            id=1,
            company_id=1,
            master_id=1,
            good_id=10,
            good_title='Wax',
            type_id=1,
            date=datetime(2026, 8, 1, 10),
            amount=-1.0,
        ),
        GoodTransaction(
            id=2,
            company_id=1,
            master_id=1,
            good_id=20,
            good_title='Future care',
            type_id=1,
            date=datetime(2026, 8, 1, 18),
            amount=-4.0,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            sold_item_id=10,
            sold_item_type='goods_transaction',
            date=datetime(2026, 8, 1, 10),
            amount=100.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=1,
            sold_item_id=20,
            sold_item_type='goods_transaction',
            date=datetime(2026, 8, 1, 18),
            amount=900.0,
        ),
    ])
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'goods_dynamics',
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
    )
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    cards = {card['label']: card['value'] for card in report['cards']}

    assert cards['Выручка товаров'] == 100.0
    assert cards['Выручка товаров'] == overview['revenue']['goods_revenue']
    assert cards['Единиц продано'] == 1.0
    assert report['raw']['by_period'] == [{
        'period': '2026-08-01',
        'sales_count': 1,
        'units': 1.0,
        'revenue': 100.0,
    }]


@pytest.mark.asyncio
async def test_ready_report_slices_share_one_factual_cutoff(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Current client', company_id=1),
        Client(id=2, name='At-risk client', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 5, 1),
            datetime=datetime(2026, 5, 1, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 10),
            attendance=1,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 18),
            attendance=1,
        ),
        Appointment(
            id=4,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2026, 5, 1),
            datetime=datetime(2026, 5, 1, 11),
            attendance=1,
        ),
        Appointment(
            id=5,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2026, 8, 1),
            datetime=datetime(2026, 8, 1, 19),
            attendance=1,
        ),
    ])
    for appointment_id, amount, moment in (
        (1, 50.0, datetime(2026, 5, 1, 10)),
        (2, 100.0, datetime(2026, 8, 1, 10)),
        (3, 900.0, datetime(2026, 8, 1, 11)),
        (4, 60.0, datetime(2026, 5, 1, 11)),
        (5, 800.0, datetime(2026, 8, 1, 11, 30)),
    ):
        async_session.add(FinancialTransaction(
            id=appointment_id,
            company_id=1,
            master_id=1,
            record_id=appointment_id,
            sold_item_id=10,
            sold_item_type='service',
            date=moment,
            amount=amount,
        ))
    await async_session.commit()

    staff_report = await dashboard_reports.fetch_report_data(
        async_session,
        'staff_efficiency',
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
    )
    client_report = await dashboard_reports.fetch_report_data(
        async_session,
        'top_clients_pareto',
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
    )
    recency_report = await dashboard_reports.fetch_report_data(
        async_session,
        'new_vs_returning_cross',
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
    )
    churn_report = await dashboard_reports.fetch_report_data(
        async_session,
        'revenue_at_risk',
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
    )
    booking_report = await dashboard_reports.fetch_report_data(
        async_session,
        'peak_load',
        date(2026, 8, 1),
        date(2026, 8, 1),
        company_id=1,
    )

    staff_cards = {card['label']: card['value'] for card in staff_report['cards']}
    client_cards = {card['label']: card['value'] for card in client_report['cards']}
    churn_cards = {card['label']: card['value'] for card in churn_report['cards']}
    booking_cards = {card['label']: card['value'] for card in booking_report['cards']}
    assert staff_cards['Завершено записей'] == 1
    assert staff_cards['Выручка услуг'] == 100.0
    assert client_cards['Визитов'] == 1
    assert client_cards['Выручка клиентов'] == 100.0
    assert recency_report['raw']['summary_metrics']['unique_clients'] == 1
    assert churn_cards['Спящие'] == 1
    assert churn_cards['Выручка под риском'] == 60.0
    assert booking_cards['Всего записей'] == 1
    assert booking_cards['Завершено'] == 1


@pytest.mark.asyncio
async def test_client_report_exposes_anonymous_residual_and_reconciles_totals(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Known client', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 7, 1),
            datetime=datetime(2026, 7, 1, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=None,
            date=date(2026, 7, 1),
            datetime=datetime(2026, 7, 1, 11),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2026, 7, 1, 10),
            amount=100.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=1,
            record_id=2,
            sold_item_id=10,
            sold_item_type='service',
            date=datetime(2026, 7, 1, 11),
            amount=200.0,
        ),
    ])
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'top_clients_pareto',
        date(2026, 7, 1),
        date(2026, 7, 1),
        company_id=1,
    )
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2026, 7, 1),
        date(2026, 7, 1),
        company_id=1,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    cards = {card['label']: card['value'] for card in report['cards']}

    assert cards['Клиентов'] == overview['visit_metrics']['unique_clients'] == 1
    assert cards['Визитов'] == overview['revenue']['appointments'] == 2
    assert cards['Выручка клиентов'] == overview['revenue']['service_revenue'] == 300.0
    assert report['raw']['anonymous_residual'] == {
        'visits': 1,
        'revenue': 200.0,
    }
    assert sum(row['revenue'] for row in report['raw']['segments']) == 300.0
    assert sum(
        row['revenue_pct'] for row in report['raw']['pareto']
    ) == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_future_ended_client_reports_measure_recency_at_factual_cutoff(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Client(id=1, name='Recent client', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 7, 1),
            datetime=datetime(2026, 7, 1, 10),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_type='service',
            date=datetime(2026, 7, 1, 10),
            amount=100.0,
        ),
    ])
    await async_session.commit()

    rows = await dashboard_reports._clients_rows(
        async_session,
        date(2000, 1, 1),
        date(2026, 12, 31),
        1,
        None,
        None,
        factual_at,
    )
    churn = await dashboard_reports.fetch_report_data(
        async_session,
        'revenue_at_risk',
        date(2026, 1, 1),
        date(2026, 12, 31),
        company_id=1,
    )
    cards = {card['label']: card['value'] for card in churn['cards']}

    assert rows[0]['days_since_last_visit'] == 31
    assert cards['Под риском'] == 0
    assert cards['Спящие'] == 0
    assert cards['Потерянные'] == 0
    assert cards['Выручка под риском'] == 0.0


@pytest.mark.asyncio
async def test_yoy_changes_remain_available_for_metrics_with_complete_sources(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(id=1, company_id=1, staff_id=1, date=date(2024, 1, 1), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, date=date(2024, 12, 31), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, date=date(2025, 1, 1), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, date=date(2025, 12, 31), attendance=1),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_type='service',
            date=datetime(2024, 1, 1, 12),
            amount=100.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=1,
            record_id=3,
            sold_item_type='service',
            date=datetime(2025, 1, 1, 12),
            amount=200.0,
        ),
        SyncSourceState(
            company_id=1,
            source='appointments_detail',
            period_start=date(2024, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=datetime(2026, 1, 1),
        ),
        SyncSourceState(
            company_id=1,
            source='financial_transactions_detail',
            period_start=date(2024, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=datetime(2026, 1, 1),
        ),
    ])
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        company_id=1,
    )
    years = {row['year']: row for row in report['raw']['years']}

    assert years[2024]['source_status'] == 'partial'
    assert years[2025]['source_status'] == 'partial'
    assert years[2025]['comparison_status'] == 'incomplete_source'
    assert years[2025]['goods_count'] is None
    assert years[2025]['revenue_change_pct'] == 100.0
    assert years[2025]['appointments_change_pct'] == 0.0
    assert years[2025]['avg_check_change_pct'] == 100.0


@pytest.mark.asyncio
async def test_linked_masterless_topup_reconciles_staff_overview_plan_fact_and_yoy(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            external_id=77,
            company_id=1,
            staff_id=1,
            date=date(2024, 1, 10),
            datetime=datetime(2024, 1, 10, 10),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=None,
            record_id=77,
            sold_item_type='personal_account',
            expense_title='Пополнение личного счета',
            date=datetime(2025, 7, 1, 12),
            amount=250.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2024, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=factual_at,
        ))
    await async_session.commit()

    branch_overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    staff_overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        staff_id=1,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    plan_fact = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_all_staff_in_leaderboards=True,
    )
    yoy = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2025, 1, 1),
        date(2025, 12, 31),
        staff_id=1,
        company_id=1,
    )

    parent_cells = {
        cell['code']: cell for cell in plan_fact['parent_group']['metrics']
    }
    staff_group = next(group for group in plan_fact['groups'] if group['staff_id'] == 1)
    staff_cells = {cell['code']: cell for cell in staff_group['metrics']}
    yoy_2025 = next(row for row in yoy['raw']['years'] if row['year'] == 2025)

    assert branch_overview['revenue']['topup_revenue'] == 250.0
    assert staff_overview['revenue']['topup_revenue'] == 250.0
    assert parent_cells['revenue']['fact'] == 250.0
    assert staff_cells['revenue']['fact'] == 250.0
    assert yoy['raw']['activity_end'] == '2025-07-01'
    assert yoy['raw']['latest_year'] == 2025
    assert yoy_2025['topup_revenue'] == 250.0
    assert yoy_2025['revenue'] == 250.0
    # A tenant that does use personal accounts keeps the column: it is a
    # component of total revenue, so hiding it would leave Выручка unexplained.
    years_table = next(table for table in yoy['tables'] if table['id'] == 'years')
    assert 'topup_revenue' in {column['key'] for column in years_table['columns']}


@pytest.mark.asyncio
async def test_yoy_keeps_uncovered_current_year_unknown_after_last_fact(
    async_session,
    monkeypatch,
):
    monkeypatch.setattr(
        dashboard_reports,
        '_report_now',
        lambda: datetime(2026, 8, 1, 12),
    )
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2025, 6, 1),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_type='service',
            date=datetime(2025, 6, 1, 12),
            amount=100.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            synced_at=datetime(2026, 1, 1),
        ))
    await async_session.commit()

    report = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2026, 1, 1),
        date(2026, 1, 31),
        company_id=1,
    )
    years = {row['year']: row for row in report['raw']['years']}

    assert report['source_status'] == 'partial'
    assert report['raw']['latest_year'] == 2025
    assert set(years) == {2025, 2026}
    assert years[2025]['revenue'] == 100.0
    assert years[2026]['is_partial_year'] is True
    assert years[2026]['source_status'] == 'partial'
    assert years[2026]['revenue'] is None
    assert years[2026]['appointments'] is None
    assert all(
        row['revenue'] is None
        for row in report['raw']['months']
        if row['year'] == 2026
    )


@pytest.mark.asyncio
async def test_yoy_admin_scope_matches_personal_overview_and_plan_fact(
    async_session,
    monkeypatch,
):
    factual_at = datetime(2026, 8, 1, 12)
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: factual_at)
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(
            id=2,
            name='Admin',
            position='Администратор',
            company_id=1,
            user_id=500,
        ),
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            created_user_id=500,
            date=date(2025, 1, 10),
            datetime=datetime(2025, 1, 10, 10),
            attendance=1,
        ),
        FinancialTransaction(
            id=1,
            company_id=1,
            master_id=1,
            record_id=1,
            sold_item_type='service',
            date=datetime(2025, 1, 10, 10),
            amount=500.0,
        ),
        FinancialTransaction(
            id=2,
            company_id=1,
            master_id=None,
            record_id=None,
            sold_item_type='personal_account',
            expense_title='Пополнение личного счета',
            date=datetime(2025, 2, 10, 10),
            amount=250.0,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=2,
            date=date(2025, 3, 10),
            datetime=datetime(2025, 3, 10, 10),
            attendance=1,
        ),
        GoodTransaction(
            id=1,
            external_id=700,
            company_id=1,
            master_id=2,
            type_id=1,
            document_id=1,
            date=datetime(2025, 3, 10, 10),
            amount=-1,
            cost=300.0,
        ),
        FinancialTransaction(
            id=3,
            company_id=1,
            master_id=None,
            sold_item_id=700,
            sold_item_type='goods_transaction',
            date=datetime(2025, 3, 10, 10),
            amount=300.0,
        ),
    ])
    for source in (
        'appointments_detail',
        'financial_transactions_detail',
        'goods_transactions_detail',
    ):
        async_session.add(SyncSourceState(
            company_id=1,
            source=source,
            period_start=date(2025, 1, 1),
            period_end=date(2026, 12, 31),
            synced_at=factual_at,
        ))
    await async_session.commit()

    plan_fact = await fetch_plan_fact(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        include_all_staff_in_leaderboards=True,
        factual_at=factual_at,
    )
    yoy = await dashboard_reports.fetch_report_data(
        async_session,
        'year_over_year',
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        staff_id=2,
    )

    admin_group = next(group for group in plan_fact['groups'] if group['staff_id'] == 2)
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    yoy_2025 = next(row for row in yoy['raw']['years'] if row['year'] == 2025)
    overview = await dashboard_service.fetch_summary(
        async_session,
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        staff_id=2,
        include_appointments_breakdown=False,
        factual_at=factual_at,
    )
    leaderboard = await dashboard_reports.fetch_report_data(
        async_session,
        'staff_leaderboard',
        date(2025, 1, 1),
        date(2025, 12, 31),
        company_id=1,
        staff_id=2,
    )
    leaderboard_cards = {
        card['label']: card['value']
        for card in leaderboard['cards']
    }

    assert admin_cells['revenue']['fact'] == overview['revenue']['total'] == 300.0
    assert admin_cells['clients']['fact'] == 1.0
    assert yoy['calculation_scope']['mode'] == 'personal'
    assert yoy['raw']['latest_year'] == 2025
    assert yoy_2025['revenue'] == overview['revenue']['total'] == 300.0
    assert yoy_2025['service_revenue'] == 0.0
    assert yoy_2025['goods_revenue'] == 300.0
    assert yoy_2025['topup_revenue'] == 0.0
    assert yoy_2025['appointments'] == 1
    assert yoy_2025['avg_check'] == overview['average_check']['total'] == 300.0
    assert leaderboard['calculation_scope']['mode'] == 'plan_fact'
    assert leaderboard_cards['Личная выручка выбранного сотрудника'] == 300.0


@pytest.mark.asyncio
async def test_cosmetics_metrics_drop_certificates_but_goods_revenue_keeps_them(async_session):
    async_session.add_all([
        Group(id=1, title='G1'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Master', position='Барбер', company_id=1),
        GoodCategoryCatalog(
            company_id=1, category_id=1, title='Основные товары',
            updated_at=datetime(2025, 1, 1),
        ),
        GoodCategoryCatalog(
            company_id=1, category_id=2, title='Сертификаты Сеть',
            updated_at=datetime(2025, 1, 1),
        ),
        GoodCatalog(
            company_id=1, good_id=10, title='Помада', category_id=1,
            updated_at=datetime(2025, 1, 1),
        ),
        # Sold as a plain title; only the catalog category marks it as a certificate.
        GoodCatalog(
            company_id=1, good_id=20, title='на сумму 5000', category_id=2,
            updated_at=datetime(2025, 1, 1),
        ),
        GoodTransaction(
            id=1, company_id=1, master_id=1, type_id=1, document_id=1,
            good_id=10, good_title='Помада', amount=-2.0, cost=600.0,
            date=datetime(2025, 1, 10, 12),
        ),
        GoodTransaction(
            id=2, company_id=1, master_id=1, type_id=1, document_id=2,
            good_id=20, good_title='на сумму 5000', amount=-1.0, cost=5000.0,
            date=datetime(2025, 1, 11, 12),
        ),
        # Not in the catalog at all — recognised by its title only.
        GoodTransaction(
            id=3, company_id=1, master_id=1, type_id=1, document_id=3,
            good_id=30, good_title='Подарочный сертификат на комплекс',
            amount=-1.0, cost=1400.0, date=datetime(2025, 1, 12, 12),
        ),
    ])
    await async_session.commit()

    cosmo = await dashboard_service._goods_sales_metrics(
        async_session, date(2025, 1, 1), date(2025, 1, 31), 1,
        factual_at=datetime(2025, 2, 1),
    )
    goods_count = await dashboard_service._goods_sold_count(
        async_session, dashboard_service.DateRange(date(2025, 1, 1), date(2025, 1, 31)), 1,
        factual_at=datetime(2025, 2, 1),
    )

    assert cosmo == {'cosmo_qty': 2.0, 'cosmo_sum': 600.0}
    assert goods_count == 4.0
