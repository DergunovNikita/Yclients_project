"""Typed plan and manual-fact numbers have a ceiling; personal roles list only themselves in /dashboard/staff."""

from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

import api
import auth_deps
import dashboard_service
from api import app
from conftest import issue_access_token
from helpers import cached_password_hash
from models import Company, Group, PortalAccount, PortalBranch, PortalUser, PortalUserBranch, Staff


async def _seed(session):
    session.add(Group(id=1, title='G'))
    session.add(Company(id=1, title='Salon', group_id=1))
    session.add(Staff(id=2, name='Admin', position='Администратор', company_id=1, fired=0))
    await session.commit()


async def _client(session):
    async def override_db():
        yield session

    app.dependency_overrides[api.get_async_db] = override_db
    return AsyncClient(transport=ASGITransport(app=app), base_url='http://test')


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/dashboard/plan/opz_fact', '/dashboard/plan/reviews_fact'])
async def test_manual_fact_above_the_ceiling_is_a_400(async_session, path):
    await _seed(async_session)
    try:
        async with await _client(async_session) as client:
            too_big = await client.post(path, json={
                'month': '2025-01', 'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': 1e300}],
            })
            at_cap = await client.post(path, json={
                'month': '2025-01', 'company_id': 1,
                'items': [{'company_id': 1, 'staff_id': 2, 'value': dashboard_service.MAX_INPUT_VALUE}],
            })
    finally:
        app.dependency_overrides.clear()

    assert too_big.status_code == 400
    assert 'cannot exceed' in too_big.json()['detail']
    assert at_cap.status_code == 200


@pytest.mark.asyncio
async def test_plan_settings_above_the_ceiling_are_a_400(async_session):
    await _seed(async_session)

    def payload(clients):
        return {
            'month': '2025-05',
            'branches': [{'company_id': 1, 'wax_pct': 10}],
            'staff': [{'company_id': 1, 'staff_id': 2, 'staff_category': 'administrator', 'clients': clients}],
        }

    try:
        async with await _client(async_session) as client:
            too_big = await client.post('/dashboard/plan/settings', json=payload(1e200))
            at_cap = await client.post(
                '/dashboard/plan/settings', json=payload(dashboard_service.MAX_INPUT_VALUE)
            )
    finally:
        app.dependency_overrides.clear()

    assert too_big.status_code == 400
    assert 'cannot exceed' in too_big.json()['detail']
    assert at_cap.status_code == 200


@pytest.mark.asyncio
async def test_dashboard_staff_lists_only_the_own_row_for_a_personal_role(async_session, monkeypatch):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    now = datetime(2025, 1, 1)
    async_session.add(Group(id=1, title='G'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=now))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    for user_id, role in ((100, 'barber'), (102, 'owner'), (104, 'barber')):
        async_session.add(PortalUser(
            id=user_id,
            portal_account_id=1,
            email=f'user{user_id}@example.com',
            password_hash=cached_password_hash('Password12345!'),
            role=role,
            is_active=True,
            email_verified_at=now,
            created_at=now,
        ))
        async_session.add(PortalUserBranch(user_id=user_id, company_id=1))
    async_session.add_all([
        Staff(id=1, name='Linked', position='Барбер', company_id=1, fired=0, portal_user_id=100, user_id=555),
        Staff(id=2, name='Colleague', position='Барбер', company_id=1, fired=0),
    ])
    await async_session.commit()

    barber = {'Authorization': f'Bearer {await issue_access_token(async_session, 100, "barber")}'}
    owner = {'Authorization': f'Bearer {await issue_access_token(async_session, 102, "owner")}'}
    orphan = {'Authorization': f'Bearer {await issue_access_token(async_session, 104, "barber")}'}
    try:
        async with await _client(async_session) as client:
            own = await client.get('/dashboard/staff', headers=barber)
            branch = await client.get('/dashboard/staff', headers=owner)
            no_row = await client.get('/dashboard/staff', headers=orphan)
    finally:
        app.dependency_overrides.clear()

    assert [row['name'] for row in own.json()['data']] == ['Linked']
    assert [row['name'] for row in branch.json()['data']] == ['Colleague', 'Linked']
    assert all('user_id' not in row for row in branch.json()['data'])
    assert no_row.status_code == 403
