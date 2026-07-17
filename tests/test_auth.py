"""Portal auth and branch access control tests."""

import json
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

import api
import auth_routes
from api import app
from auth_sessions import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME
from auth_service import TOKEN_PURPOSE_RESET, create_access_token, create_email_token, hash_password
from config import AUTH_CSRF_COOKIE_NAME
from yclients_credentials import new_credential
from models import (
    Company,
    Group,
    PortalAccount,
    PortalAuditEvent,
    PortalEmailToken,
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
        payload = login.json()
        assert 'access_token' not in payload['data']
        assert 'token_type' not in payload['data']
        assert client.cookies.get(ACCESS_COOKIE_NAME)
        assert client.cookies.get(REFRESH_COOKIE_NAME)
        assert client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        set_cookie_headers = login.headers.get_list('set-cookie')
        assert any(f'{ACCESS_COOKIE_NAME}=' in item and 'HttpOnly' in item for item in set_cookie_headers)
        assert any(f'{REFRESH_COOKIE_NAME}=' in item and 'HttpOnly' in item for item in set_cookie_headers)
        assert any(f'{AUTH_CSRF_COOKIE_NAME}=' in item and 'HttpOnly' not in item for item in set_cookie_headers)

        me = await client.get('/auth/me')
        assert me.status_code == 200
        assert me.json()['data']['role'] == 'owner'

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_refresh_keeps_cookie_session_without_returning_access_token(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        login = await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'Admin12345!'})
        assert login.status_code == 200
        missing_csrf = await client.post('/auth/refresh')
        bearer_without_csrf = await client.post(
            '/auth/refresh',
            headers={'Authorization': 'Bearer not-a-valid-session-choice'},
        )
        csrf = client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        refresh = await client.post('/auth/refresh', headers={'X-CSRF-Token': csrf})

    app.dependency_overrides.clear()

    assert missing_csrf.status_code == 403
    assert bearer_without_csrf.status_code == 403
    assert refresh.status_code == 200
    payload = refresh.json()
    assert 'access_token' not in payload['data']
    assert 'token_type' not in payload['data']
    assert payload['data']['user']['email'] == 'admin@example.com'
    set_cookie_headers = refresh.headers.get_list('set-cookie')
    assert any(f'{ACCESS_COOKIE_NAME}=' in item and 'HttpOnly' in item for item in set_cookie_headers)
    assert any(f'{REFRESH_COOKIE_NAME}=' in item and 'HttpOnly' in item for item in set_cookie_headers)
    assert any(f'{AUTH_CSRF_COOKIE_NAME}=' in item and 'HttpOnly' not in item for item in set_cookie_headers)


@pytest.mark.asyncio
async def test_logout_requires_csrf_for_cookie_session(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        login = await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'Admin12345!'})
        assert login.status_code == 200

        missing_csrf = await client.post('/auth/logout')
        assert client.cookies.get(ACCESS_COOKIE_NAME)
        assert client.cookies.get(REFRESH_COOKIE_NAME)
        bearer_without_csrf = await client.post(
            '/auth/logout',
            headers={'Authorization': 'Bearer not-a-valid-session-choice'},
        )
        assert client.cookies.get(ACCESS_COOKIE_NAME)
        assert client.cookies.get(REFRESH_COOKIE_NAME)

        csrf = client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        logout = await client.post('/auth/logout', headers={'X-CSRF-Token': csrf})

    app.dependency_overrides.clear()

    assert missing_csrf.status_code == 403
    assert bearer_without_csrf.status_code == 403
    assert logout.status_code == 200
    assert not client.cookies.get(ACCESS_COOKIE_NAME)
    assert not client.cookies.get(REFRESH_COOKIE_NAME)
    assert not client.cookies.get(AUTH_CSRF_COOKIE_NAME)


@pytest.mark.asyncio
async def test_demo_login_keeps_cookie_session_without_returning_access_token(auth_db):
    demo_user = PortalUser(
        id=80,
        portal_account_id=1,
        email='demo@example.com',
        password_hash=hash_password('Demo12345!'),
        full_name='Demo',
        role='owner',
        is_active=True,
        is_demo=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(demo_user)
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post('/auth/demo-login')

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert 'access_token' not in payload['data']
    assert 'token_type' not in payload['data']
    assert payload['data']['user']['email'] == 'demo@example.com'
    assert client.cookies.get(ACCESS_COOKIE_NAME)
    assert client.cookies.get(REFRESH_COOKIE_NAME)


@pytest.mark.asyncio
async def test_cookie_mutation_requires_csrf_but_bearer_does_not(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as cookie_client:
        login = await cookie_client.post(
            '/auth/login',
            json={'email': 'admin@example.com', 'password': 'Admin12345!'},
        )
        assert login.status_code == 200
        missing_csrf = await cookie_client.post(
            '/auth/change-password',
            json={'current_password': 'Admin12345!', 'new_password': 'Changed12345!'},
        )
        cookie_bearer = create_access_token(1, 'owner')
        bearer_with_cookies_without_csrf = await cookie_client.post(
            '/auth/change-password',
            headers={'Authorization': f'Bearer {cookie_bearer}'},
            json={'current_password': 'Admin12345!', 'new_password': 'CookieBearer12345!'},
        )
        csrf = cookie_client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        with_csrf = await cookie_client.post(
            '/auth/change-password',
            headers={'X-CSRF-Token': csrf},
            json={'current_password': 'Admin12345!', 'new_password': 'Changed12345!'},
        )

    # Fresh database fixture is not available inside the same test, so restore
    # the original password before verifying Bearer behavior.
    admin = (await auth_db.execute(select(PortalUser).where(PortalUser.id == 1))).scalar_one()
    admin.password_hash = hash_password('Admin12345!')
    admin.token_version = 0
    await auth_db.commit()

    token = create_access_token(1, 'owner')
    async with AsyncClient(transport=transport, base_url='http://test') as bearer_client:
        bearer = await bearer_client.post(
            '/auth/change-password',
            headers={'Authorization': f'Bearer {token}'},
            json={'current_password': 'Admin12345!', 'new_password': 'Bearer12345!'},
        )

    app.dependency_overrides.clear()

    assert missing_csrf.status_code == 403
    assert bearer_with_cookies_without_csrf.status_code == 403
    assert with_csrf.status_code == 200
    assert bearer.status_code == 200


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
async def test_platform_admin_yclients_payload_test_requires_and_audits_selected_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    partner_secret = 'platform-partner-secret'
    login_secret = 'platform-login-secret'
    password_secret = 'platform-password-secret'
    platform_admin = PortalUser(
        id=52,
        portal_account_id=None,
        email='platform.credentials@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Platform Credentials',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(platform_admin)
    await auth_db.commit()

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            self.partner_token = partner_token
            self.login = login
            self.password = password

        def authenticate(self):
            return (
                self.partner_token == partner_secret
                and self.login == login_secret
                and self.password == password_secret
            )

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)

    async def override_db():
        yield auth_db

    token = create_access_token(52, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    payload = {
        'partner_token': partner_secret,
        'login': login_secret,
        'password': password_secret,
    }
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        missing_tenant = await client.post(
            '/auth/admin/yclients-credentials/test',
            headers={'Authorization': f'Bearer {token}'},
            json=payload,
        )
        unknown_tenant = await client.post(
            '/auth/admin/yclients-credentials/test',
            headers={'Authorization': f'Bearer {token}', 'X-Portal-Account-Id': '999'},
            json=payload,
        )
        unknown_tenant_saved_test = await client.post(
            '/auth/admin/yclients-credentials/404/test',
            headers={'Authorization': f'Bearer {token}', 'X-Portal-Account-Id': '999'},
        )
        selected_tenant = await client.post(
            '/auth/admin/yclients-credentials/test',
            headers={'Authorization': f'Bearer {token}', 'X-Portal-Account-Id': '1'},
            json=payload,
        )

    app.dependency_overrides.clear()

    assert missing_tenant.status_code == 400
    assert unknown_tenant.status_code == 404
    assert unknown_tenant_saved_test.status_code == 404
    assert selected_tenant.status_code == 200
    for secret in (partner_secret, login_secret, password_secret):
        assert secret not in missing_tenant.text
        assert secret not in unknown_tenant.text
        assert secret not in unknown_tenant_saved_test.text
        assert secret not in selected_tenant.text
    audits = (
        await auth_db.execute(
            select(PortalAuditEvent)
            .where(PortalAuditEvent.action.in_([
                'yclients_credentials.payload_tested',
                'yclients_credentials.tested',
            ]))
            .order_by(PortalAuditEvent.id.asc())
        )
    ).scalars().all()
    assert all(audit.portal_account_id != 999 for audit in audits)
    assert any(
        audit.action == 'yclients_credentials.payload_tested'
        and audit.portal_account_id is None
        and audit.metadata_json == {
            'success': False,
            'error_type': 'tenant_selection_failed',
            'requested_portal_account_id': 999,
        }
        for audit in audits
    )
    assert any(
        audit.action == 'yclients_credentials.tested'
        and audit.portal_account_id is None
        and audit.metadata_json == {
            'success': False,
            'error_type': 'credential_lookup_failed',
            'requested_portal_account_id': 999,
        }
        for audit in audits
    )
    audit = audits[-1]
    assert audit is not None
    assert audit.actor_user_id == 52
    assert audit.portal_account_id == 1
    assert audit.metadata_json == {'source_type': 'yclients', 'success': True}
    metadata_text = json.dumps(audit.metadata_json, sort_keys=True)
    for secret in (partner_secret, login_secret, password_secret):
        assert secret not in metadata_text


@pytest.mark.asyncio
async def test_platform_admin_update_clears_platform_admin_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    actor = PortalUser(
        id=55,
        portal_account_id=None,
        email='platform-cleaner@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Platform Cleaner',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    dirty_admin = PortalUser(
        id=56,
        portal_account_id=1,
        email='dirty-platform@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Dirty Platform',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add_all([actor, dirty_admin])
    auth_db.add(PortalUserBranch(user_id=56, company_id=1))
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(55, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        updated = await client.patch(
            '/auth/admin/users/56',
            headers={'Authorization': f'Bearer {token}'},
            json={'full_name': 'Clean Platform'},
        )

    app.dependency_overrides.clear()

    assert updated.status_code == 200
    await auth_db.refresh(dirty_admin)
    assert dirty_admin.portal_account_id is None
    branch_rows = (
        await auth_db.execute(select(PortalUserBranch).where(PortalUserBranch.user_id == 56))
    ).scalars().all()
    assert branch_rows == []


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
        unknown_tenant_credentials = await client.get(
            '/auth/admin/yclients-credentials',
            headers={**headers, 'X-Portal-Account-Id': '999'},
        )

    app.dependency_overrides.clear()

    assert all_credentials.status_code == 400
    assert all_credentials.json()['detail'] == 'portal_account_id is required'
    assert unknown_tenant_credentials.status_code == 404
    assert unknown_tenant_credentials.json()['detail'] == 'Portal account not found'
    assert tenant_b_credentials.status_code == 200
    assert [item['title'] for item in tenant_b_credentials.json()['data']] == ['Tenant B Credentials']
    audits = (
        await auth_db.execute(
            select(PortalAuditEvent)
            .where(PortalAuditEvent.action == 'yclients_credentials.listed')
            .order_by(PortalAuditEvent.id.asc())
        )
    ).scalars().all()
    assert all(audit.portal_account_id != 999 for audit in audits)
    assert any(audit.metadata_json == {'success': False, 'error_type': 'tenant_selection_failed'} for audit in audits)
    assert any(
        audit.portal_account_id is None
        and audit.metadata_json == {
            'success': False,
            'error_type': 'tenant_selection_failed',
            'requested_portal_account_id': 999,
        }
        for audit in audits
    )
    assert any(audit.portal_account_id == 2 and audit.metadata_json == {'success': True, 'count': 1} for audit in audits)


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
        unknown_tenant = await client.patch(
            '/auth/admin/yclients-credentials/3',
            headers={
                'Authorization': f'Bearer {token}',
                'X-Portal-Account-Id': '1',
            },
            json={'portal_account_id': 999, 'company_ids': [], 'is_active': True},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert 'company_ids' in response.json()['detail']
    assert unknown_tenant.status_code == 404
    assert unknown_tenant.json()['detail'] == 'Portal account not found'
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
async def test_platform_admin_create_yclients_credentials_rejects_unknown_tenant(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    platform_admin = PortalUser(
        id=57,
        portal_account_id=None,
        email='platform.create.credentials@example.com',
        password_hash=hash_password('Platform12345!'),
        full_name='Platform Create Credentials',
        role='platform_admin',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(platform_admin)
    await auth_db.commit()

    async def override_db():
        yield auth_db

    token = create_access_token(57, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/auth/admin/yclients-credentials',
            headers={'Authorization': f'Bearer {token}', 'X-Portal-Account-Id': '999'},
            json={
                'title': 'Unknown tenant',
                'partner_token': 'create-partner-secret',
                'login': 'create-login-secret',
                'password': 'create-password-secret',
                'company_ids': [],
            },
        )

    app.dependency_overrides.clear()

    assert response.status_code == 404
    assert response.json()['detail'] == 'Portal account not found'
    for secret in ('create-partner-secret', 'create-login-secret', 'create-password-secret'):
        assert secret not in response.text


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
    pending_invite_emails = {item['email'] for item in tenant_b_passwords.json()['data']}
    assert 'tenant-b@example.com' in pending_invite_emails
    assert 'admin@example.com' not in pending_invite_emails
    for row in tenant_b_passwords.json()['data']:
        assert row['portal_account_id'] == 2
        assert 'initial_password' not in row
    assert tenant_b_passwords.json()['deprecated'] is True


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
async def test_onboarding_claims_branch_from_admin_only_tenant_with_active_credential(auth_db, monkeypatch):
    """Owner takes over branches from an admin-only tenant even if a platform
    admin previously uploaded YClients credentials for them: the stale
    credential<->company link is detached, and the owner's fresh credential
    becomes the new owner of the company."""
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
    assert link.credential_id != 5


@pytest.mark.asyncio
async def test_register_requires_email_verification_before_login(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr('auth_routes.AUTH_PUBLIC_REGISTRATION_ENABLED', True)
    monkeypatch.setattr('auth_routes.AUTH_EMAIL_VERIFY_REQUIRED', True)
    sent = []

    def _capture_email(to_email, subject, body):
        sent.append((to_email, subject, body))

    monkeypatch.setattr('auth_service.send_auth_email', _capture_email)

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
        assert created.json()['message_key'] == 'register.verifySuccess'
        login = await client.post('/auth/login', json={'email': 'newuser@example.com', 'password': 'NewUser123!'})
        assert login.status_code == 403
        assert login.json()['detail'] == 'Email verification required'

        assert len(sent) == 1
        raw_token = sent[0][2].split('token=', 1)[1].split()[0]
        verified = await client.post('/auth/verify-email', json={'token': raw_token})
        assert verified.status_code == 200
        login_after_verify = await client.post(
            '/auth/login',
            json={'email': 'newuser@example.com', 'password': 'NewUser123!'},
        )
        assert login_after_verify.status_code == 200

    created_user = (
        await auth_db.execute(select(PortalUser).where(PortalUser.email == 'newuser@example.com'))
    ).scalar_one()
    assert created_user.role == 'owner'
    await auth_db.refresh(created_user)
    assert created_user.email_verified_at is not None
    assert created_user.portal_account_id is None

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_register_email_failure_rolls_back_unverified_account(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr('auth_routes.AUTH_PUBLIC_REGISTRATION_ENABLED', True)
    monkeypatch.setattr('auth_routes.AUTH_EMAIL_VERIFY_REQUIRED', True)

    def _fail_email(*_args, **_kwargs):
        raise RuntimeError('smtp unavailable')

    monkeypatch.setattr('auth_service.send_auth_email', _fail_email)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        failed = await client.post(
            '/auth/register',
            json={'email': 'mailfail@example.com', 'password': 'MailFail123!', 'full_name': 'Mail Fail'},
        )
        retry = await client.post(
            '/auth/register',
            json={'email': 'mailfail@example.com', 'password': 'MailFail123!', 'full_name': 'Mail Fail'},
        )

    app.dependency_overrides.clear()

    assert failed.status_code == 503
    assert failed.json()['detail'] == 'Email delivery failed'
    assert retry.status_code == 503
    assert await auth_db.scalar(
        select(func.count()).select_from(PortalUser).where(PortalUser.email == 'mailfail@example.com')
    ) == 0
    assert await auth_db.scalar(select(func.count()).select_from(PortalEmailToken)) == 0


@pytest.mark.asyncio
async def test_register_is_blocked_when_public_registration_disabled(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr('auth_routes.AUTH_PUBLIC_REGISTRATION_ENABLED', False)
    before_count = await auth_db.scalar(select(func.count()).select_from(PortalUser))

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        new_email = await client.post(
            '/auth/register',
            json={'email': 'blocked@example.com', 'password': 'Blocked123!', 'full_name': 'Blocked'},
        )
        duplicate_email = await client.post(
            '/auth/register',
            json={'email': 'admin@example.com', 'password': 'Blocked123!', 'full_name': 'Blocked'},
        )
        dashboard_prefix = await client.post(
            '/dashboard/auth/register',
            json={'email': 'blocked2@example.com', 'password': 'Blocked123!', 'full_name': 'Blocked'},
        )

    app.dependency_overrides.clear()
    after_count = await auth_db.scalar(select(func.count()).select_from(PortalUser))

    assert new_email.status_code == 403
    assert duplicate_email.status_code == 403
    assert dashboard_prefix.status_code == 403
    assert new_email.json()['detail'] == 'Public registration is disabled'
    assert duplicate_email.json()['detail'] == 'Public registration is disabled'
    assert after_count == before_count


@pytest.mark.asyncio
async def test_login_rate_limit_is_scoped_by_email_and_ip(auth_db, monkeypatch):
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_MAX_REQUESTS', 2)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_IP_MAX_REQUESTS', 10)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db

    async def post_login(ip: str, email: str, path: str = '/auth/login'):
        transport = ASGITransport(app=app, client=(ip, 12345))
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            return await client.post(path, json={'email': email, 'password': 'wrong'})

    first = await post_login('203.0.113.10', 'ADMIN@example.com')
    second = await post_login('203.0.113.10', ' admin@example.com ', '/dashboard/auth/login')
    limited = await post_login('203.0.113.10', 'admin@example.com')
    different_email = await post_login('203.0.113.10', 'other@example.com')
    different_ip = await post_login('203.0.113.11', 'admin@example.com')

    app.dependency_overrides.clear()

    assert first.status_code == 401
    assert second.status_code == 401
    assert limited.status_code == 429
    assert limited.headers['retry-after']
    assert different_email.status_code == 401
    assert different_ip.status_code == 401


@pytest.mark.asyncio
async def test_login_rate_limit_ignores_spoofed_forwarded_for_by_default(auth_db, monkeypatch):
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_MAX_REQUESTS', 2)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_IP_MAX_REQUESTS', 10)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app, client=('198.51.100.10', 12345))
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        first = await client.post(
            '/auth/login',
            json={'email': 'admin@example.com', 'password': 'wrong'},
            headers={'X-Forwarded-For': '203.0.113.10'},
        )
        second = await client.post(
            '/auth/login',
            json={'email': 'admin@example.com', 'password': 'wrong'},
            headers={'X-Forwarded-For': '203.0.113.11'},
        )
        limited = await client.post(
            '/auth/login',
            json={'email': 'admin@example.com', 'password': 'wrong'},
            headers={'X-Forwarded-For': '203.0.113.12'},
        )

    app.dependency_overrides.clear()

    assert first.status_code == 401
    assert second.status_code == 401
    assert limited.status_code == 429


@pytest.mark.asyncio
async def test_login_rate_limit_has_route_ip_aggregate_bucket(auth_db, monkeypatch):
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_MAX_REQUESTS', 100)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_IP_MAX_REQUESTS', 2)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app, client=('203.0.113.30', 12345))
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        first = await client.post('/auth/login', json={'email': 'one@example.com', 'password': 'wrong'})
        second = await client.post('/auth/login', json={'email': 'two@example.com', 'password': 'wrong'})
        limited = await client.post('/auth/login', json={'email': 'three@example.com', 'password': 'wrong'})

    other_transport = ASGITransport(app=app, client=('203.0.113.31', 12345))
    async with AsyncClient(transport=other_transport, base_url='http://test') as client:
        other_ip = await client.post('/auth/login', json={'email': 'three@example.com', 'password': 'wrong'})

    app.dependency_overrides.clear()

    assert first.status_code == 401
    assert second.status_code == 401
    assert limited.status_code == 429
    assert other_ip.status_code == 401


@pytest.mark.asyncio
async def test_forgot_password_rate_limit_preserves_email_cooldown_flow(auth_db, monkeypatch):
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_MAX_REQUESTS', 5)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_IP_MAX_REQUESTS', 10)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        first = await client.post('/auth/forgot-password', json={'email': 'admin@example.com'})
        second = await client.post('/auth/forgot-password', json={'email': 'admin@example.com'})

    app.dependency_overrides.clear()

    assert first.status_code == 200
    assert second.status_code == 200
    reset_tokens = await auth_db.scalar(
        select(func.count())
        .select_from(PortalEmailToken)
        .where(PortalEmailToken.user_id == 1, PortalEmailToken.purpose == TOKEN_PURPOSE_RESET)
    )
    assert reset_tokens == 1


@pytest.mark.asyncio
async def test_reset_password_rate_limit_does_not_consume_valid_token(auth_db, monkeypatch):
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_MAX_REQUESTS', 1)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_IP_MAX_REQUESTS', 1)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)
    raw_token = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app, client=('203.0.113.20', 12345))
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        invalid = await client.post(
            '/auth/reset-password',
            json={'token': 'invalid-reset-token', 'password': 'Changed12345!'},
        )
        limited = await client.post(
            '/auth/reset-password',
            json={'token': raw_token, 'password': 'Changed12345!'},
        )

    other_transport = ASGITransport(app=app, client=('203.0.113.21', 12345))
    async with AsyncClient(transport=other_transport, base_url='http://test') as client:
        usable = await client.post(
            '/auth/reset-password',
            json={'token': raw_token, 'password': 'Changed12345!'},
        )

    app.dependency_overrides.clear()

    assert invalid.status_code == 400
    assert limited.status_code == 429
    assert usable.status_code == 200


@pytest.mark.asyncio
async def test_onboarding_credentials_creates_tenant_for_new_owner(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    owner = PortalUser(
        id=70,
        portal_account_id=None,
        email='new-owner@example.com',
        password_hash=hash_password('Owner12345!'),
        full_name='New Owner',
        role='owner',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(owner)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    await auth_db.commit()
    token = create_access_token(70, 'owner')

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
        state = await client.get('/onboarding/state', headers={'Authorization': f'Bearer {token}'})
        before_count = await auth_db.scalar(select(func.count(PortalAccount.id)))
        credentials = await client.post(
            '/onboarding/credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'partner_token': 'partner', 'login': 'login', 'password': 'password'},
        )

    app.dependency_overrides.clear()

    assert state.status_code == 200
    assert state.json()['data']['step'] == 'pending_credentials'
    assert before_count == 1
    assert credentials.status_code == 200
    await auth_db.refresh(owner)
    assert owner.portal_account_id is not None
    assert owner.portal_account_id != 1
    account = await auth_db.get(PortalAccount, owner.portal_account_id)
    assert account is not None
    credential_id = credentials.json()['data']['credential_id']
    credential = await auth_db.get(YClientsCredential, credential_id)
    assert credential.portal_account_id == owner.portal_account_id


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
async def test_owner_can_assign_multiple_branches_to_manager_and_viewer(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        manager = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'multibranch.manager@example.com',
                'password': 'Manager12345!',
                'full_name': 'Multi Branch Manager',
                'role': 'manager',
                'company_ids': [1, 2],
            },
        )
        viewer = await client.post(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'email': 'multibranch.viewer@example.com',
                'password': 'Viewer12345!',
                'full_name': 'Multi Branch Viewer',
                'role': 'viewer',
                'company_ids': [1, 2],
            },
        )
        updated = await client.patch(
            '/auth/admin/users/2',
            headers={'Authorization': f'Bearer {token}'},
            json={'company_ids': [1, 2]},
        )

        assert manager.status_code == 200
        assert manager.json()['data']['company_ids'] == [1, 2]
        assert viewer.status_code == 200
        assert viewer.json()['data']['company_ids'] == [1, 2]
        assert updated.status_code == 200
        assert updated.json()['data']['company_ids'] == [1, 2]

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
        assert listed.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_manages_yclients_credentials(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    token = create_access_token(1, 'owner')
    partner_secret = 'partner-secret-value'
    login_secret = 'login-secret-value'
    password_secret = 'password-secret-value'
    bad_partner_secret = 'bad-partner-secret-value'
    bad_login_secret = 'bad-login-secret-value'
    bad_password_secret = 'bad-password-secret-value'
    validation_secret = 'validation-secret-value'
    all_secret_values = (
        partner_secret,
        login_secret,
        password_secret,
        bad_partner_secret,
        bad_login_secret,
        bad_password_secret,
        validation_secret,
    )

    def assert_no_raw_secret_fields(value):
        forbidden_keys = {
            'partner_token',
            'login',
            'password',
            'partner_token_encrypted',
            'login_encrypted',
            'password_encrypted',
        }
        if isinstance(value, dict):
            for key, item in value.items():
                assert key not in forbidden_keys
                assert not key.endswith('_encrypted')
                assert_no_raw_secret_fields(item)
        elif isinstance(value, list):
            for item in value:
                assert_no_raw_secret_fields(item)
        elif isinstance(value, str):
            for secret in all_secret_values:
                assert secret not in value

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            self.partner_token = partner_token
            self.login = login
            self.password = password

        def authenticate(self):
            return (
                self.partner_token == partner_secret
                and self.login == login_secret
                and self.password == password_secret
            )

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        payload_test = await client.post(
            '/auth/admin/yclients-credentials/test',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'partner_token': partner_secret,
                'login': login_secret,
                'password': password_secret,
            },
        )
        assert payload_test.status_code == 200
        assert payload_test.json()['source_type'] == 'yclients'
        assert_no_raw_secret_fields(payload_test.json())

        missing_payload_test = await client.post(
            '/auth/admin/yclients-credentials/test',
            headers={'Authorization': f'Bearer {token}'},
            json={},
        )
        assert missing_payload_test.status_code == 400
        assert missing_payload_test.json()['detail'] == 'Invalid credential request'
        for identifier in ('partner_token', 'login', 'password'):
            assert identifier not in missing_payload_test.text

        unsupported_source_test = await client.post(
            '/auth/admin/yclients-credentials/test',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'source_type': 'unsupported',
                'partner_token': partner_secret,
                'login': login_secret,
                'password': password_secret,
            },
        )
        assert unsupported_source_test.status_code == 400
        assert unsupported_source_test.json()['detail'] == 'Unsupported credential source type'
        for secret in all_secret_values:
            assert secret not in unsupported_source_test.text

        bad_payload_test = await client.post(
            '/auth/admin/yclients-credentials/test',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'partner_token': bad_partner_secret,
                'login': bad_login_secret,
                'password': bad_password_secret,
            },
        )
        assert bad_payload_test.status_code == 400
        assert_no_raw_secret_fields(bad_payload_test.json())

        invalid_create = await client.post(
            '/auth/admin/yclients-credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'title': 'Invalid credential',
                'partner_token': validation_secret * 300,
                'login': validation_secret,
                'password': validation_secret,
                'company_ids': [1],
            },
        )
        assert invalid_create.status_code == 422
        assert invalid_create.json()['detail'] == 'Invalid credential request'
        for identifier in ('partner_token', 'login', 'password'):
            assert identifier not in invalid_create.text

        created = await client.post(
            '/auth/admin/yclients-credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={
                'title': 'Main credential',
                'partner_token': partner_secret,
                'login': login_secret,
                'password': password_secret,
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
        assert 'login' not in data
        assert 'password' not in data
        assert_no_raw_secret_fields(created.json())
        for secret in all_secret_values:
            assert secret not in created.text
            assert secret not in payload_test.text
            assert secret not in bad_payload_test.text
            assert secret not in missing_payload_test.text
            assert secret not in unsupported_source_test.text
            assert secret not in invalid_create.text

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
        assert_no_raw_secret_fields(checked.json())
        assert_no_raw_secret_fields(checked_after_update.json())
        assert_no_raw_secret_fields(listed.json())
        for secret in all_secret_values:
            assert secret not in checked.text
            assert secret not in checked_after_update.text
            assert secret not in listed.text

    app.dependency_overrides.clear()

    audits = (
        await auth_db.execute(
            select(PortalAuditEvent)
            .where(PortalAuditEvent.action.in_([
                'yclients_credentials.payload_tested',
                'yclients_credentials.created',
                'yclients_credentials.tested',
                'yclients_credentials.updated',
                'yclients_credentials.listed',
            ]))
            .order_by(PortalAuditEvent.id.asc())
        )
    ).scalars().all()
    actions = [audit.action for audit in audits]
    assert 'yclients_credentials.payload_tested' in actions
    assert 'yclients_credentials.created' in actions
    assert actions.count('yclients_credentials.tested') == 2
    assert 'yclients_credentials.updated' in actions
    assert 'yclients_credentials.listed' in actions
    metadata_text = json.dumps([audit.metadata_json for audit in audits], sort_keys=True)
    for secret in all_secret_values:
        assert secret not in metadata_text
    assert any(
        audit.action == 'yclients_credentials.listed' and audit.metadata_json == {'success': True, 'count': 1}
        for audit in audits
    )
    assert any(
        audit.action == 'yclients_credentials.payload_tested'
        and audit.metadata_json == {'source_type': 'yclients', 'success': True}
        for audit in audits
    )
    assert any(
        audit.action == 'yclients_credentials.payload_tested'
        and audit.metadata_json == {'source_type': 'yclients', 'success': False, 'error_type': 'HTTPException'}
        for audit in audits
    )
    assert any(
        audit.action == 'yclients_credentials.payload_tested'
        and audit.metadata_json == {'source_type': 'yclients', 'success': False, 'reason': 'invalid_payload'}
        for audit in audits
    )
    assert any(
        audit.action == 'yclients_credentials.payload_tested'
        and audit.metadata_json == {'source_type': 'invalid', 'success': False, 'error_type': 'unsupported_source_type'}
        for audit in audits
    )


@pytest.mark.asyncio
async def test_saved_yclients_credentials_test_failures_are_sanitized(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    partner_secret = 'saved-partner-secret'
    login_secret = 'saved-login-secret'
    password_secret = 'saved-password-secret'
    token = create_access_token(1, 'owner')
    credential = new_credential(
        portal_account_id=1,
        title='Saved credential',
        partner_token=partner_secret,
        login=login_secret,
        password=password_secret,
        is_active=True,
    )
    auth_db.add(credential)
    await auth_db.commit()
    await auth_db.refresh(credential)

    class FailingYClientsAPI:
        def __init__(self, partner_token, login, password):
            self.partner_token = partner_token
            self.login = login
            self.password = password

        def authenticate(self):
            return False

    monkeypatch.setattr('data_sources.YClientsAPI', FailingYClientsAPI)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        failed = await client.post(
            f'/auth/admin/yclients-credentials/{credential.id}/test',
            headers={'Authorization': f'Bearer {token}'},
        )
        listed_after_failed = await client.get(
            '/auth/admin/yclients-credentials',
            headers={'Authorization': f'Bearer {token}'},
        )

        credential.partner_token_encrypted = 'not-a-valid-fernet-token'
        await auth_db.commit()

        corrupt = await client.post(
            f'/auth/admin/yclients-credentials/{credential.id}/test',
            headers={'Authorization': f'Bearer {token}'},
        )
        listed_after_corrupt = await client.get(
            '/auth/admin/yclients-credentials',
            headers={'Authorization': f'Bearer {token}'},
        )

    app.dependency_overrides.clear()

    assert failed.status_code == 400
    assert failed.json()['detail'] == 'Data source authentication failed'
    assert corrupt.status_code == 500
    assert corrupt.json()['detail'] == 'Stored credentials could not be decrypted'
    failed_row = listed_after_failed.json()['data'][0]
    corrupt_row = listed_after_corrupt.json()['data'][0]
    assert failed_row['last_error'] == 'Data source authentication failed'
    assert corrupt_row['last_error'] == 'Stored credentials could not be decrypted'
    for response in (failed, listed_after_failed, corrupt, listed_after_corrupt):
        for secret in (partner_secret, login_secret, password_secret):
            assert secret not in response.text
    for response in (failed, corrupt):
        for identifier in ('partner_token', 'login', 'password'):
            assert identifier not in response.text

    audits = (
        await auth_db.execute(
            select(PortalAuditEvent)
            .where(PortalAuditEvent.action == 'yclients_credentials.tested')
            .order_by(PortalAuditEvent.id.asc())
        )
    ).scalars().all()
    assert len(audits) == 2
    assert audits[0].metadata_json == {'source_type': 'yclients', 'success': False, 'error_type': 'HTTPException'}
    assert audits[1].metadata_json['source_type'] == 'yclients'
    assert audits[1].metadata_json['success'] is False
    assert audits[1].metadata_json['error_type']
    metadata_text = json.dumps([audit.metadata_json for audit in audits], sort_keys=True)
    for secret in (partner_secret, login_secret, password_secret):
        assert secret not in metadata_text


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
            json={'role': 'viewer', 'email': 'worker9003@example.com', 'company_ids': [1, 2]},
        )
        assert created.status_code == 200
        data = created.json()['data']
        assert data['email'] == 'worker9003@example.com'
        assert data['user_id'] == 9003
        assert data['staff_id'] == 9003
        assert 'initial_password' not in data
        branch_ids = (
            await auth_db.execute(
                select(PortalUserBranch.company_id)
                .where(PortalUserBranch.user_id == 9003)
                .order_by(PortalUserBranch.company_id.asc())
            )
        ).scalars().all()
        assert branch_ids == [1, 2]

        passwords = await client.get(
            '/auth/admin/initial-passwords',
            headers={'Authorization': f'Bearer {token}'},
        )
        assert passwords.status_code == 200
        pending_invites = passwords.json()['data']
        assert any(row['email'] == 'worker9003@example.com' for row in pending_invites)
        for row in pending_invites:
            assert 'initial_password' not in row

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_change_password_updates_hash_and_invalidates_old_password(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = create_access_token(2, 'manager')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        user = (await auth_db.execute(select(PortalUser).where(PortalUser.id == 2))).scalar_one()
        changed = await client.post(
            '/auth/change-password',
            headers={'Authorization': f'Bearer {token}'},
            json={'current_password': 'Manager12345!', 'new_password': 'NewManager123!'},
        )
        assert changed.status_code == 200

        await auth_db.refresh(user)
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
async def test_initial_passwords_endpoint_lists_pending_invites_without_passwords(auth_db, monkeypatch):
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

        pending_invites = await client.get(
            '/auth/admin/initial-passwords',
            headers={'Authorization': f'Bearer {branch_token}'},
        )
        assert pending_invites.status_code == 200
        data = pending_invites.json()['data']
        assert pending_invites.json()['deprecated'] is True
        emails = {row['email'] for row in data}
        assert 'branch.worker@example.com' in emails
        assert 'other.branch.worker@example.com' not in emails
        for row in data:
            assert row['user_id']
            assert 'initial_password' not in row
        branch_row = next(row for row in data if row['email'] == 'branch.worker@example.com')
        assert branch_row['full_name'] == 'Branch Worker'
        assert branch_row['staff_id'] == 9004
        assert branch_row['company_ids'] == [1]
        assert branch_row['password_reset_sent_at'] is None

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

    async def _capture_send(db, user):
        sent.append(user.email)

    monkeypatch.setattr('auth_routes.send_account_invite_email', _capture_send)

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
        assert sent == ['real.user@example.com']

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_distribute_credentials_reports_invite_failures_after_rollback(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    auth_db.add(
        PortalUser(
            id=12,
            portal_account_id=1,
            email='rollback.invite@example.com',
            password_hash=hash_password('Rollback123!'),
            full_name='Rollback Invite',
            role='viewer',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(PortalUserBranch(user_id=12, company_id=1))
    await auth_db.commit()

    async def _fail_after_rollback(db, _user):
        await db.rollback()
        raise RuntimeError('smtp unavailable')

    monkeypatch.setattr('auth_routes.send_account_invite_email', _fail_after_rollback)
    token = create_access_token(1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/auth/admin/distribute-credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'user_ids': [12]},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert data['sent_count'] == 0
    assert data['sent'] == []
    assert data['errors'] == [
        {'user_id': 12, 'email': 'rollback.invite@example.com', 'reason': 'smtp unavailable'}
    ]


# --------------------------------------------------------------------------- #
# Demo tenant: passwordless login, scope, onboarding bypass, read-only guard   #
# --------------------------------------------------------------------------- #

@pytest.fixture(autouse=True)
def _reset_demo_login_limiter():
    """Auth rate limiters are module-global; isolate them per test."""
    auth_routes._auth_rate_limit_hits.clear()
    auth_routes._demo_login_hits.clear()
    yield


async def _seed_demo_owner(db, user_id: int = 90):
    db.add(
        PortalUser(
            id=user_id,
            portal_account_id=1,
            email='demo@portal.local',
            password_hash=hash_password('DemoSeed123!'),
            full_name='Demo',
            role='owner',
            is_active=True,
            is_demo=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    await db.commit()
    return user_id


@pytest.mark.asyncio
async def test_demo_login_returns_is_demo_and_scoped_companies(auth_db):
    await _seed_demo_owner(auth_db)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        resp = await client.post('/auth/demo-login')
        assert resp.status_code == 200
        user = resp.json()['data']['user']
        assert user['is_demo'] is True
        assert sorted(user['company_ids']) == [1, 2]
        assert client.cookies.get(ACCESS_COOKIE_NAME)
        assert client.cookies.get(REFRESH_COOKIE_NAME)
        assert client.cookies.get(AUTH_CSRF_COOKIE_NAME)

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_demo_login_returns_503_when_not_provisioned(auth_db):
    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        resp = await client.post('/auth/demo-login')
        assert resp.status_code == 503
        assert resp.json()['detail'] == 'Demo mode is not available'

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_demo_login_rate_limited_after_window_cap(auth_db):
    await _seed_demo_owner(auth_db)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        codes = [(await client.post('/auth/demo-login')).status_code for _ in range(11)]

    app.dependency_overrides.clear()
    assert codes[:10] == [200] * 10
    assert codes[10] == 429


@pytest.mark.asyncio
async def test_onboarding_state_done_for_demo_user(auth_db):
    # The demo tenant has branches but no YClients credentials, so a non-demo
    # owner would report 'pending_credentials'; 'done' proves the is_demo bypass.
    demo_id = await _seed_demo_owner(auth_db)
    token = create_access_token(demo_id, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        resp = await client.get('/onboarding/state', headers={'Authorization': f'Bearer {token}'})
        assert resp.status_code == 200
        assert resp.json()['data']['step'] == 'done'

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_forbid_demo_blocks_writes_but_allows_reads(auth_db):
    demo_id = await _seed_demo_owner(auth_db)
    headers = {'Authorization': f'Bearer {create_access_token(demo_id, "owner")}'}

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        for path in (
            '/sync/trigger',
            '/dashboard/services/kpi_groups',
            '/onboarding/credentials',
            '/auth/logout-all',
        ):
            resp = await client.post(path, headers=headers, json={})
            assert resp.status_code == 403, (path, resp.status_code, resp.text)
            assert resp.json()['detail'] == 'Demo account is read-only'

        me = await client.get('/auth/me', headers=headers)
        assert me.status_code == 200
        assert me.json()['data']['is_demo'] is True

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_forbid_demo_does_not_block_non_demo_owner(auth_db):
    headers = {'Authorization': f'Bearer {create_access_token(1, "owner")}'}

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        me = await client.get('/auth/me', headers=headers)
        assert me.status_code == 200
        assert me.json()['data']['is_demo'] is False
        logout_all = await client.post('/auth/logout-all', headers=headers)
        assert logout_all.status_code == 200

    app.dependency_overrides.clear()


def test_access_context_money_metric_helpers():
    from auth_scope import (
        AccessContext,
        can_view_financials,
        can_view_money_metric,
        hidden_money_codes,
    )
    from plan_config import ALL_MONEY_CODES

    api_ctx = AccessContext.api_key()
    assert can_view_financials(api_ctx) is True
    assert hidden_money_codes(api_ctx) == frozenset()

    manager = AccessContext.from_user(1, 'manager', 1, [1])
    assert manager.money_metrics == frozenset()
    assert can_view_financials(manager) is False
    assert hidden_money_codes(manager) == ALL_MONEY_CODES

    branch_admin = AccessContext.from_user(2, 'branch_admin', 1, [1])
    assert branch_admin.money_metrics == ALL_MONEY_CODES
    assert can_view_financials(branch_admin) is True

    partial = AccessContext.from_user(3, 'manager', 1, [1], money_metrics=frozenset({'avg_check'}))
    assert can_view_money_metric(partial, 'avg_check') is True
    assert can_view_money_metric(partial, 'revenue') is False
    assert can_view_financials(partial) is False
    assert hidden_money_codes(partial) == ALL_MONEY_CODES - {'avg_check'}
