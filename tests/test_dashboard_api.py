"""Dashboard JSON API (product portal metrics)."""

import csv
from datetime import date, datetime, time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import api
import plan_import
from dashboard_service import fetch_plan_fact
from plan_import import (
    import_plan_sheet_csv,
    import_services_sheet_csv,
    _google_sheet_values_to_csv_text,
    _normalize_google_sheet_csv_url,
    _spreadsheet_id_from_url,
)
from api import app
from models import (
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Group,
    ManualFactMetric,
    PlanBranchSetting,
    PlanMetric,
    PlanStaffInput,
    Service,
    ServiceCatalog,
    ServiceLabel,
    Staff,
    StaffSchedule,
    Transaction,
)


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
    assert len(data) == 71
    by_id = {item['id']: item for item in data}
    assert by_id['revenue_dynamics']['status'] == 'ready'
    assert by_id['conversion_funnel']['status'] == 'source_missing'
    assert by_id['nps_dashboard']['status'] == 'partial'
    assert by_id['revenue_dynamics']['filters']['compare'] is True


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
    assert data['report_id'] == 'revenue_dynamics'
    assert data['cards'][0]['value'] == 1200.0
    assert data['comparison']['cards'][0]['value'] == 800.0
    assert data['charts']
    assert data['tables']


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
async def test_dashboard_bundle_requires_api_key(async_session, monkeypatch):
    monkeypatch.setattr(api, 'API_KEY', 'k')

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
    monkeypatch.setattr(api, 'API_KEY', '')


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
    assert data['appointments_breakdown']['source_status'] == 'unavailable'
    assert data['appointments_breakdown']['attended'] is None


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
async def test_dashboard_staff_directory_csv_exports_working_staff(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add(Staff(id=10, name='Active', position='Барбер', company_id=1, fired=0, bookable=True))
    async_session.add(Staff(id=20, name='Fired', position='Барбер', company_id=1, fired=1, bookable=False))
    async_session.add(Staff(id=30, name='Admin', position='Администратор', company_id=2, fired=0, user_id=500))
    async_session.add(Staff(id=40, name='Администратор Ривьера', position='Администратор', company_id=2, fired=0))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        active_only = await client.get('/dashboard/staff_directory.csv')
        all_staff = await client.get('/dashboard/staff_directory.csv', params={'include_fired': 1})
    app.dependency_overrides.clear()

    assert active_only.status_code == 200
    assert active_only.headers['content-type'].startswith('text/csv')
    assert 'company_id,company_title,staff_id,staff_name,position,user_id,fired,working,bookable' in active_only.text
    assert '1,Salon 1,10,Active' in active_only.text
    assert '20,Fired' not in active_only.text
    assert '2,Salon 2,30,Admin' in active_only.text
    assert 'Администратор Ривьера' not in active_only.text

    assert all_staff.status_code == 200
    assert '1,Salon 1,20,Fired' in all_staff.text
    assert 'Администратор Ривьера' not in all_staff.text


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
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Cut 1', cost=1000.0, first_cost=1000.0, amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=20, service_title='Cut 2', cost=2000.0, first_cost=2000.0, amount=1, company_id=1),
        FinancialTransaction(id=1, date=datetime(2025, 1, 10, 12, 0, 0), amount=1000.0, record_id=1, visit_id=1, sold_item_id=10, sold_item_type='service', master_id=1, company_id=1),
        FinancialTransaction(id=2, date=datetime(2025, 1, 10, 14, 0, 0), amount=2000.0, record_id=2, visit_id=2, sold_item_id=20, sold_item_type='service', master_id=2, company_id=1),
        FinancialTransaction(id=3, date=datetime(2025, 1, 10, 13, 0, 0), amount=300.0, record_id=1, visit_id=1, sold_item_id=1, sold_item_type='goods_transaction', master_id=1, company_id=1),
        FinancialTransaction(id=4, date=datetime(2025, 1, 10, 15, 0, 0), amount=700.0, record_id=2, visit_id=2, sold_item_id=2, sold_item_type='goods_transaction', master_id=2, company_id=1),
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
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    assert data['summary']['revenue']['total'] == 1300.0
    assert data['summary']['revenue']['appointments'] == 1
    assert data['revenue_daily'] == [
        {
            'date': '2025-01-10',
            'revenue': 1300.0,
            'service_revenue': 1000.0,
            'goods_revenue': 300.0,
            'appointments': 1,
        }
    ]
    assert [row['title'] for row in data['top_services']] == ['Cut 1']


@pytest.mark.asyncio
async def test_dashboard_plan_fact_uses_plan_and_fact_formulas(async_session):
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

    assert r_partial.status_code == 200
    partial_data = r_partial.json()['data']
    assert partial_data['period'] == {'start': '2025-01-15', 'end': '2025-01-20'}
    assert partial_data['plan_period'] == {'start': '2025-01-01', 'end': '2025-01-31'}
    partial_branch_group = next(group for group in partial_data['groups'] if group['scope'] == 'branch')
    partial_cells = {cell['code']: cell for cell in partial_branch_group['metrics']}
    assert partial_cells['revenue']['plan'] == 7000.0
    assert partial_cells['revenue']['fact'] == 0.0


@pytest.mark.asyncio
async def test_plan_fact_returns_staff_rankings_and_goods_kpis_by_scope(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add_all([
        Staff(id=1, name='Alpha', position='Барбер', company_id=1, fired=0),
        Staff(id=2, name='Bravo', position='Барбер', company_id=1, fired=0),
        Staff(id=3, name='Charlie', position='Барбер', company_id=2, fired=0),
    ])
    async_session.add_all([
        Client(id=1, name='Client 1', company_id=1),
        Client(id=2, name='Client 2', company_id=1),
        Client(id=3, name='Client 3', company_id=1),
        Client(id=4, name='Client 4', company_id=2),
        Client(id=5, name='Client 5', company_id=2),
    ])
    await async_session.flush()

    appointments = [
        (1, 1, 1, 1, date(2025, 1, 10)),
        (2, 1, 1, 2, date(2025, 1, 11)),
        (3, 1, 2, 3, date(2025, 1, 12)),
        (4, 2, 3, 4, date(2025, 1, 13)),
        (5, 2, 3, 5, date(2025, 1, 14)),
    ]
    async_session.add_all([
        Appointment(
            id=appointment_id,
            company_id=company_id,
            staff_id=staff_id,
            client_id=client_id,
            date=appointment_date,
            datetime=datetime(2025, 1, appointment_date.day, 12, 0, 0),
            create_date=datetime(2025, 1, appointment_date.day - 1, 12, 0, 0),
            seance_length=3600,
            attendance=1,
        )
        for appointment_id, company_id, staff_id, client_id, appointment_date in appointments
    ])
    await async_session.flush()

    transaction_rows = [
        (1, 1, 1, 10, 'воск', 1000.0, 1),
        (2, 2, 1, 11, 'камуфляж', 2000.0, 2),
        (3, 3, 2, 12, 'Black Mask', 5000.0, 3),
        (4, 4, 3, 13, 'уход за головой', 1500.0, 4),
        (5, 5, 3, 14, 'Стрижка', 2500.0, 5),
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
    app.dependency_overrides.clear()

    assert network_response.status_code == 200
    network_data = network_response.json()['data']
    assert [row['title'] for row in network_data['staff_rankings']['revenue_top']] == [
        'Bravo',
        'Charlie',
        'Alpha',
    ]
    assert [row['title'] for row in network_data['staff_rankings']['avg_check_top']] == [
        'Bravo',
        'Charlie',
        'Alpha',
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
    assert [row['title'] for row in branch_data['staff_rankings']['revenue_top']] == ['Bravo', 'Alpha']
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
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'date': '2025-01-01', 'value': 7}],
            },
        )
        editor_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        plan_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 1},
        )
        network_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
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
                ManualFactMetric.period_end == date(2025, 1, 1),
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
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'staff_id': 2,
            },
        )
        save_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
                'company_id': 1,
                'staff_id': 3,
                'items': [{'company_id': 1, 'staff_id': 3, 'date': '2025-01-01', 'value': 9}],
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
async def test_manual_review_facts_are_daily_and_sum_selected_period(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    async_session.add(ManualFactMetric(
        period_start=date(2025, 6, 1),
        period_end=date(2025, 6, 4),
        company_id=1,
        staff_id=2,
        metric_code='reviews_qty',
        value=99.0,
        source='legacy',
        updated_at=datetime(2025, 6, 1, 0, 0, 0),
    ))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        save_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'start_date': '2025-06-01',
                'end_date': '2025-06-04',
                'company_id': 1,
                'items': [
                    {'company_id': 1, 'staff_id': 2, 'date': '2025-06-01', 'value': 1},
                    {'company_id': 1, 'staff_id': 2, 'date': '2025-06-02', 'value': 2},
                    {'company_id': 1, 'staff_id': 2, 'date': '2025-06-03', 'value': 3},
                    {'company_id': 1, 'staff_id': 2, 'date': '2025-06-04', 'value': 4},
                ],
            },
        )
        full_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-06-04', 'company_id': 1},
        )
        partial_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'start_date': '2025-06-03', 'end_date': '2025-06-04', 'company_id': 1},
        )
        extended_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-06-05', 'company_id': 1},
        )
        plan_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-03', 'end_date': '2025-06-04', 'company_id': 1},
        )
        network_response = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-06-05'},
        )
    app.dependency_overrides.clear()

    assert save_response.status_code == 200

    full_data = full_response.json()['data']
    assert full_data['days'] == ['2025-06-01', '2025-06-02', '2025-06-03', '2025-06-04']
    assert full_data['total_value'] == 10.0
    full_row = full_data['rows'][0]
    assert full_row['value'] == 10.0
    assert [(item['date'], item['value']) for item in full_row['values']] == [
        ('2025-06-01', 1.0),
        ('2025-06-02', 2.0),
        ('2025-06-03', 3.0),
        ('2025-06-04', 4.0),
    ]

    partial_row = partial_response.json()['data']['rows'][0]
    assert partial_row['value'] == 7.0
    assert [item['value'] for item in partial_row['values']] == [3.0, 4.0]

    extended_row = extended_response.json()['data']['rows'][0]
    assert extended_row['value'] == 10.0
    assert extended_row['values'][-1] == {'date': '2025-06-05', 'value': None, 'updated_at': None}

    plan_data = plan_response.json()['data']
    parent_cells = {cell['code']: cell for cell in plan_data['parent_group']['metrics']}
    admin_group = next(group for group in plan_data['groups'] if group['category'] == 'administrator')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert parent_cells['reviews_qty']['fact'] == 7.0
    assert admin_cells['reviews_qty']['fact'] == 7.0

    network_cells = {cell['code']: cell for cell in network_response.json()['data']['groups'][0]['metrics']}
    assert network_cells['reviews_qty']['fact'] == 10.0


@pytest.mark.asyncio
async def test_manual_review_facts_daily_validation(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        long_range_response = await client.get(
            '/dashboard/plan/reviews_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-07-02', 'company_id': 1},
        )
        outside_date_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'start_date': '2025-06-01',
                'end_date': '2025-06-04',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'date': '2025-06-05', 'value': 1}],
            },
        )
        negative_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'start_date': '2025-06-01',
                'end_date': '2025-06-04',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'date': '2025-06-01', 'value': -1}],
            },
        )
        zero_response = await client.post(
            '/dashboard/plan/reviews_fact',
            json={
                'start_date': '2025-06-01',
                'end_date': '2025-06-04',
                'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'date': '2025-06-02', 'value': 0}],
            },
        )
    app.dependency_overrides.clear()

    assert long_range_response.status_code == 400
    assert outside_date_response.status_code == 400
    assert negative_response.status_code == 400
    assert zero_response.status_code == 200
    zero_row = zero_response.json()['data']['rows'][0]
    assert zero_row['value'] == 0.0
    assert [item['value'] for item in zero_row['values']] == [None, 0.0, None, None]


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
                'branches': [{'company_id': 1, 'wax_pct': '0,2', 'cosmo_price': 2000}],
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
    assert data['branches'][0]['wax_pct'] == 0.2
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
                'head_care_pct': 0.1,
                'face_care_pct': 0.05,
                'camouflage_pct': 0.05,
                'cosmo_pct': 0.1,
                'opz_pct': 0.25,
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
                },
            ],
        }

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        may_save = await client.post('/dashboard/plan/settings', json=settings_payload('2025-05', '0,2', 12))
        june_save = await client.post('/dashboard/plan/settings', json=settings_payload('2025-06', 0.3, 20))
        may_plan = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-05-10', 'end_date': '2025-05-20', 'company_id': 1},
        )
        june_plan = await client.get(
            '/dashboard/widget/plan_fact',
            params={'start_date': '2025-06-01', 'end_date': '2025-06-15', 'company_id': 1},
        )
        june_resave = await client.post('/dashboard/plan/settings', json=settings_payload('2025-06', 0.4, 25))
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
    assert may_parent_cells['reviews_qty']['plan'] == 12.0
    assert may_parent_cells['cosmo_qty']['plan'] == 10.0

    may_admin = next(group for group in may_data['groups'] if group['category'] == 'administrator')
    may_admin_cells = {cell['code']: cell for cell in may_admin['metrics']}
    assert may_admin_cells['clients']['plan'] == 90.0
    assert may_admin_cells['reviews_qty']['plan'] == 12.0
    assert may_admin_cells['cosmo_qty']['plan'] == 4.0

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
    app.dependency_overrides.clear()

    assert r.status_code == 200
    groups = r.json()['data']['groups']
    barber_group = next(g for g in groups if g['category'] == 'barber')
    admin_opz_by_staff = {
        group['staff_id']: next(m for m in group['metrics'] if m['code'] == 'opz_qty')['fact']
        for group in groups
        if group['category'] == 'administrator'
    }
    barber_opz = next(m for m in barber_group['metrics'] if m['code'] == 'opz_qty')['fact']
    assert barber_opz == 1.0
    assert admin_opz_by_staff == {2: 0.0, 3: 1.0}
    assert sum(admin_opz_by_staff.values()) == barber_opz


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
async def test_plan_fact_branch_totals_use_visible_staff_facts(async_session):
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
    app.dependency_overrides.clear()

    assert r.status_code == 200
    data = r.json()['data']
    branch_cells = {cell['code']: cell for cell in data['parent_group']['metrics']}
    groups = data['groups']
    barber_clients = sum(
        next(m for m in group['metrics'] if m['code'] == 'clients')['fact']
        for group in groups
        if group['category'] == 'barber'
    )
    admin_clients = sum(
        next(m for m in group['metrics'] if m['code'] == 'clients')['fact']
        for group in groups
        if group['category'] == 'administrator'
    )
    barber_opz = sum(
        next(m for m in group['metrics'] if m['code'] == 'opz_qty')['fact']
        for group in groups
        if group['category'] == 'barber'
    )
    admin_opz = sum(
        next(m for m in group['metrics'] if m['code'] == 'opz_qty')['fact']
        for group in groups
        if group['category'] == 'administrator'
    )

    assert branch_cells['clients']['fact'] == barber_clients == admin_clients == 1.0
    assert branch_cells['opz_qty']['fact'] == barber_opz == admin_opz == 1.0


@pytest.mark.asyncio
async def test_plan_fact_network_branch_rows_use_normalized_staff_facts(async_session):
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
    app.dependency_overrides.clear()

    assert branch_response.status_code == 200
    assert network_response.status_code == 200
    branch_cells = {cell['code']: cell for cell in branch_response.json()['data']['parent_group']['metrics']}
    network_groups = network_response.json()['data']['groups']
    network_cells = {cell['code']: cell for cell in network_groups[0]['metrics']}
    network_branch = next(group for group in network_groups if group['scope'] == 'branch')
    network_branch_cells = {cell['code']: cell for cell in network_branch['metrics']}

    assert network_branch_cells['clients']['fact'] == branch_cells['clients']['fact'] == 1.0
    assert network_branch_cells['opz_qty']['fact'] == branch_cells['opz_qty']['fact'] == 1.0
    assert network_cells['clients']['fact'] == 1.0
    assert network_cells['opz_qty']['fact'] == 1.0


@pytest.mark.asyncio
async def test_admin_fact_revenue_uses_created_records_and_goods_cost(async_session):
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
    admin_group = next(g for g in r.json()['data']['groups'] if g['category'] == 'administrator')
    admin_cells = {cell['code']: cell for cell in admin_group['metrics']}
    assert admin_cells['revenue']['fact'] == 1300.0
    assert admin_cells['avg_check_total']['fact'] == 1300.0
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
    async_session.add(
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=1,
            staff_category='barber',
            metric_code='clients',
            value=1.0,
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
    assert barber_clients == 3.0
    assert sum(admin_clients_by_staff.values()) == barber_clients
    assert admin_clients_by_staff == {2: 2.0, 3: 1.0}


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
async def test_plan_fact_lists_active_staff_when_branch_has_only_branch_plan(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=1, name='Active', position='Барбер', company_id=1, fired=0))
    async_session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    async_session.add(Staff(id=3, name='Fired', position='Барбер', company_id=1, fired=1))
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
    assert [group['title'] for group in groups] == ['Admin', 'Active']
    barber_cells = {cell['code']: cell for cell in groups[1]['metrics']}
    assert barber_cells['revenue']['plan'] is None


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
    app.dependency_overrides.clear()

    assert jan.status_code == 200
    assert feb.status_code == 200

    jan_branch = next(group for group in jan.json()['data']['groups'] if group['scope'] == 'branch')
    feb_branch = next(group for group in feb.json()['data']['groups'] if group['scope'] == 'branch')
    jan_cells = {cell['code']: cell for cell in jan_branch['metrics']}
    feb_cells = {cell['code']: cell for cell in feb_branch['metrics']}
    assert jan_cells['opz_qty']['fact'] == 1.0
    assert feb_cells['opz_qty']['fact'] == 1.0


@pytest.mark.asyncio
async def test_plan_sheet_csv_imports_wide_branch_rows(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,branch,выручка,кол-во клиентов,воск\n'
        '2025-01,Salon,"10 000",5,2\n',
    )

    assert result['imported'] == 3
    assert result['diagnostics']['parsed_rows'] == {'total': 1, 'network': 0, 'branch': 1, 'staff': 0}
    assert result['diagnostics']['imported_metrics'] == {'total': 3, 'branch': 3, 'staff': 0}
    assert 'plan sheet has no staff rows; staff plans will be empty' in result['warnings']
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
            )
        )
    ).scalars().all()
    values = {row.metric_code: row.value for row in rows}
    assert values == {'revenue': 10000.0, 'clients': 5.0, 'wax_qty': 2.0}


@pytest.mark.asyncio
async def test_services_sheet_csv_imports_extra_service_labels(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Group(id=2, title='G2'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=2))
    async_session.add(Service(id=10, title='Стрижка', company_id=1))
    async_session.add(Service(id=11, title='Воск', company_id=1))
    async_session.add(Service(id=12, title='Воск', company_id=2))
    await async_session.commit()

    result = await import_services_sheet_csv(
        async_session,
        'company_id,service_id,service_title,доп услуга\n'
        '1,10,Стрижка,нет\n'
        ',,Воск,да\n',
    )

    assert result['imported'] == 2
    assert result['processed'] == 2
    rows = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assert sorted((row.service_id, row.company_id, row.is_extra) for row in rows) == [
        (11, 1, True),
        (12, 2, True),
    ]


@pytest.mark.asyncio
async def test_services_sheet_csv_expands_unscoped_shared_service_labels_from_transactions(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add(Service(id=10, title='Воск', company_id=1))
    async_session.add_all([
        ServiceCatalog(company_id=1, service_id=10, title='Воск', updated_at=datetime(2025, 1, 1, 0, 0, 0)),
        ServiceCatalog(company_id=2, service_id=10, title='Воск', updated_at=datetime(2025, 1, 1, 0, 0, 0)),
    ])
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Воск', amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=10, service_title='Воск', amount=1, company_id=2),
    ])
    await async_session.commit()

    result = await import_services_sheet_csv(
        async_session,
        'id_услуги,Название услуги,доп услуга\n'
        '10,Воск,да\n',
    )

    assert result['imported'] == 2
    assert result['processed'] == 1
    rows = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assert sorted((row.service_id, row.company_id, row.is_extra) for row in rows) == [
        (10, 1, True),
        (10, 2, True),
    ]


@pytest.mark.asyncio
async def test_services_sheet_csv_keeps_explicit_branch_scope_for_shared_service_ids(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add(Service(id=10, title='Воск', company_id=1))
    async_session.add_all([
        ServiceCatalog(company_id=1, service_id=10, title='Воск', updated_at=datetime(2025, 1, 1, 0, 0, 0)),
        ServiceCatalog(company_id=2, service_id=10, title='Воск', updated_at=datetime(2025, 1, 1, 0, 0, 0)),
    ])
    async_session.add_all([
        Transaction(id=1, appointment_id=1, service_id=10, service_title='Воск', amount=1, company_id=1),
        Transaction(id=2, appointment_id=2, service_id=10, service_title='Воск', amount=1, company_id=2),
    ])
    await async_session.commit()

    result = await import_services_sheet_csv(
        async_session,
        'company_id,id_услуги,Название услуги,доп услуга\n'
        '1,10,Воск,да\n',
    )

    assert result['imported'] == 1
    assert result['processed'] == 1
    rows = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assert [(row.service_id, row.company_id, row.is_extra) for row in rows] == [
        (10, 1, True),
    ]


def test_google_sheet_page_url_is_normalized_to_csv_export():
    url = 'https://docs.google.com/spreadsheets/d/sheet-id/edit#gid=12345'

    assert _normalize_google_sheet_csv_url(url) == (
        'https://docs.google.com/spreadsheets/d/sheet-id/export?format=csv&gid=12345'
    )


def test_spreadsheet_id_is_extracted_from_google_sheet_url():
    url = 'https://docs.google.com/spreadsheets/d/sheet-id/edit#gid=12345'

    assert _spreadsheet_id_from_url(url) == 'sheet-id'


def test_google_sheet_values_are_converted_to_csv_text():
    csv_text = _google_sheet_values_to_csv_text([
        ['Категория', 'id_услуги', 'Название услуги', 'доп услуга'],
        ['Уход', 10, 'Black Mask', 'да'],
    ])

    rows = list(csv.DictReader(csv_text.splitlines()))
    assert rows == [{
        'Категория': 'Уход',
        'id_услуги': '10',
        'Название услуги': 'Black Mask',
        'доп услуга': 'да',
    }]


def test_service_account_sheet_read_reports_generic_missing_sheet_id(monkeypatch):
    monkeypatch.setattr(plan_import, '_service_account_info', lambda: {'private_key': 'unused'})

    with pytest.raises(ValueError, match='Google sheet id is not configured'):
        plan_import._sheet_csv_text_from_service_account('', 'plan')


@pytest.mark.asyncio
async def test_services_sheet_csv_imports_current_format_with_category_scope(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Group(id=2, title='G2'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=2))
    async_session.add(Service(id=10, title='Black Mask', category_title='Уход', company_id=1))
    async_session.add(Service(id=11, title='Black Mask', category_title='Основные', company_id=2))
    async_session.add(Service(id=12, title='Окантовка', category_title='Финиш', company_id=2))
    await async_session.commit()

    result = await import_services_sheet_csv(
        async_session,
        'Категория,id_услуги,Название услуги,доп услуга\n'
        'Уход,,Black Mask,да\n'
        'Финиш,12,Окантовка,да\n',
    )

    assert result['imported'] == 2
    assert result['processed'] == 2
    rows = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assert sorted((row.service_id, row.company_id, row.is_extra) for row in rows) == [
        (10, 1, True),
        (12, 2, True),
    ]


@pytest.mark.asyncio
async def test_services_sheet_csv_category_falls_back_when_service_categories_are_empty(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Service(id=10, title='Black Mask', category_title='', company_id=1))
    await async_session.commit()

    result = await import_services_sheet_csv(
        async_session,
        'Категория,id_услуги,Название услуги,доп услуга\n'
        'Уход,,Black Mask,да\n',
    )

    assert result['imported'] == 1
    assert result['processed'] == 1
    rows = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assert [(row.service_id, row.company_id, row.is_extra) for row in rows] == [
        (10, 1, True),
    ]


@pytest.mark.asyncio
async def test_services_sheet_config_import_falls_back_to_service_account(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Service(id=10, title='Black Mask', category_title='Уход', company_id=1))
    await async_session.commit()

    def fail_csv_url(url):
        raise RuntimeError('401')

    def fake_service_account_sheet(sheet_id, sheet_name):
        assert sheet_id == 'sheet-id'
        assert sheet_name == 'services'
        return (
            'Категория,id_услуги,Название услуги,доп услуга\n'
            'Уход,10,Black Mask,да\n'
        )

    monkeypatch.setattr(
        plan_import,
        'SERVICES_SHEET_CSV_URL',
        'https://docs.google.com/spreadsheets/d/sheet-id/export?format=csv&gid=12345',
    )
    monkeypatch.setattr(plan_import, 'PLAN_SHEET_CSV_URL', '')
    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_ID', '')
    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_NAME', 'services')
    monkeypatch.setattr(plan_import, '_csv_text_from_url', fail_csv_url)
    monkeypatch.setattr(plan_import, '_sheet_csv_text_from_service_account', fake_service_account_sheet)

    result = await plan_import.import_services_sheet_from_config(async_session)

    assert result['imported'] == 1
    assert result['processed'] == 1
    rows = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assert [(row.service_id, row.company_id, row.is_extra) for row in rows] == [
        (10, 1, True),
    ]


@pytest.mark.asyncio
async def test_services_sheet_config_import_uses_plan_sheet_id_for_service_account(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Service(id=10, title='Black Mask', category_title='Уход', company_id=1))
    await async_session.commit()

    def fake_service_account_sheet(sheet_id, sheet_name):
        assert sheet_id == 'plan-sheet-id'
        assert sheet_name == 'services'
        return (
            'Категория,id_услуги,Название услуги,доп услуга\n'
            'Уход,10,Black Mask,да\n'
        )

    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_CSV_URL', '')
    monkeypatch.setattr(plan_import, 'PLAN_SHEET_CSV_URL', '')
    monkeypatch.setattr(plan_import, 'PLAN_SHEET_ID', 'plan-sheet-id')
    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_ID', '')
    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_NAME', 'services')
    monkeypatch.setattr(plan_import, '_sheet_csv_text_from_service_account', fake_service_account_sheet)

    result = await plan_import.import_services_sheet_from_config(async_session)

    assert result['imported'] == 1
    assert result['processed'] == 1
    rows = (await async_session.execute(select(ServiceLabel))).scalars().all()
    assert [(row.service_id, row.company_id, row.is_extra) for row in rows] == [
        (10, 1, True),
    ]


@pytest.mark.asyncio
async def test_plan_sheet_config_import_falls_back_to_service_account(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Staff(id=10, name='Alice', position='Барбер', company_id=1))
    await async_session.commit()

    def fail_csv_url(url):
        raise RuntimeError('401')

    def fake_service_account_sheet(sheet_id, sheet_name):
        assert sheet_id == 'sheet-id'
        if sheet_name == 'plan':
            return (
                'month,company_id,branch,staff_id,position,Выручка,Кол-во клиентов,"Воск, шт"\n'
                '2025-01,1,Salon 1,10,Барбер,8000,4,2\n'
            )
        return 'Категория,id_услуги,Название услуги,доп услуга\n'

    monkeypatch.setattr(
        plan_import,
        'PLAN_SHEET_CSV_URL',
        'https://docs.google.com/spreadsheets/d/sheet-id/export?format=csv&gid=0',
    )
    monkeypatch.setattr(plan_import, 'PLAN_SHEET_ID', '')
    monkeypatch.setattr(plan_import, 'PLAN_SHEET_NAME', 'plan')
    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_CSV_URL', '')
    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_ID', '')
    monkeypatch.setattr(plan_import, 'SERVICES_SHEET_NAME', 'services')
    monkeypatch.setattr(plan_import, '_csv_text_from_url', fail_csv_url)
    monkeypatch.setattr(plan_import, '_sheet_csv_text_from_service_account', fake_service_account_sheet)

    result = await plan_import.import_plan_sheet_from_config(async_session)

    # staff row (3 metrics) + derived branch plan (3 summed metrics + avg check)
    assert result['imported'] == 7
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.company_id == 1,
                PlanMetric.staff_id == 10,
            )
        )
    ).scalars().all()
    values = {row.metric_code: row.value for row in rows}
    assert values == {'revenue': 8000.0, 'clients': 4.0, 'wax_qty': 2.0}


@pytest.mark.asyncio
async def test_plan_sheet_config_import_prefers_named_service_account_sheet(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Staff(id=10, name='Alice', position='Барбер', company_id=1))
    await async_session.commit()

    csv_called = False

    def csv_url(_url):
        nonlocal csv_called
        csv_called = True
        return (
            'month,company_id,branch,Выручка,Кол-во клиентов\n'
            '2025-01,1,Salon 1,10000,5\n'
        )

    def fake_service_account_sheet(sheet_id, sheet_name):
        assert sheet_id == 'plan-sheet-id'
        assert sheet_name == 'plan'
        return (
            'month,company_id,branch,staff_id,position,Выручка,Кол-во клиентов,"Воск, шт"\n'
            '2025-01,1,Salon 1,10,Барбер,8000,4,2\n'
        )

    async def fake_services(_db):
        return {'imported': 0, 'processed': 0, 'skipped': [], 'warnings': []}

    monkeypatch.setattr(
        plan_import,
        'PLAN_SHEET_CSV_URL',
        'https://docs.google.com/spreadsheets/d/csv-sheet-id/export?format=csv&gid=0',
    )
    monkeypatch.setattr(plan_import, 'PLAN_SHEET_ID', 'plan-sheet-id')
    monkeypatch.setattr(plan_import, 'PLAN_SHEET_NAME', 'plan')
    monkeypatch.setattr(plan_import, '_csv_text_from_url', csv_url)
    monkeypatch.setattr(plan_import, '_sheet_csv_text_from_service_account', fake_service_account_sheet)
    monkeypatch.setattr(plan_import, 'import_services_sheet_from_config', fake_services)

    result = await plan_import.import_plan_sheet_from_config(async_session)

    assert csv_called is False
    assert result['imported'] == 7
    assert result['diagnostics']['parsed_rows']['staff'] == 1
    assert result['warnings'] == []


@pytest.mark.asyncio
async def test_plan_sheet_csv_imports_staff_rows_and_validates_branch_totals(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Alice', position='Барбер', company_id=1))
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,branch,staff_id,category,выручка,кол-во клиентов,воск\n'
        '2025-01,Salon,,,10000,5,2\n'
        '2025-01,,10,Барбер,8000,4,2\n',
    )

    assert result['imported'] == 7
    assert any('branch staff total' in warning for warning in result['warnings'])
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
                PlanMetric.staff_id == 10,
            )
        )
    ).scalars().all()
    values = {row.metric_code: row.value for row in rows}
    assert values == {'revenue': 8000.0, 'clients': 4.0, 'wax_qty': 2.0}
    assert {row.staff_category for row in rows} == {'barber'}

    branch_rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
                PlanMetric.staff_id.is_(None),
            )
        )
    ).scalars().all()
    branch_values = {row.metric_code: row.value for row in branch_rows}
    assert branch_values == {
        'revenue': 8000.0,
        'clients': 4.0,
        'wax_qty': 2.0,
        'avg_check_total': 2000.0,
    }


@pytest.mark.asyncio
async def test_plan_sheet_csv_imports_flat_staff_rows_and_derives_branch_plan(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Alice', position='Барбер', company_id=1))
    async_session.add(Staff(id=20, name='Admin', position='Администратор', company_id=1))
    async_session.add(
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=10,
            staff_category='barber',
            metric_code='wax_qty',
            value=9.0,
            updated_at=datetime(2025, 1, 1),
        )
    )
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,company_id,branch,stuff_id,stuff_name,position,Выручка,СЧ общий,Кол-во клиентов,"Воск, шт","Камуфляж, шт","Уход лицо, шт","Уход голова, шт","Космо, шт",Космо сумм.,"ОПЗ, шт"\n'
        '2025-01,1,Salon,10,Alice,Барбер,7000,,7,,2,,,,1000,1\n'
        '2025-01,1,Salon,20,Admin,Администратор,3000,,3,,,,,4,500,2\n',
    )

    assert result['imported'] == 17
    assert result['warnings'] == []

    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
            )
        )
    ).scalars().all()
    values = {
        (row.staff_id, row.metric_code): row.value
        for row in rows
    }
    assert values[(10, 'revenue')] == 7000.0
    assert values[(10, 'camouflage_qty')] == 2.0
    assert (10, 'wax_qty') not in values
    assert values[(20, 'revenue')] == 3000.0
    assert values[(20, 'cosmo_sum')] == 500.0
    assert values[(None, 'revenue')] == 10000.0
    assert values[(None, 'clients')] == 10.0
    assert values[(None, 'cosmo_sum')] == 1500.0
    assert values[(None, 'avg_check_total')] == 1000.0


@pytest.mark.asyncio
async def test_plan_sheet_csv_branch_avg_check_averages_barber_avg_checks(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Alice', position='Барбер', company_id=1))
    async_session.add(Staff(id=11, name='Bob', position='Барбер', company_id=1))
    async_session.add(Staff(id=20, name='Admin', position='Администратор', company_id=1))
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,company_id,staff_id,position,Выручка,СЧ общий,Кол-во клиентов\n'
        '2025-01,1,10,Барбер,4000,1000,4\n'
        '2025-01,1,11,Барбер,9000,,3\n'
        '2025-01,1,20,Администратор,10000,10000,1\n',
    )

    assert result['warnings'] == []
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
                PlanMetric.staff_id.is_(None),
            )
        )
    ).scalars().all()
    branch_values = {row.metric_code: row.value for row in rows}
    assert branch_values['revenue'] == 23000.0
    assert branch_values['clients'] == 8.0
    assert branch_values['avg_check_total'] == 2000.0


@pytest.mark.asyncio
async def test_plan_sheet_csv_excludes_staff_rows_with_zero_client_plan(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Alice', position='Барбер', company_id=1))
    async_session.add(Staff(id=20, name='Bob', position='Барбер', company_id=1))
    async_session.add(Staff(id=30, name='Charlie', position='Барбер', company_id=1))
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,company_id,staff_id,position,выручка,кол-во клиентов,воск\n'
        '2025-01,1,10,Барбер,8000,4,2\n'
        '2025-01,1,20,Барбер,9000,3,5\n'
        '2025-01,1,20,Барбер,9000,0,5\n'
        '2025-01,1,30,Барбер,0,,0\n',
    )

    assert result['imported'] == 7
    assert result['diagnostics']['parsed_rows']['staff'] == 4
    assert result['diagnostics']['effective_rows']['staff'] == 1
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
            )
        )
    ).scalars().all()
    values = {
        (row.staff_id, row.metric_code): row.value
        for row in rows
    }
    assert values == {
        (10, 'revenue'): 8000.0,
        (10, 'clients'): 4.0,
        (10, 'wax_qty'): 2.0,
        (None, 'revenue'): 8000.0,
        (None, 'clients'): 4.0,
        (None, 'wax_qty'): 2.0,
        (None, 'avg_check_total'): 2000.0,
    }


@pytest.mark.asyncio
async def test_plan_sheet_csv_replaces_duplicate_staff_month_with_latest_row(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=10, name='Alice', position='Барбер', company_id=1))
    async_session.add_all([
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            staff_id=10,
            staff_category='barber',
            metric_code='wax_qty',
            value=9.0,
            updated_at=datetime(2025, 1, 1),
        ),
        PlanMetric(
            period_start=date(2025, 1, 1),
            period_end=date(2025, 1, 31),
            company_id=1,
            metric_code='avg_check_total',
            value=9999.0,
            updated_at=datetime(2025, 1, 1),
        ),
    ])
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,company_id,staff_id,position,выручка,кол-во клиентов,воск\n'
        '2025-01,1,10,Барбер,8000,4,1\n'
        '2025-01,1,10,Барбер,9000,3,\n',
    )

    assert result['imported'] == 5
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
            )
        )
    ).scalars().all()
    values = {
        (row.staff_id, row.metric_code): row.value
        for row in rows
    }
    assert values == {
        (10, 'revenue'): 9000.0,
        (10, 'clients'): 3.0,
        (None, 'revenue'): 9000.0,
        (None, 'clients'): 3.0,
        (None, 'avg_check_total'): 3000.0,
    }

    plan_fact = await fetch_plan_fact(async_session, date(2025, 1, 1), date(2025, 1, 31), company_id=1)
    branch_cells = {cell['code']: cell for cell in plan_fact['parent_group']['metrics']}
    assert branch_cells['avg_check_total']['plan'] == 3000.0


@pytest.mark.asyncio
async def test_plan_sheet_csv_skips_metrics_not_applicable_to_administrators(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(Staff(id=20, name='Admin', position='Администратор', company_id=1))
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,branch,staff_id,выручка,воск,космо сумм.\n'
        '2025-01,Salon,20,1000,3,500\n',
    )

    assert result['imported'] == 4
    assert any('skipped metrics not applicable to administrator' in warning for warning in result['warnings'])
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
                PlanMetric.staff_id == 20,
            )
        )
    ).scalars().all()
    values = {row.metric_code: row.value for row in rows}
    assert values == {'revenue': 1000.0, 'cosmo_sum': 500.0}


@pytest.mark.asyncio
async def test_plan_sheet_csv_imports_google_thousands_commas(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    await async_session.commit()

    result = await import_plan_sheet_csv(
        async_session,
        'month,company_id,Выручка,СЧ общий,Космо сумм.\n'
        '2025-01,1,"2,156,400","3,655","138,000"\n',
    )

    assert result['imported'] == 3
    rows = (
        await async_session.execute(
            select(PlanMetric).where(
                PlanMetric.period_start == date(2025, 1, 1),
                PlanMetric.period_end == date(2025, 1, 31),
                PlanMetric.company_id == 1,
            )
        )
    ).scalars().all()
    values = {row.metric_code: row.value for row in rows}
    assert values == {
        'revenue': 2156400.0,
        'avg_check_total': 3655.0,
        'cosmo_sum': 138000.0,
    }
