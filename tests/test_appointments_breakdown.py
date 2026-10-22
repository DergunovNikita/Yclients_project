from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient

import api
import dashboard_service
from api import app
from report_helpers import table_rows
from dashboard_service import _ready_appointments_breakdown, fetch_appointments_breakdown
from models import (
    Appointment,
    Company,
    FinancialTransaction,
    Group,
    Staff,
    Transaction,
)

JUNE = (date(2026, 6, 1), date(2026, 6, 30))


async def _seed_two_branches(session):
    """Branch 1: three June visits (completed, no-show, pending) and one in July; branch 2: one June visit."""
    session.add_all([
        Group(id=1, title='G'),
        Company(id=1, title='One', group_id=1),
        Company(id=2, title='Two', group_id=1),
    ])
    await session.flush()
    session.add_all([
        Staff(id=1, name='Master', company_id=1, fired=0),
        Staff(id=22, name='Other master', company_id=2, fired=0),
    ])
    await session.flush()
    session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, date=date(2026, 6, 10), attendance=1),
        Appointment(id=2, company_id=1, staff_id=1, date=date(2026, 6, 10), attendance=-1),
        Appointment(id=3, company_id=1, staff_id=1, date=date(2026, 6, 10), attendance=2),
        Appointment(id=4, company_id=1, staff_id=1, date=date(2026, 7, 10), attendance=1),
        Appointment(id=5, company_id=2, staff_id=22, date=date(2026, 6, 10), attendance=1),
    ])
    await session.commit()


def test_appointment_shares_are_integer_and_sum_to_100():
    data = _ready_appointments_breakdown({
        'total': 3,
        'cancelled': 1,
        'completed': 1,
        'incomplete': 1,
    })

    assert data['source_status'] == 'ready'
    assert data['cancelled_share_pct'] == 34
    assert data['completed_share_pct'] == 33
    assert data['incomplete_share_pct'] == 33
    assert data['cancelled_share_pct'] + data['completed_share_pct'] + data['incomplete_share_pct'] == 100
    assert data['total_share_pct'] == 100


def test_appointment_shares_handle_single_category_and_empty_period():
    single = _ready_appointments_breakdown({
        'total': 5,
        'cancelled': 0,
        'completed': 5,
        'incomplete': 0,
    })
    empty = _ready_appointments_breakdown({
        'total': 0,
        'cancelled': 0,
        'completed': 0,
        'incomplete': 0,
    })

    assert single['completed_share_pct'] == 100
    assert empty['total_share_pct'] == 0
    assert empty['cancelled_share_pct'] == 0
    assert empty['completed_share_pct'] == 0
    assert empty['incomplete_share_pct'] == 0


def test_appointment_breakdown_rejects_inconsistent_totals():
    data = _ready_appointments_breakdown({
        'total': 10,
        'cancelled': 2,
        'completed': 6,
        'incomplete': 1,
    })

    assert data['source_status'] == 'unavailable'
    assert data['total'] is None


@pytest.mark.asyncio
async def test_dashboard_summary_exposes_exact_appointment_contract(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G'))
    async_session.add(Company(id=1, title='One', group_id=1))
    await async_session.commit()

    async def fake_record_stats(company_ids, start, end, staff_id, db=None):
        assert company_ids == [1]
        return {'total': 10, 'cancelled': 3, 'completed': 6, 'incomplete': 1}

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fake_record_stats)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/bundle',
            params={
                'start_date': '2026-06-01',
                'end_date': '2026-06-30',
                'company_id': 1,
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    breakdown = response.json()['data']['summary']['appointments_breakdown']
    assert breakdown == {
        'source_status': 'ready',
        'total': 10,
        'cancelled': 3,
        'completed': 6,
        'incomplete': 1,
        'total_share_pct': 100,
        'cancelled_share_pct': 30,
        'completed_share_pct': 60,
        'incomplete_share_pct': 10,
    }


@pytest.mark.asyncio
async def test_confirmed_appointment_is_not_counted_as_completed_visit(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G'))
    async_session.add(Company(id=1, title='One', group_id=1))
    async_session.add(Staff(id=1, name='Master', company_id=1, fired=0))
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            client_id=1,
            date=date(2026, 6, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            client_id=2,
            date=date(2026, 6, 10),
            attendance=2,
        ),
    ])
    await async_session.flush()
    async_session.add_all([
        Transaction(
            id=1,
            appointment_id=1,
            service_id=10,
            service_title='Cut',
            cost=1000,
            amount=1,
            company_id=1,
        ),
        Transaction(
            id=2,
            appointment_id=2,
            service_id=10,
            service_title='Cut',
            cost=1000,
            amount=1,
            company_id=1,
        ),
        FinancialTransaction(
            id=1,
            date=date(2026, 6, 10),
            amount=1000,
            record_id=1,
            sold_item_id=10,
            sold_item_type='service',
            company_id=1,
        ),
    ])
    await async_session.commit()

    async def fake_record_stats(company_ids, start, end, staff_id, db=None):
        if start == date(2026, 6, 1):
            return {'total': 2, 'cancelled': 0, 'completed': 1, 'incomplete': 1}
        return {'total': 0, 'cancelled': 0, 'completed': 0, 'incomplete': 0}

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fake_record_stats)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/bundle',
            params={
                'start_date': '2026-06-01',
                'end_date': '2026-06-18',
                'company_id': 1,
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['summary']['revenue']['appointments'] == 1
    assert data['summary']['appointments_breakdown']['completed'] == 1
    assert data['summary']['appointments_breakdown']['incomplete'] == 1
    assert sum(item['appointments'] for item in data['revenue_daily']) == 1
    assert data['summary']['revenue']['service_count'] == 1
    assert data['top_services'][0]['sold'] == 1


@pytest.mark.asyncio
async def test_cancellation_report_uses_exact_aggregate_counts(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G'))
    async_session.add(Company(id=1, title='One', group_id=1))
    async_session.add(Staff(id=1, name='Master', company_id=1, fired=0))
    await async_session.flush()
    async_session.add_all([
        Appointment(
            id=1,
            company_id=1,
            staff_id=1,
            date=date(2026, 6, 10),
            attendance=1,
        ),
        Appointment(
            id=2,
            company_id=1,
            staff_id=1,
            date=date(2026, 6, 10),
            attendance=2,
        ),
        Appointment(
            id=3,
            company_id=1,
            staff_id=1,
            date=date(2026, 6, 10),
            attendance=-1,
        ),
    ])
    await async_session.commit()

    async def fake_record_stats(company_ids, start, end, staff_id, db=None):
        return {'total': 10, 'cancelled': 7, 'completed': 1, 'incomplete': 2}

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fake_record_stats)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/reports/data',
            params={
                'report_id': 'bookings_dynamics',
                'start_date': '2026-06-01',
                'end_date': '2026-06-18',
                'company_id': 1,
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert [card['value'] for card in data['cards']] == [10, 1, 7, 70.0]
    june_10 = next(row for row in table_rows(data, 'periods') if row['period'] == '2026-06-10')
    assert june_10['completed'] == 1
    assert june_10['records'] == 3
    assert june_10['no_show'] == 1
    assert len(table_rows(data, 'periods')) == 18
    assert data['notes'][0]['title'] == 'Что считается'
    assert 'агрегатов YClients' in data['notes'][0]['text']


@pytest.mark.asyncio
async def test_appointment_breakdown_falls_back_to_local_appointments(async_session, monkeypatch):
    await _seed_two_branches(async_session)

    async def fail_record_stats(*args, **kwargs):
        raise dashboard_service.yclients_analytics.YClientsAnalyticsError('timeout')

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fail_record_stats)
    data = await fetch_appointments_breakdown(async_session, *JUNE, company_id=1)

    assert data['source_status'] == 'local'
    # The one reason that is a degradation, and so the one the page warns about.
    assert data['local_reason'] == 'upstream_error'
    assert (data['total'], data['completed'], data['cancelled'], data['incomplete']) == (3, 1, 1, 1)


@pytest.mark.asyncio
async def test_appointment_scope_is_all_branches_one_branch_or_the_staffs_branch(async_session, monkeypatch):
    await _seed_two_branches(async_session)

    async def fail_record_stats(*args, **kwargs):
        raise dashboard_service.yclients_analytics.YClientsAnalyticsError('timeout')

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fail_record_stats)

    assert (await fetch_appointments_breakdown(async_session, *JUNE))['total'] == 4
    assert (await fetch_appointments_breakdown(async_session, *JUNE, company_id=1))['total'] == 3
    assert (await fetch_appointments_breakdown(async_session, *JUNE, staff_id=22))['total'] == 1


@pytest.mark.asyncio
async def test_appointment_breakdown_treats_the_branch_day_as_today(async_session, monkeypatch):
    """21:00-24:00 UTC is already tomorrow in the branch: yesterday is a closed period, served upstream."""
    async_session.add_all([Group(id=1, title='G'), Company(id=1, title='One', group_id=1)])
    await async_session.commit()

    async def fake_record_stats(company_ids, start, end, staff_id, db=None):
        return {'total': 4, 'cancelled': 1, 'completed': 3, 'incomplete': 0}

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fake_record_stats)
    late_evening_utc = datetime(2026, 6, 30, 22, 0)
    closed = await fetch_appointments_breakdown(
        async_session, date(2026, 6, 1), date(2026, 6, 30), company_id=1, factual_at=late_evening_utc
    )
    still_open = await fetch_appointments_breakdown(
        async_session, date(2026, 6, 1), date(2026, 7, 1), company_id=1, factual_at=late_evening_utc
    )

    assert closed['source_status'] == 'ready'
    assert 'local_reason' not in closed
    assert still_open['source_status'] == 'local'
    assert still_open['local_reason'] == 'period_includes_today'


@pytest.mark.asyncio
async def test_a_reporting_window_is_a_local_reason_of_its_own(async_session, monkeypatch):
    async_session.add_all([
        Group(id=1, title='G'),
        Company(id=1, title='One', group_id=1, reporting_start_date=date(2026, 6, 5)),
        Staff(id=1, name='Master', company_id=1, fired=0),
    ])
    await async_session.flush()
    async_session.add(Appointment(id=1, company_id=1, staff_id=1, date=date(2026, 6, 10), attendance=1))
    await async_session.commit()

    async def upstream(*args, **kwargs):
        return {'total': 99, 'cancelled': 0, 'completed': 99, 'incomplete': 0}

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', upstream)
    data = await fetch_appointments_breakdown(
        async_session, *JUNE, company_id=1, factual_at=datetime(2026, 7, 15, 12, 0)
    )

    # YClients knows nothing about the reporting window, so its counts are not used for such a scope.
    assert data['source_status'] == 'local'
    assert data['local_reason'] == 'reporting_window'
    assert data['total'] == 1
