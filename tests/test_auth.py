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
async def test_register_requires_verification_before_login(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    async def _noop_send(_db, _user):
        return None

    monkeypatch.setattr('auth_service.send_verification_email', _noop_send)

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
        assert login.status_code == 403

    created_user = (
        await auth_db.execute(select(PortalUser).where(PortalUser.email == 'newuser@example.com'))
    ).scalar_one()
    assert created_user.role == 'owner'
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

    monkeypatch.setattr('auth_routes.YClientsAPI', FakeYClientsAPI)

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
        assert created.status_code == 200
        data = created.json()['data']
        assert data['email'].endswith('@portal.local')
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
        assert any('9004' in email for email in branch_emails)
        assert not any('9005' in email for email in branch_emails)

    app.dependency_overrides.clear()


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
