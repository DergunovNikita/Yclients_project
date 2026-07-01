"""Portal auth and branch access control tests."""

from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import api
from api import app
from auth_service import create_access_token, hash_password
from models import (
    Company,
    Group,
    PortalAccount,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    Staff,
    SyncJob,
    YClientsCredential,
    YClientsCredentialCompany,
)


@pytest_asyncio.fixture
async def auth_db(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Branch 1', group_id=1))
    async_session.add(Company(id=2, title='Branch 2', group_id=1))
    async_session.add(PortalAccount(id=1, label='Default tenant', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(PortalBranch(portal_account_id=1, company_id=2))
    await async_session.flush()

    admin = PortalUser(
        id=1,
        portal_account_id=1,
        email='admin@example.com',
        password_hash=hash_password('Admin12345!'),
        full_name='Admin',
        role='owner',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    manager = PortalUser(
        id=2,
        portal_account_id=1,
        email='manager@example.com',
        password_hash=hash_password('Manager12345!'),
        full_name='Manager',
        role='manager',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    async_session.add_all([admin, manager])
    async_session.add(PortalUserBranch(user_id=2, company_id=1))
    branch_admin = PortalUser(
        id=3,
        portal_account_id=1,
        email='branch@example.com',
        password_hash=hash_password('Branch12345!'),
        full_name='Branch Admin',
        role='branch_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    async_session.add(branch_admin)
    async_session.add(PortalUserBranch(user_id=3, company_id=1))
    await async_session.commit()
    return async_session


@pytest.mark.asyncio
async def test_login_and_me(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        login = await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'Admin12345!'})
        assert login.status_code == 200
        token = login.json()['data']['access_token']
        me = await client.get('/auth/me', headers={'Authorization': f'Bearer {token}'})
        assert me.status_code == 200
        assert me.json()['data']['role'] == 'owner'

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_platform_admin_me_without_selected_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    platform_admin = PortalUser(
        id=50,
        portal_account_id=None,
        email='platform@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Platform Admin',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(platform_admin)
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(50, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        me = await client.get('/auth/me', headers={'Authorization': f'Bearer {token}'})

    app.dependency_overrides.clear()

    assert me.status_code == 200
    assert me.json()['data']['role'] == 'platform_admin'
    assert me.json()['data']['portal_account_id'] is None


@pytest.mark.asyncio
async def test_platform_admin_lists_portal_accounts(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    platform_admin = PortalUser(
        id=51,
        portal_account_id=None,
        email='platform.list@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Platform Admin',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(platform_admin)
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(51, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get(
            '/auth/admin/portal-accounts',
            headers={'Authorization': f'Bearer {token}'},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()['data'] == [
        {
            'id': 1,
            'label': 'Default tenant',
            'created_at': response.json()['data'][0]['created_at'],
            'branch_count': 2,
        }
    ]


@pytest.mark.asyncio
async def test_platform_admin_filters_yclients_credentials_by_selected_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Second tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=52,
            portal_account_id=None,
            email='platform.credentials@example.com',
            password_hash=hash_password('Platform12345!'),
            full_name='Platform Admin',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add_all([
        YClientsCredential(
            id=1,
            portal_account_id=1,
            title='Tenant A Credentials',
            partner_token_encrypted='token-a',
            login_encrypted='login-a',
            password_encrypted='password-a',
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ),
        YClientsCredential(
            id=2,
            portal_account_id=2,
            title='Tenant B Credentials',
            partner_token_encrypted='token-b',
            login_encrypted='login-b',
            password_encrypted='password-b',
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        ),
        YClientsCredentialCompany(credential_id=1, company_id=1),
        YClientsCredentialCompany(credential_id=2, company_id=3),
    ])
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(52, 'platform_admin')
    headers = {'Authorization': f'Bearer {token}'}
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        all_credentials = await client.get('/auth/admin/yclients-credentials', headers=headers)
        tenant_b_credentials = await client.get(
            '/auth/admin/yclients-credentials',
            headers={**headers, 'X-Portal-Account-Id': '2'},
        )

    app.dependency_overrides.clear()

    assert all_credentials.status_code == 400
    assert all_credentials.json()['detail'] == 'portal_account_id is required'
    assert tenant_b_credentials.status_code == 200
    assert [item['title'] for item in tenant_b_credentials.json()['data']] == ['Tenant B Credentials']


@pytest.mark.asyncio
async def test_platform_admin_move_yclients_credentials_requires_company_ids(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Second tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=53,
            portal_account_id=None,
            email='platform.move.required@example.com',
            password_hash=hash_password('Platform12345!'),
            full_name='Platform Admin',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(
        YClientsCredential(
            id=3,
            portal_account_id=1,
            title='Tenant A Credentials',
            partner_token_encrypted='token-a',
            login_encrypted='login-a',
            password_encrypted='password-a',
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    auth_db.add(YClientsCredentialCompany(credential_id=3, company_id=1))
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(53, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.patch(
            '/auth/admin/yclients-credentials/3',
            headers={
                'Authorization': f'Bearer {token}',
                'X-Portal-Account-Id': '1',
            },
            json={'portal_account_id': 2, 'is_active': True},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert 'company_ids' in response.json()['detail']
    credential = await auth_db.get(YClientsCredential, 3)
    assert credential.portal_account_id == 1
    links = (await auth_db.execute(select(YClientsCredentialCompany.company_id))).scalars().all()
    assert links == [1]


@pytest.mark.asyncio
async def test_platform_admin_move_yclients_credentials_updates_company_bindings(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Second tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=54,
            portal_account_id=None,
            email='platform.move.valid@example.com',
            password_hash=hash_password('Platform12345!'),
            full_name='Platform Admin',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(
        YClientsCredential(
            id=4,
            portal_account_id=1,
            title='Tenant A Credentials',
            partner_token_encrypted='token-a',
            login_encrypted='login-a',
            password_encrypted='password-a',
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    auth_db.add(YClientsCredentialCompany(credential_id=4, company_id=1))
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(54, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.patch(
            '/auth/admin/yclients-credentials/4',
            headers={
                'Authorization': f'Bearer {token}',
                'X-Portal-Account-Id': '1',
            },
            json={'portal_account_id': 2, 'company_ids': [3], 'is_active': True},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    credential = await auth_db.get(YClientsCredential, 4)
    assert credential.portal_account_id == 2
    links = (await auth_db.execute(select(YClientsCredentialCompany.company_id))).scalars().all()
    assert links == [3]


@pytest.mark.asyncio
async def test_platform_admin_user_management_is_scoped_to_selected_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Second tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=53,
            portal_account_id=None,
            email='platform.users@example.com',
            password_hash=hash_password('Platform12345!'),
            full_name='Platform Admin',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(
        PortalUser(
            id=54,
            portal_account_id=2,
            email='tenant-b@example.com',
            password_hash=hash_password('TenantB12345!'),
            full_name='Tenant B',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            initial_password='TenantB12345!',
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(PortalUserBranch(user_id=54, company_id=3))
    auth_db.add(Staff(id=9101, name='Tenant A Staff', email='staff-a@example.com', company_id=1, fired=0))
    auth_db.add(Staff(id=9102, name='Tenant B Staff', email='staff-b@example.com', company_id=3, fired=0))
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(53, 'platform_admin')
    headers = {'Authorization': f'Bearer {token}'}
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        no_tenant = await client.get('/auth/admin/users', headers=headers)
        tenant_a = await client.get('/auth/admin/users', headers={**headers, 'X-Portal-Account-Id': '1'})
        tenant_b = await client.get('/auth/admin/users', headers={**headers, 'X-Portal-Account-Id': '2'})
        tenant_b_passwords = await client.get(
            '/auth/admin/initial-passwords',
            headers={**headers, 'X-Portal-Account-Id': '2'},
        )

    app.dependency_overrides.clear()

    assert no_tenant.status_code == 400
    assert no_tenant.json()['detail'] == 'X-Portal-Account-Id is required'
    assert tenant_a.status_code == 200
    assert tenant_b.status_code == 200
    tenant_a_emails = {item['email'] for item in tenant_a.json()['data']}
    tenant_b_emails = {item['email'] for item in tenant_b.json()['data']}
    assert 'admin@example.com' in tenant_a_emails
    assert 'staff-a@example.com' in tenant_a_emails
    assert 'tenant-b@example.com' not in tenant_a_emails
    assert 'tenant-b@example.com' in tenant_b_emails
    assert 'staff-b@example.com' in tenant_b_emails
    assert 'admin@example.com' not in tenant_b_emails
    assert tenant_b_passwords.status_code == 200
    assert [row['email'] for row in tenant_b_passwords.json()['data']] == ['tenant-b@example.com']


@pytest.mark.asyncio
async def test_platform_admin_creates_tenant_users_in_selected_existing_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    platform_admin = PortalUser(
        id=53,
        portal_account_id=None,
        email='platform.create@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Platform Admin',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(platform_admin)
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(53, 'platform_admin')
    headers = {'Authorization': f'Bearer {token}'}
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        owner = await client.post(
            '/auth/admin/users',
            headers=headers,
            json={
                'email': 'network.owner@example.com',
                'password': 'Owner12345!',
                'full_name': 'Network Owner',
                'role': 'owner',
                'portal_account_id': 1,
                'company_ids': [],
            },
        )
        manager = await client.post(
            '/auth/admin/users',
            headers=headers,
            json={
                'email': 'network.manager@example.com',
                'password': 'Manager12345!',
                'full_name': 'Network Manager',
                'role': 'manager',
                'portal_account_id': 1,
                'company_ids': [1],
            },
        )

    app.dependency_overrides.clear()

    assert owner.status_code == 200
    assert owner.json()['data']['role'] == 'owner'
    assert owner.json()['data']['portal_account_id'] == 1
    assert owner.json()['data']['company_ids'] == [1, 2]
    assert manager.status_code == 200
    assert manager.json()['data']['role'] == 'manager'
    assert manager.json()['data']['portal_account_id'] == 1
    assert manager.json()['data']['company_ids'] == [1]

    account_ids = (await auth_db.execute(select(PortalAccount.id).order_by(PortalAccount.id))).scalars().all()
    assert account_ids == [1]


@pytest.mark.asyncio
async def test_dashboard_auth_alias_login(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        login = await client.post(
            '/dashboard/auth/login',
            json={'email': 'admin@example.com', 'password': 'Admin12345!'},
        )
        assert login.status_code == 200
        assert login.json()['data']['user']['role'] == 'owner'

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_user_cannot_access_other_branch(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(2, 'manager')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        allowed = await client.get(
            '/dashboard/branches',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert allowed.status_code == 200
        assert [item['id'] for item in allowed.json()['data']] == [1]

        forbidden = await client.get(
            '/dashboard/widget/summary',
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 2},
            headers={'Authorization': f'Bearer {token}'},
        )
        assert forbidden.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_onboarding_branches_queue_full_sync(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    token = create_access_token(1, 'owner')

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            self.partner_token = partner_token
            self.login = login
            self.password = password

        def authenticate(self):
            return self.partner_token == 'partner' and self.login == 'login' and self.password == 'password'

        def get_groups(self):
            return [
                {
                    'id': 1,
                    'title': 'G1',
                    'companies': [
                        {'id': 1, 'title': 'Branch 1'},
                        {'id': 2, 'title': 'Branch 2'},
                    ],
                }
            ]

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        credentials = await client.post(
            '/onboarding/credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'partner_token': 'partner', 'login': 'login', 'password': 'password'},
        )
        assert credentials.status_code == 200
        credential_id = credentials.json()['data']['credential_id']

        branches = await client.post(
            '/onboarding/branches',
            headers={'Authorization': f'Bearer {token}'},
            json={'credential_id': credential_id, 'company_ids': [1, 2]},
        )

    app.dependency_overrides.clear()

    assert branches.status_code == 200
    data = branches.json()['data']
    assert data['source_type'] == 'yclients'
    assert data['company_ids'] == [1, 2]
    assert data['sync_status'] == 'queued'
    assert data['sync_job_id']

    job = await auth_db.get(SyncJob, data['sync_job_id'])
    assert job.mode == 'full'
    assert job.initiator == 'onboarding'
    assert job.portal_account_id == 1
    assert job.credential_id == credential_id
    assert job.company_ids == [1, 2]

    links = (await auth_db.execute(select(YClientsCredentialCompany.company_id))).scalars().all()
    assert sorted(links) == [1, 2]


@pytest.mark.asyncio
async def test_onboarding_credentials_rejects_branch_owned_by_other_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Other tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(PortalUser(
        id=60,
        portal_account_id=2,
        email='other-owner@example.com',
        password_hash=hash_password('Owner12345!'),
        full_name='Other Owner',
        role='owner',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    ))
    await auth_db.commit()
    token = create_access_token(1, 'owner')

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            pass

        def authenticate(self):
            return True

        def get_groups(self):
            return [{'id': 1, 'title': 'G1', 'companies': [{'id': 3, 'title': 'Branch 3'}]}]

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/onboarding/credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'partner_token': 'partner', 'login': 'login', 'password': 'password'},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_onboarding_claims_branch_from_admin_only_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Old platform tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(PortalUser(
        id=61,
        portal_account_id=2,
        email='old-platform@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Old Platform Admin',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    ))
    await auth_db.commit()
    token = create_access_token(1, 'owner')

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            pass

        def authenticate(self):
            return True

        def get_groups(self):
            return [{'id': 1, 'title': 'G1', 'companies': [{'id': 3, 'title': 'Branch 3'}]}]

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        credentials = await client.post(
            '/onboarding/credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'partner_token': 'partner', 'login': 'login', 'password': 'password'},
        )
        assert credentials.status_code == 200
        credential_id = credentials.json()['data']['credential_id']

        branches = await client.post(
            '/onboarding/branches',
            headers={'Authorization': f'Bearer {token}'},
            json={'credential_id': credential_id, 'company_ids': [3]},
        )

    app.dependency_overrides.clear()

    assert branches.status_code == 200
    branch = (
        await auth_db.execute(select(PortalBranch).where(PortalBranch.company_id == 3))
    ).scalar_one()
    assert branch.portal_account_id == 1
    link = (
        await auth_db.execute(select(YClientsCredentialCompany).where(YClientsCredentialCompany.company_id == 3))
    ).scalar_one()
    assert link.credential_id == credential_id


@pytest.mark.asyncio
async def test_onboarding_rejects_claim_from_admin_only_tenant_with_active_credential(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Prepared tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=62,
            portal_account_id=2,
            email='old-platform-active@example.com',
            password_hash=hash_password('Platform12345!'),
            full_name='Old Platform Admin',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(
        YClientsCredential(
            id=5,
            portal_account_id=2,
            title='Prepared credential',
            partner_token_encrypted='token',
            login_encrypted='login',
            password_encrypted='password',
            is_active=True,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
    )
    auth_db.add(YClientsCredentialCompany(credential_id=5, company_id=3))
    await auth_db.commit()
    token = create_access_token(1, 'owner')

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            pass

        def authenticate(self):
            return True

        def get_groups(self):
            return [{'id': 1, 'title': 'G1', 'companies': [{'id': 3, 'title': 'Branch 3'}]}]

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/onboarding/credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'partner_token': 'partner', 'login': 'login', 'password': 'password'},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 409


@pytest.mark.asyncio
async def test_register_allows_login_without_email_verification(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/register',
            json={'email': 'newuser@example.com', 'password': 'NewUser123!', 'full_name': 'New'},
        )
        assert created.status_code == 200
        login = await client.post('/auth/login', json={'email': 'newuser@example.com', 'password': 'NewUser123!'})
        assert login.status_code == 200

    created_user = (
        await auth_db.execute(select(PortalUser).where(PortalUser.email == 'newuser@example.com'))
    ).scalar_one()
    assert created_user.role == 'owner'
    assert created_user.email_verified_at is not None
    assert created_user.portal_account_id is not None
    created_account = await auth_db.get(PortalAccount, created_user.portal_account_id)
    assert created_account is not None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_creates_manager(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'newmanager@example.com',
                'password': 'Manager12345!',
                'full_name': 'New Manager',
                'role': 'manager',
                'company_ids': [1],
            },
        )
        assert created.status_code == 200
        data = created.json()['data']
        assert data['email'] == 'newmanager@example.com'
        assert data['role'] == 'manager'
        assert data['company_ids'] == [1]
        assert data['email_verified'] is True

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_updates_user_email_login(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        updated = await client.patch(
            '/auth/admin/users/2',
            headers={'Authorization': f'Bearer {token}'},
            json={'email': 'New.Manager@Example.COM', 'full_name': 'Manager Renamed'},
        )
        assert updated.status_code == 200
        assert updated.json()['data']['email'] == 'new.manager@example.com'
        assert updated.json()['data']['email_verified'] is True

        old_login = await client.post(
            '/auth/login',
            json={'email': 'manager@example.com', 'password': 'Manager12345!'},
        )
        assert old_login.status_code == 401

        new_login = await client.post(
            '/auth/login',
            json={'email': 'new.manager@example.com', 'password': 'Manager12345!'},
        )
        assert new_login.status_code == 200

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_update_user_email_rejects_duplicate(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        updated = await client.patch(
            '/auth/admin/users/2',
            headers={'Authorization': f'Bearer {token}'},
            json={'email': 'branch@example.com'},
        )
        assert updated.status_code == 409

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_admin_creates_viewer_in_own_branch(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(3, 'branch_admin')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'viewer@example.com',
                'password': 'Viewer12345!',
                'role': 'viewer',
                'company_ids': [1],
            },
        )
        assert created.status_code == 200
        assert created.json()['data']['role'] == 'viewer'

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_admin_cannot_create_peer_role(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(3, 'branch_admin')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'peer@example.com',
                'password': 'Branch12345!',
                'role': 'branch_admin',
                'company_ids': [1],
            },
        )
        assert created.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_admin_cannot_create_user_in_foreign_branch(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(3, 'branch_admin')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'foreign@example.com',
                'password': 'Viewer12345!',
                'role': 'viewer',
                'company_ids': [2],
            },
        )
        assert created.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_lists_same_rank_and_lower(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        payload = await client.get('/auth/admin/users', headers={'Authorization': f'Bearer {token}'})
        assert payload.status_code == 200
        data = payload.json()['data']
        emails = {item['email'] for item in data}
        assert 'admin@example.com' in emails
        assert 'manager@example.com' in emails
        assert 'branch@example.com' in emails

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_manager_cannot_create_or_update_users(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(2, 'manager')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'blocked@example.com',
                'password': 'Viewer12345!',
                'role': 'viewer',
                'company_ids': [1],
            },
        )
        assert created.status_code == 403

        updated = await client.patch(
            '/auth/admin/users/3',
            headers={'Authorization': f'Bearer {token}'},
            json={'full_name': 'Blocked Update'},
        )
        assert updated.status_code == 403

        listed = await client.get('/auth/admin/users', headers={'Authorization': f'Bearer {token}'})
        assert listed.status_code == 200
        portal_emails = [
            item.get('email')
            for item in listed.json()['data']
            if item.get('is_portal_user')
        ]
        assert 'admin@example.com' not in portal_emails
        assert 'branch@example.com' not in portal_emails
        self_row = next(
            (item for item in listed.json()['data'] if item.get('email') == 'manager@example.com'),
            None,
        )
        assert self_row is not None
        assert self_row['manageable'] is False

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_manages_yclients_credentials(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    token = create_access_token(1, 'owner')

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            self.partner_token = partner_token
            self.login = login
            self.password = password

        def authenticate(self):
            return self.partner_token == 'partner' and self.login == 'login' and self.password == 'password'

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/yclients-credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'title': 'Main credential',
                'partner_token': 'partner',
                'login': 'login',
                'password': 'password',
                'company_ids': [1],
            },
        )
        assert created.status_code == 200
        data = created.json()['data']
        assert data['title'] == 'Main credential'
        assert data['company_ids'] == [1]
        assert data['has_partner_token'] is True
        assert data['has_login'] is True
        assert data['has_password'] is True
        assert 'partner_token' not in data
        assert 'password' not in data

        checked = await client.post(
            f"/auth/admin/yclients-credentials/{data['id']}/test",
            headers={'Authorization': f'Bearer {token}'},
        )
        assert checked.status_code == 200

        updated = await client.patch(
            f"/auth/admin/yclients-credentials/{data['id']}",
            headers={'Authorization': f'Bearer {token}'},
            json={'title': 'Updated credential', 'company_ids': [2], 'is_active': True},
        )
        assert updated.status_code == 200
        updated_row = next(item for item in updated.json()['data'] if item['id'] == data['id'])
        assert updated_row['title'] == 'Updated credential'
        assert updated_row['company_ids'] == [2]

        checked_after_update = await client.post(
            f"/auth/admin/yclients-credentials/{data['id']}/test",
            headers={'Authorization': f'Bearer {token}'},
        )
        assert checked_after_update.status_code == 200

        listed = await client.get('/auth/admin/yclients-credentials', headers={'Authorization': f'Bearer {token}'})
        assert listed.status_code == 200
        assert listed.json()['data'][0]['company_ids'] == [2]

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_admin_cannot_manage_yclients_credentials(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(3, 'branch_admin')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        listed = await client.get('/auth/admin/yclients-credentials', headers={'Authorization': f'Bearer {token}'})
        assert listed.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_deletes_manager(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        deleted = await client.delete('/auth/admin/users/2', headers={'Authorization': f'Bearer {token}'})
        assert deleted.status_code == 200
        listed = await client.get('/auth/admin/users', headers={'Authorization': f'Bearer {token}'})
        emails = [item['email'] for item in listed.json()['data']]
        assert 'manager@example.com' not in emails

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_created_manager_appears_in_dashboard_staff(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'worker@example.com',
                'password': 'Worker12345!',
                'full_name': 'Worker One',
                'role': 'manager',
                'company_ids': [1],
            },
        )
        assert created.status_code == 200
        staff = await client.get('/dashboard/staff', headers={'Authorization': f'Bearer {token}'})
        assert staff.status_code == 200
        names = [row['name'] for row in staff.json()['data']]
        assert 'Worker One' in names

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_admin_appears_in_dashboard_staff(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'branchboss@example.com',
                'password': 'Branch12345!',
                'full_name': 'Branch Boss',
                'role': 'branch_admin',
                'company_ids': [1],
            },
        )
        assert created.status_code == 200
        staff = await client.get('/dashboard/staff', headers={'Authorization': f'Bearer {token}'})
        assert staff.status_code == 200
        names = [row['name'] for row in staff.json()['data']]
        assert 'Branch Boss' in names

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_updates_unlinked_staff(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        Staff(
            id=9001,
            name='Demo Worker',
            position='master',
            company_id=1,
            fired=0,
            bookable=True,
        )
    )
    await auth_db.commit()
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        updated = await client.patch(
            '/auth/admin/staff/9001',
            headers={'Authorization': f'Bearer {token}'},
            json={'full_name': 'Updated Worker', 'company_id': 2, 'position': 'senior'},
        )
        assert updated.status_code == 200
        assert updated.json()['data']['full_name'] == 'Updated Worker'
        assert updated.json()['data']['company_ids'] == [2]

        listed = await client.get('/auth/admin/users', headers={'Authorization': f'Bearer {token}'})
        staff_rows = [item for item in listed.json()['data'] if item.get('staff_id') == 9001]
        assert staff_rows
        assert staff_rows[0]['full_name'] == 'Updated Worker'
        assert staff_rows[0]['manageable'] is True

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_deletes_unlinked_staff(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        Staff(
            id=9002,
            name='Remove Me',
            position='master',
            company_id=1,
            fired=0,
            bookable=True,
        )
    )
    await auth_db.commit()
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        deleted = await client.delete('/auth/admin/staff/9002', headers={'Authorization': f'Bearer {token}'})
        assert deleted.status_code == 200
        listed = await client.get('/auth/admin/users', headers={'Authorization': f'Bearer {token}'})
        staff_ids = [item.get('staff_id') for item in listed.json()['data']]
        assert 9002 not in staff_ids

        staff = await client.get('/dashboard/staff', headers={'Authorization': f'Bearer {token}'})
        names = [row['name'] for row in staff.json()['data']]
        assert 'Remove Me' not in names

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_provision_staff_account(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        Staff(
            id=9003,
            name='No Account Worker',
            position='master',
            company_id=1,
            fired=0,
            bookable=True,
        )
    )
    await auth_db.commit()
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/staff/9003/create-account',
            headers={'Authorization': f'Bearer {token}'},
            json={'role': 'viewer'},
        )
        assert created.status_code == 409
        assert 'Real email is required' in created.json()['detail']

        created = await client.post(
            '/auth/admin/staff/9003/create-account',
            headers={'Authorization': f'Bearer {token}'},
            json={'role': 'viewer', 'email': 'worker9003@example.com'},
        )
        assert created.status_code == 200
        data = created.json()['data']
        assert data['email'] == 'worker9003@example.com'
        assert data['user_id'] == 9003
        assert data['staff_id'] == 9003
        assert len(data['initial_password']) >= 8

        login = await client.post(
            '/auth/login',
            json={'email': data['email'], 'password': data['initial_password']},
        )
        assert login.status_code == 200

        passwords = await client.get(
            '/auth/admin/initial-passwords',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert passwords.status_code == 200
        emails = {row['email'] for row in passwords.json()['data']}
        assert data['email'] in emails

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_change_password_clears_initial_password(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(2, 'manager')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        user = (await auth_db.execute(select(PortalUser).where(PortalUser.id == 2))).scalar_one()
        user.initial_password = 'Manager12345!'
        await auth_db.commit()

        changed = await client.post(
            '/auth/change-password',
            headers={'Authorization': f'Bearer {token}'},
            json={'current_password': 'Manager12345!', 'new_password': 'NewManager123!'},
        )
        assert changed.status_code == 200

        await auth_db.refresh(user)
        assert user.initial_password is None
        assert user.password_changed_at is not None

        login_old = await client.post(
            '/auth/login',
            json={'email': 'manager@example.com', 'password': 'Manager12345!'},
        )
        assert login_old.status_code == 401

        login_new = await client.post(
            '/auth/login',
            json={'email': 'manager@example.com', 'password': 'NewManager123!'},
        )
        assert login_new.status_code == 200

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_admin_sees_branch_initial_passwords_only(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        Staff(
            id=9004,
            name='Branch Worker',
            email='branch.worker@example.com',
            position='master',
            company_id=1,
            fired=0,
            bookable=True,
        )
    )
    auth_db.add(
        Staff(
            id=9005,
            name='Other Branch Worker',
            email='other.branch.worker@example.com',
            position='master',
            company_id=2,
            fired=0,
            bookable=True,
        )
    )
    await auth_db.commit()
    super_token = create_access_token(1, 'owner')
    branch_token = create_access_token(3, 'branch_admin')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        await client.post(
            '/auth/admin/provision-accounts',
            headers={'Authorization': f'Bearer {super_token}'},
        )

        branch_passwords = await client.get(
            '/auth/admin/initial-passwords',
            headers={'Authorization': f'Bearer {branch_token}'},
        )
        assert branch_passwords.status_code == 200
        branch_emails = {row['email'] for row in branch_passwords.json()['data']}
        assert 'branch.worker@example.com' in branch_emails
        assert 'other.branch.worker@example.com' not in branch_emails

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_mass_provision_requires_real_staff_email(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        Staff(
            id=9006,
            name='Worker With Email',
            email='worker.with.email@example.com',
            position='master',
            company_id=1,
            fired=0,
            bookable=True,
        )
    )
    auth_db.add(
        Staff(
            id=9007,
            name='Worker Without Email',
            position='master',
            company_id=1,
            fired=0,
            bookable=True,
        )
    )
    await auth_db.commit()
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/auth/admin/provision-accounts',
            headers={'Authorization': f'Bearer {token}'},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['created_count'] == 1
    assert data['created'][0]['email'] == 'worker.with.email@example.com'
    assert any('Real email is required for staff 9007' in error for error in data['errors'])


@pytest.mark.asyncio
async def test_distribute_credentials_sends_real_email_only(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    sent = []

    def _capture_send(user, password):
        sent.append((user.email, password))

    monkeypatch.setattr('auth_routes.send_account_credentials_email', _capture_send)

    auth_db.add(
        PortalUser(
            id=10,
            portal_account_id=1,
            email='real.user@example.com',
            password_hash=hash_password('RealUser123!'),
            full_name='Real User',
            role='viewer',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            initial_password='RealUser123!',
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(PortalUserBranch(user_id=10, company_id=1))
    auth_db.add(
        PortalUser(
            id=11,
            portal_account_id=1,
            email='fake.worker.99@portal.local',
            password_hash=hash_password('FakeWorker123!'),
            full_name='Fake Worker',
            role='viewer',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            initial_password='FakeWorker123!',
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(PortalUserBranch(user_id=11, company_id=1))
    await auth_db.commit()

    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/auth/admin/distribute-credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'user_ids': [10, 11]},
        )
        assert response.status_code == 200
        data = response.json()['data']
        assert data['sent_count'] == 1
        assert len(data['skipped']) == 1
        assert sent == [('real.user@example.com', 'RealUser123!')]

    app.dependency_overrides.clear()
