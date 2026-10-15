"""Owner-configurable branch order: default key, override, validation, roles, audit and tenant isolation."""

from datetime import date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import api
import auth_deps
import dashboard_service
from api import app
from auth_service import create_access_token, hash_password
from conftest import new_session_row
from models import (
    Company,
    Group,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    ServiceCatalog,
    Staff,
)

USER_IDS = {'owner': 1, 'branch_admin': 2, 'manager': 3, 'barber': 4, 'demo': 5, 'platform_admin': 6, 'other_owner': 7}
PASSWORD_HASH = hash_password('Passw0rd12345!')
ENDPOINT = '/dashboard/branch-order'
# Opening date first (no date last), id as the tiebreaker: 3 and 9 open the same day.
DEFAULT_ORDER = [3, 9, 5, 2, 7]


@pytest.fixture(autouse=True)
def login_required(monkeypatch):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)


def _user(user_id, role, **extra):
    account = None if role == 'platform_admin' else extra.pop('portal_account_id', 1)
    return PortalUser(
        id=user_id,
        portal_account_id=account,
        email=f'{role}{user_id}@example.com',
        password_hash=PASSWORD_HASH,
        full_name=f'{role} name',
        role=role,
        is_active=True,
        email_verified_at=datetime(2026, 1, 1),
        created_at=datetime(2026, 1, 1),
        **extra,
    )


@pytest_asyncio.fixture
async def seeded(async_session):
    async_session.add_all([
        Group(id=1, title='G'),
        PortalAccount(id=1, label='Tenant', created_at=datetime(2026, 1, 1)),
        PortalAccount(id=2, label='Other', created_at=datetime(2026, 1, 1)),
        Company(id=5, title='Echo', group_id=1, reporting_start_date=date(2025, 1, 1)),
        Company(id=3, title='Charlie', group_id=1, reporting_start_date=date(2024, 6, 1)),
        Company(id=9, title='Alpha', group_id=1, reporting_start_date=date(2024, 6, 1)),
        Company(id=7, title='Bravo', group_id=1),
        Company(id=2, title='Delta', group_id=1),
        Company(id=40, title='Foreign', group_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        *[PortalBranch(portal_account_id=1, company_id=company) for company in (5, 3, 9, 7, 2)],
        PortalBranch(portal_account_id=2, company_id=40),
        _user(1, 'owner'),
        _user(2, 'branch_admin'),
        _user(3, 'manager'),
        _user(4, 'barber'),
        _user(5, 'owner', is_demo=True),
        _user(6, 'platform_admin'),
        _user(7, 'owner', portal_account_id=2),
        *[PortalUserBranch(user_id=user, company_id=company) for user in (2, 3, 4) for company in (5, 3, 9, 7, 2)],
        *[new_session_row(user_id, session_id=user_id) for user_id in USER_IDS.values()],
        *[Staff(id=100 + company, name='Same name', position='Барбер', company_id=company) for company in (5, 3, 9)],
        *[
            ServiceCatalog(
                company_id=company, service_id=1, title='Cut', is_active=True, updated_at=datetime(2026, 1, 1)
            )
            for company in (5, 3, 9, 7, 2)
        ],
    ])
    await async_session.commit()
    return async_session


@pytest_asyncio.fixture
async def client(seeded):
    async def override_db():
        yield seeded

    app.dependency_overrides[api.get_async_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        yield http
    app.dependency_overrides.clear()


def _auth(role):
    user_id = USER_IDS[role]
    token = create_access_token(user_id, 'owner' if role in ('demo', 'other_owner') else role, 0, user_id)
    return {'Authorization': f'Bearer {token}'}


async def _branch_ids(client, role='owner'):
    response = await client.get('/dashboard/branches', headers=_auth(role))
    assert response.status_code == 200
    return [branch['id'] for branch in response.json()['data']]


@pytest.mark.asyncio
async def test_default_order_is_opening_date_then_id_with_undated_branches_last(client):
    assert await _branch_ids(client) == DEFAULT_ORDER
    assert await _branch_ids(client, 'branch_admin') == DEFAULT_ORDER

    listed = await client.get(ENDPOINT, headers=_auth('owner'))

    assert listed.status_code == 200
    assert [(row['id'], row['sort_order']) for row in listed.json()['data']] == [(i, None) for i in DEFAULT_ORDER]


@pytest.mark.asyncio
async def test_saved_order_reaches_every_list_and_survives_for_other_roles(client, seeded):
    chosen = [2, 5, 7, 9, 3]

    saved = await client.put(ENDPOINT, json={'company_ids': chosen}, headers=_auth('owner'))

    assert saved.status_code == 200
    assert [row['id'] for row in saved.json()['data']] == chosen
    assert await _branch_ids(client) == chosen
    assert await _branch_ids(client, 'branch_admin') == chosen
    staff = await dashboard_service.fetch_staff(seeded, allowed_company_ids=chosen, force_allowed=True)
    # Same name in three branches: the branch key decides, not the title.
    assert [row['company_id'] for row in staff] == [5, 9, 3]
    services = await dashboard_service.fetch_dashboard_services(seeded, allowed_company_ids=chosen)
    assert [row['company_id'] for row in services['rows']] == chosen


@pytest.mark.asyncio
async def test_a_partly_chosen_order_still_sorts_the_rest_by_default(client, seeded):
    branch = (await seeded.execute(select(PortalBranch).where(PortalBranch.company_id == 7))).scalar_one()
    branch.sort_order = 0
    await seeded.commit()

    assert await _branch_ids(client) == [7, 3, 9, 5, 2]


@pytest.mark.asyncio
async def test_an_empty_list_restores_the_default_order(client):
    await client.put(ENDPOINT, json={'company_ids': [2, 5, 7, 9, 3]}, headers=_auth('owner'))

    reset = await client.put(ENDPOINT, json={'company_ids': []}, headers=_auth('owner'))

    assert reset.status_code == 200
    assert [row['sort_order'] for row in reset.json()['data']] == [None] * 5
    assert await _branch_ids(client) == DEFAULT_ORDER


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'company_ids',
    [
        [3, 9, 5, 2],  # a branch missing
        [3, 9, 5, 2, 7, 7],  # a branch twice
        [3, 9, 5, 2, 40],  # another tenant's branch instead of 7
        [3, 9, 5, 2, 7, 40],  # another tenant's branch added
        [3, 9, 5, 2, 7, 12345],  # unknown id
    ],
)
async def test_the_list_must_be_exactly_the_tenants_branches(client, seeded, company_ids):
    response = await client.put(ENDPOINT, json={'company_ids': company_ids}, headers=_auth('owner'))

    assert response.status_code == 400
    rows = (await seeded.execute(select(PortalBranch.sort_order))).scalars().all()
    assert rows == [None] * 6
    assert (await seeded.execute(select(PortalAuditEvent))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize('role', ['branch_admin', 'manager', 'barber'])
async def test_only_the_owner_and_platform_admin_may_read_or_change_the_order(client, seeded, role):
    body = {'company_ids': [2, 5, 7, 9, 3]}

    assert (await client.get(ENDPOINT, headers=_auth(role))).status_code == 403
    assert (await client.put(ENDPOINT, json=body, headers=_auth(role))).status_code == 403
    assert await _branch_ids(client) == DEFAULT_ORDER


@pytest.mark.asyncio
async def test_the_demo_account_cannot_change_the_order(client):
    refused = await client.put(ENDPOINT, json={'company_ids': [2, 5, 7, 9, 3]}, headers=_auth('demo'))

    assert refused.status_code == 403
    assert refused.json()['detail'] == 'Demo account is read-only'


@pytest.mark.asyncio
async def test_every_save_is_audited_before_commit_with_the_new_order(client, seeded):
    await client.put(ENDPOINT, json={'company_ids': [2, 5, 7, 9, 3]}, headers=_auth('owner'))

    events = (await seeded.execute(select(PortalAuditEvent))).scalars().all()

    assert [(event.action, event.actor_user_id, event.portal_account_id) for event in events] == [
        ('branch_order.updated', 1, 1)
    ]
    assert events[0].metadata_json == {'company_ids': [2, 5, 7, 9, 3]}


@pytest.mark.asyncio
async def test_tenants_are_isolated_and_a_platform_admin_names_the_tenant(client, seeded):
    own = await client.put(ENDPOINT, json={'company_ids': [40]}, headers=_auth('other_owner'))
    assert own.status_code == 200
    # The first tenant's owner cannot touch the second tenant's branch, and vice versa.
    assert (await client.put(ENDPOINT, json={'company_ids': [40]}, headers=_auth('owner'))).status_code == 400
    assert (await client.put(
        ENDPOINT, json={'company_ids': [3, 9, 5, 2, 7]}, headers=_auth('other_owner')
    )).status_code == 400

    unnamed = await client.get(ENDPOINT, headers=_auth('platform_admin'))
    assert unnamed.status_code == 400
    assert unnamed.json()['detail'] == 'X-Portal-Account-Id is required'
    headers = {**_auth('platform_admin'), 'X-Portal-Account-Id': '1'}
    saved = await client.put(ENDPOINT, json={'company_ids': [2, 5, 7, 9, 3]}, headers=headers)

    assert saved.status_code == 200
    assert [row['id'] for row in saved.json()['data']] == [2, 5, 7, 9, 3]
    sort_orders = dict((await seeded.execute(select(PortalBranch.company_id, PortalBranch.sort_order))).all())
    assert sort_orders[40] == 0
    assert [sort_orders[company] for company in (2, 5, 7, 9, 3)] == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_the_yandex_pay_editor_follows_the_saved_order(client):
    editor = '/dashboard/payments/yandex_pay'
    before = await client.get(editor, headers=_auth('owner'), params={'month': '2026-08'})
    await client.put(ENDPOINT, json={'company_ids': [2, 5, 7, 9, 3]}, headers=_auth('owner'))
    after = await client.get(editor, headers=_auth('owner'), params={'month': '2026-08'})

    assert [row['company_id'] for row in before.json()['data']['rows']] == DEFAULT_ORDER
    assert [row['company_id'] for row in after.json()['data']['rows']] == [2, 5, 7, 9, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize('company_ids', [list(range(1001)), ['x'], [1.5], 'not-a-list'])
async def test_a_malformed_or_oversized_list_is_refused_by_the_schema(client, company_ids):
    response = await client.put(ENDPOINT, json={'company_ids': company_ids}, headers=_auth('owner'))

    assert response.status_code == 422
