from datetime import date

import pytest
from httpx import ASGITransport, AsyncClient

import api
import dashboard_service
from api import app
from dashboard_service import (
    _appointment_company_ids,
    _fetch_appointments_breakdown,
    _ready_appointments_breakdown,
)
from models import Company, Group, Staff


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
    assert data['shares_total_pct'] == 100
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
    assert single['shares_total_pct'] == 100
    assert empty['total_share_pct'] == 0
    assert empty['shares_total_pct'] == 0
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
    assert data['shares_total_pct'] is None


@pytest.mark.asyncio
async def test_appointment_breakdown_maps_counts_and_compatibility_aliases(monkeypatch):
    async def fake_record_stats(company_ids, start, end, staff_id):
        assert company_ids == [1, 2]
        assert start == date(2026, 6, 1)
        assert end == date(2026, 6, 30)
        assert staff_id is None
        return {'total': 20, 'cancelled': 5, 'completed': 12, 'incomplete': 3}

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fake_record_stats)
    data = await _fetch_appointments_breakdown(
        [1, 2],
        date(2026, 6, 1),
        date(2026, 6, 30),
        None,
    )

    assert data['source_status'] == 'ready'
    assert data['total'] == 20
    assert data['cancelled'] == 5
    assert data['completed'] == 12
    assert data['incomplete'] == 3
    assert data['attended'] == 12
    assert data['pending'] == 3
    assert data['shares_total_pct'] == 100


@pytest.mark.asyncio
async def test_appointment_breakdown_handles_source_error(monkeypatch):
    async def fail_record_stats(*args, **kwargs):
        raise dashboard_service.yclients_analytics.YClientsAnalyticsError('timeout')

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fail_record_stats)
    data = await _fetch_appointments_breakdown(
        [1],
        date(2026, 6, 1),
        date(2026, 6, 30),
        None,
    )

    assert data['source_status'] == 'unavailable'
    assert data['cancelled'] is None


@pytest.mark.asyncio
async def test_appointment_scope_uses_all_branches_or_staff_company(async_session):
    async_session.add(Group(id=1, title='G'))
    async_session.add_all([
        Company(id=1, title='One', group_id=1),
        Company(id=2, title='Two', group_id=1),
    ])
    async_session.add(Staff(id=22, name='Master', company_id=2, fired=0))
    await async_session.commit()

    assert await _appointment_company_ids(async_session, None, None) == [1, 2]
    assert await _appointment_company_ids(async_session, 1, None) == [1]
    assert await _appointment_company_ids(async_session, None, 22) == [2]


@pytest.mark.asyncio
async def test_dashboard_summary_exposes_exact_appointment_contract(async_session, monkeypatch):
    async_session.add(Group(id=1, title='G'))
    async_session.add(Company(id=1, title='One', group_id=1))
    await async_session.commit()

    async def fake_record_stats(company_ids, start, end, staff_id):
        assert company_ids == [1]
        return {'total': 10, 'cancelled': 3, 'completed': 6, 'incomplete': 1}

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', fake_record_stats)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/dashboard/widget/summary',
            params={
                'start_date': '2026-06-01',
                'end_date': '2026-06-30',
                'company_id': 1,
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == 200
    breakdown = response.json()['data']['appointments_breakdown']
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
        'shares_total_pct': 100,
        'attended': 6,
        'pending': 1,
    }
