"""Portal auth and branch access control tests."""

import json
import logging
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import api
import auth_routes
import auth_sessions
import yclients_credential_routes
from api import app
from auth_sessions import ACCESS_COOKIE_NAME, REFRESH_COOKIE_NAME
from auth_service import TOKEN_PURPOSE_RESET, create_access_token, create_email_token, hash_password
from conftest import issue_access_token
from helpers import cached_password_hash
from config import AUTH_CSRF_COOKIE_NAME
from report_helpers import get_summary
from yclients_credentials import new_credential
from models import (
    Company,
    Group,
    PortalAccount,
    PortalAuditEvent,
    PortalEmailToken,
    PortalBranch,
    PortalRefreshToken,
    PortalUser,
    PortalUserBranch,
    Staff,
    SyncJob,
    YClientsCredential,
    YClientsCredentialCompany,
)


@pytest_asyncio.fixture
async def auth_db(async_session, monkeypatch):
    # Mail goes out from a background task on its own session; point it at the same in-memory database.
    monkeypatch.setattr(
        auth_routes, 'get_async_session_factory', lambda: async_sessionmaker(async_session.bind, expire_on_commit=False)
    )
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
        password_hash=cached_password_hash('Admin12345!'),
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
        password_hash=cached_password_hash('Manager12345!'),
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
        password_hash=cached_password_hash('Branch12345!'),
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
        initial_session = (await auth_db.scalars(
            select(PortalRefreshToken).where(PortalRefreshToken.user_id == 1)
        )).one()
        initial_session_id = initial_session.id
        initial_token_hash = initial_session.token_hash
        missing_csrf = await client.post('/auth/refresh')
        bearer_without_csrf = await client.post(
            '/auth/refresh',
            headers={'Authorization': 'Bearer not-a-valid-session-choice'},
        )
        csrf = client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        refresh = await client.post('/auth/refresh', headers={'X-CSRF-Token': csrf})
        rotated_sessions = list((await auth_db.scalars(
            select(PortalRefreshToken).where(PortalRefreshToken.user_id == 1)
        )).all())

    app.dependency_overrides.clear()

    assert missing_csrf.status_code == 403
    assert bearer_without_csrf.status_code == 403
    assert refresh.status_code == 200
    payload = refresh.json()
    assert 'access_token' not in payload['data']
    assert 'token_type' not in payload['data']
    assert payload['data']['user']['email'] == 'admin@example.com'
    assert len(rotated_sessions) == 1
    assert rotated_sessions[0].id == initial_session_id
    assert rotated_sessions[0].token_hash != initial_token_hash
    set_cookie_headers = refresh.headers.get_list('set-cookie')
    assert any(f'{ACCESS_COOKIE_NAME}=' in item and 'HttpOnly' in item for item in set_cookie_headers)
    assert any(f'{REFRESH_COOKIE_NAME}=' in item and 'HttpOnly' in item for item in set_cookie_headers)
    assert any(f'{AUTH_CSRF_COOKIE_NAME}=' in item and 'HttpOnly' not in item for item in set_cookie_headers)


@pytest.mark.asyncio
async def test_refresh_rotation_rejects_a_token_preloaded_before_a_competing_rotation(auth_db, monkeypatch):
    raw_refresh = 'preloaded-refresh-token'
    refresh_record = PortalRefreshToken(
        user_id=1,
        token_hash=auth_sessions._hash_token(raw_refresh),
        expires_at=datetime.utcnow() + timedelta(days=7),
        last_used_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(refresh_record)
    await auth_db.commit()

    async def load_preloaded_refresh(_db, _token_hash, _now):
        return refresh_record, False

    monkeypatch.setattr(auth_sessions, '_load_rotatable_refresh', load_preloaded_refresh)
    request = Request({
        'type': 'http',
        'headers': [],
        'client': ('127.0.0.1', 12345),
        'scheme': 'https',
        'server': ('test', 443),
    })
    _, rotated = await auth_sessions.rotate_session(
        auth_db,
        raw_refresh,
        request,
        access_token_fn=lambda _user, _session_id: 'access-token',
    )
    await auth_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await auth_sessions.rotate_session(
            auth_db,
            raw_refresh,
            request,
            access_token_fn=lambda _user, _session_id: 'access-token',
        )

    assert exc_info.value.status_code == 401
    stored_hash = await auth_db.scalar(
        select(PortalRefreshToken.token_hash).where(PortalRefreshToken.id == refresh_record.id)
    )
    assert stored_hash == auth_sessions._hash_token(rotated.refresh_token)


@pytest.mark.asyncio
async def test_sessions_list_removes_inactive_and_oldest_excess_sessions(auth_db, monkeypatch):
    monkeypatch.setattr(auth_sessions, 'AUTH_MAX_ACTIVE_SESSIONS', 3)
    now = datetime.utcnow()
    auth_db.add_all([
        PortalRefreshToken(
            id=100 + offset,
            user_id=1,
            token_hash=f'active-{offset}',
            expires_at=now + timedelta(days=10),
            last_used_at=now - timedelta(hours=offset),
            created_at=now - timedelta(days=offset),
        )
        for offset in range(5)
    ])
    auth_db.add_all([
        PortalRefreshToken(
            id=200,
            user_id=1,
            token_hash='expired',
            expires_at=now - timedelta(seconds=1),
            last_used_at=now - timedelta(days=40),
            created_at=now - timedelta(days=40),
        ),
    ])
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    bearer = create_access_token(1, 'owner', 0, 100)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/auth/sessions', headers={'Authorization': f'Bearer {bearer}'})
        removed_session = await client.get(
            '/auth/me',
            headers={'Authorization': f'Bearer {create_access_token(1, "owner", 0, 104)}'},
        )
        kept_session = await client.get(
            '/auth/me',
            headers={'Authorization': f'Bearer {create_access_token(1, "owner", 0, 100)}'},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [item['id'] for item in response.json()['data']] == [100, 101, 102]
    remaining_ids = list((await auth_db.scalars(
        select(PortalRefreshToken.id)
        .where(PortalRefreshToken.user_id == 1)
        .order_by(PortalRefreshToken.id)
    )).all())
    assert remaining_ids == [100, 101, 102]
    assert removed_session.status_code == 401
    assert kept_session.status_code == 200


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
        cookie_bearer = await issue_access_token(auth_db, 1, 'owner')
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

    token = await issue_access_token(auth_db, 1, 'owner')
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

    token = await issue_access_token(auth_db, 50, 'platform_admin')
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

    token = await issue_access_token(auth_db, 51, 'platform_admin')
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

    token = await issue_access_token(auth_db, 52, 'platform_admin')
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

    token = await issue_access_token(auth_db, 55, 'platform_admin')
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

    token = await issue_access_token(auth_db, 52, 'platform_admin')
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
    # The successful list of tenant 2 left no row behind.
    assert not any(audit.portal_account_id == 2 for audit in audits)


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

    token = await issue_access_token(auth_db, 53, 'platform_admin')
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

    token = await issue_access_token(auth_db, 54, 'platform_admin')
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

    token = await issue_access_token(auth_db, 57, 'platform_admin')
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

    token = await issue_access_token(auth_db, 53, 'platform_admin')
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

    token = await issue_access_token(auth_db, 53, 'platform_admin')
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
    token = await issue_access_token(auth_db, 2, 'manager')

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

        forbidden = await get_summary(
            client,
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31', 'company_id': 2},
            headers={'Authorization': f'Bearer {token}'},
        )
        assert forbidden.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_onboarding_branches_queue_full_sync(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 70, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
                'role': 'barber',
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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 3, 'branch_admin')

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
                'role': 'barber',
                'company_ids': [1],
            },
        )
        assert created.status_code == 200
        assert created.json()['data']['role'] == 'barber'

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_branch_admin_cannot_create_peer_role(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = await issue_access_token(auth_db, 3, 'branch_admin')

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
    token = await issue_access_token(auth_db, 3, 'branch_admin')

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
                'role': 'barber',
                'company_ids': [2],
            },
        )
        assert created.status_code == 403

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_owner_lists_same_rank_and_lower(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 2, 'manager')

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
                'role': 'barber',
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


@pytest.mark.parametrize('phase', ['credential_test', 'company_discovery'])
@pytest.mark.parametrize(('failure_kind', 'expected_status'), [('http', 429), ('unexpected', 500)])
@pytest.mark.asyncio
async def test_create_yclients_credential_failures_are_sanitized_and_audited(
    auth_db,
    monkeypatch,
    phase,
    failure_kind,
    expected_status,
):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    secret = 'credential-create-secret'
    failure = (
        HTTPException(status_code=429, detail=f'upstream rejected {secret}')
        if failure_kind == 'http'
        else RuntimeError(f'unexpected failure with {secret}')
    )

    if phase == 'credential_test':
        def fail_credential_test(*_args, **_kwargs):
            raise failure

        monkeypatch.setattr(yclients_credential_routes, '_authenticated_adapter', fail_credential_test)
        company_ids = [1]
    else:
        monkeypatch.setattr(yclients_credential_routes, '_authenticated_adapter', lambda *_args, **_kwargs: None)

        async def fail_company_discovery(*_args, **_kwargs):
            raise failure

        monkeypatch.setattr(
            yclients_credential_routes, '_sync_credential_companies_from_yclients', fail_company_discovery
        )
        company_ids = []

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/auth/admin/yclients-credentials',
            headers={'Authorization': f"Bearer {await issue_access_token(auth_db, 1, 'owner')}"},
            json={
                'title': 'Credential that fails safely',
                'partner_token': secret,
                'login': 'login',
                'password': 'password',
                'company_ids': company_ids,
            },
        )
    app.dependency_overrides.clear()

    assert response.status_code == expected_status
    assert response.json()['detail'] == 'Data source authentication failed'
    assert secret not in response.text
    assert await auth_db.scalar(select(func.count()).select_from(YClientsCredential)) == 0
    audits = list((await auth_db.scalars(
        select(PortalAuditEvent).where(PortalAuditEvent.action == 'yclients_credentials.create_failed')
    )).all())
    assert len(audits) == 1
    assert audits[0].portal_account_id == 1
    assert audits[0].metadata_json == {
        'success': False,
        'error_type': 'HTTPException' if failure_kind == 'http' else 'RuntimeError',
    }


@pytest.mark.asyncio
async def test_create_credential_does_not_save_one_when_the_group_listing_fails(auth_db, monkeypatch):
    from data_sources import YClientsDataSourceAdapter

    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    class GroupListingFails:
        def __init__(self, *args, **kwargs):
            pass

        def get_groups(self):
            return None

    adapter = YClientsDataSourceAdapter('partner', 'login', 'password', api_factory=GroupListingFails)
    monkeypatch.setattr(yclients_credential_routes, '_authenticated_adapter', lambda *_args, **_kwargs: adapter)

    async with _Api(auth_db) as client:
        response = await client.post(
            '/auth/admin/yclients-credentials',
            headers=await _bearer(auth_db, 1, 'owner'),
            json={'title': 'No listing', 'partner_token': 'p', 'login': 'l', 'password': 'w', 'company_ids': []},
        )

    assert response.status_code == 400
    assert response.json()['detail'] == 'Data source authentication failed'
    assert await auth_db.scalar(select(func.count()).select_from(YClientsCredential)) == 0


@pytest.mark.asyncio
async def test_owner_manages_yclients_credentials(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    token = await issue_access_token(auth_db, 1, 'owner')
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
    # A successful list is a read of masked payloads: it writes no audit row (only a refused tenant selection does).
    assert 'yclients_credentials.listed' not in actions
    metadata_text = json.dumps([audit.metadata_json for audit in audits], sort_keys=True)
    for secret in all_secret_values:
        assert secret not in metadata_text
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
    token = await issue_access_token(auth_db, 1, 'owner')
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
    token = await issue_access_token(auth_db, 3, 'branch_admin')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
async def test_cross_tenant_update_and_delete_match_not_found_response(auth_db, monkeypatch):
    """A cross-tenant id must be indistinguishable from one that does not exist at all.

    Before the fix, admin_update_user/admin_delete_user returned a distinguishable 403 ("Cannot
    manage/delete user from another tenant") for a row that exists in another tenant, but a 404
    ("User not found") for a genuinely missing id. That difference is an id-enumeration oracle:
    any owner/branch_admin could binary-search the whole PortalUser table across every tenant.
    `_load_credential`/`_load_manageable_staff` already return 404 uniformly for both cases —
    these two endpoints must match that convention.
    """
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Second tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=70,
            portal_account_id=2,
            email='tenant-b-target@example.com',
            password_hash=hash_password('TenantBTarget123!'),
            full_name='Tenant B Target',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    await auth_db.commit()

    token = await issue_access_token(auth_db, 1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    headers = {'Authorization': f'Bearer {token}'}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        missing_patch = await client.patch(
            '/auth/admin/users/999999', headers=headers, json={'full_name': 'Nobody'}
        )
        cross_tenant_patch = await client.patch(
            '/auth/admin/users/70', headers=headers, json={'full_name': 'Hijacked'}
        )
        missing_delete = await client.delete('/auth/admin/users/999998', headers=headers)
        cross_tenant_delete = await client.delete('/auth/admin/users/70', headers=headers)

    app.dependency_overrides.clear()

    assert missing_patch.status_code == 404
    assert cross_tenant_patch.status_code == 404
    assert cross_tenant_patch.json() == missing_patch.json()

    assert missing_delete.status_code == 404
    assert cross_tenant_delete.status_code == 404
    assert cross_tenant_delete.json() == missing_delete.json()


@pytest.mark.asyncio
async def test_created_manager_appears_in_dashboard_staff(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    token = await issue_access_token(auth_db, 1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        created = await client.post(
            '/auth/admin/staff/9003/create-account',
            headers={'Authorization': f'Bearer {token}'},
            json={'role': 'barber'},
        )
        assert created.status_code == 409
        assert 'Real email is required' in created.json()['detail']

        created = await client.post(
            '/auth/admin/staff/9003/create-account',
            headers={'Authorization': f'Bearer {token}'},
            json={'role': 'barber', 'email': 'worker9003@example.com', 'company_ids': [1, 2]},
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
    token = await issue_access_token(auth_db, 2, 'manager')

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
    auth_db.add_all([
        Staff(
            id=9008,
            name='Branch Administrator',
            email='branch.administrator@example.com',
            position='Администратор',
            company_id=1,
            fired=0,
            bookable=True,
        ),
        Staff(
            id=9005,
            name='Other Branch Worker',
            email='other.branch.worker@example.com',
            position='master',
            company_id=2,
            fired=0,
            bookable=True,
        ),
    ])
    await auth_db.commit()
    super_token = await issue_access_token(auth_db, 1, 'owner')
    branch_token = await issue_access_token(auth_db, 3, 'branch_admin')

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

    # Bulk provisioning asks nobody, so the label has to come from the CRM position — it is
    # what the user list shows and the only thing per-role money visibility can key on.
    roles = {
        row.id: row.role
        for row in (await auth_db.execute(select(PortalUser).where(PortalUser.id.in_([9004, 9008])))).scalars().all()
    }
    assert roles == {9004: 'barber', 9008: 'admin'}


@pytest.mark.asyncio
async def test_create_account_without_a_role_takes_it_from_the_crm_position(auth_db, monkeypatch):
    """The row-menu shortcut skips the modal, so nobody picks a role — the server must.

    Deriving it in the browser instead would make the answer depend on which button was
    pressed: the two implementations cannot agree on a mixed title like «Барбер-администратор».
    """
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add_all([
        Staff(
            id=9101,
            name='Front Desk',
            email='front.desk@example.com',
            position='Администратор',
            company_id=1,
            fired=0,
            bookable=True,
        ),
        Staff(
            id=9102,
            name='Chair',
            email='chair@example.com',
            position='Барбер',
            company_id=1,
            fired=0,
            bookable=True,
        ),
    ])
    await auth_db.commit()
    token = await issue_access_token(auth_db, 1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    roles = {}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        # The modal preselects from this rather than deriving the role a second time.
        listing = await client.get('/auth/admin/users', headers={'Authorization': f'Bearer {token}'})
        assert listing.status_code == 200
        suggested = {
            row['staff_id']: row['suggested_role']
            for row in listing.json()['data']
            if row.get('staff_id') in (9101, 9102) and not row['is_portal_user']
        }
        assert suggested == {9101: 'admin', 9102: 'barber'}

        for staff_id in (9101, 9102):
            created = await client.post(
                f'/auth/admin/staff/{staff_id}/create-account',
                headers={'Authorization': f'Bearer {token}'},
                json={'company_ids': [1]},
            )
            assert created.status_code == 200, created.text
            roles[staff_id] = created.json()['data']['role']
    app.dependency_overrides.clear()

    assert roles == {9101: 'admin', 9102: 'barber'}


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
    token = await issue_access_token(auth_db, 1, 'owner')

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
            role='barber',
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
            role='barber',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(PortalUserBranch(user_id=11, company_id=1))
    await auth_db.commit()

    token = await issue_access_token(auth_db, 1, 'owner')

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
            role='barber',
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
    token = await issue_access_token(auth_db, 1, 'owner')

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
    # The exception text (SMTP host, DB internals) stays server-side.
    assert data['errors'] == [
        {'user_id': 12, 'email': 'rollback.invite@example.com', 'reason': 'Email delivery failed'}
    ]
    assert 'smtp unavailable' not in response.text


@pytest.mark.asyncio
async def test_distribute_credentials_keeps_going_after_a_failed_invite_rolled_the_session_back(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    for user_id in (12, 13):
        auth_db.add(
            PortalUser(
                id=user_id,
                portal_account_id=1,
                email=f'invite{user_id}@example.com',
                password_hash=hash_password('Rollback123!'),
                full_name='Invite',
                role='barber',
                is_active=True,
                email_verified_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
        )
        auth_db.add(PortalUserBranch(user_id=user_id, company_id=1))
    await auth_db.commit()
    attempts = []

    async def _fail_first_after_rollback(db, user):
        attempts.append(user.id)
        if len(attempts) == 1:
            await db.rollback()
            raise RuntimeError('smtp unavailable')

    monkeypatch.setattr('auth_routes.send_account_invite_email', _fail_first_after_rollback)
    token = await issue_access_token(auth_db, 1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/auth/admin/distribute-credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'user_ids': [12, 13]},
        )
    app.dependency_overrides.clear()

    # The rollback expires the loaded rows; the second recipient must still be processed, not 500 the batch.
    assert response.status_code == 200
    data = response.json()['data']
    assert [item['user_id'] for item in data['sent']] == [13]
    assert [item['user_id'] for item in data['errors']] == [12]


@pytest.mark.asyncio
async def test_distribute_credentials_does_not_leak_cross_tenant_email(auth_db, monkeypatch):
    """The select behind distribute-credentials must be tenant-scoped.

    Before the fix, PortalUser rows were fetched with no tenant filter at all, and the target's
    email was read into the `errors` entry before the `_same_tenant` check rejected it. Any
    owner/branch_admin could POST arbitrary ids (they are small sequential integers) and harvest
    the email of every user in every other tenant from the response.
    """
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Second tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=71,
            portal_account_id=2,
            email='tenant-b-secret@example.com',
            password_hash=hash_password('TenantBSecret123!'),
            full_name='Tenant B Secret',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    await auth_db.commit()

    token = await issue_access_token(auth_db, 1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/auth/admin/distribute-credentials',
            headers={'Authorization': f'Bearer {token}'},
            json={'user_ids': [71]},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    # No trace of the tenant-B user's email anywhere in the payload, not just outside `errors`.
    assert 'tenant-b-secret@example.com' not in response.text
    data = response.json()['data']
    assert data['errors'] == [{'user_id': 71, 'reason': 'User not found'}]
    assert data['sent'] == []
    assert data['skipped'] == []


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
async def test_demo_tenant_hides_reports_its_data_cannot_populate(auth_db):
    """Demo is seeded, not synced, so it has no SyncSourceState coverage at all.

    year_over_year certifies whole years against that coverage and can therefore
    only render every metric as unknown for demo — hide it instead of shipping a
    permanently blank report in the showcase.
    """
    await _seed_demo_owner(auth_db)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        assert (await client.post('/auth/demo-login')).status_code == 200

        catalog = await client.get('/dashboard/reports')
        assert catalog.status_code == 200
        demo_ids = {item['id'] for item in catalog.json()['data']}
        assert 'year_over_year' not in demo_ids
        # Hiding one report must not thin out the rest of the catalog.
        assert 'financial_overview' in demo_ids
        assert len(demo_ids) == 16

        # A bookmarked URL must not bypass the catalog.
        blocked = await client.get('/dashboard/reports/data', params={
            'report_id': 'year_over_year',
            'start_date': '2026-03-01',
            'end_date': '2026-07-31',
        })
        assert blocked.status_code == 404

    # Without a demo session the report stays in the catalog.
    async with AsyncClient(transport=transport, base_url='http://test') as anon:
        full = await anon.get('/dashboard/reports')
        assert 'year_over_year' in {item['id'] for item in full.json()['data']}
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
    token = await issue_access_token(auth_db, demo_id, 'owner')

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
    headers = {'Authorization': f'Bearer {await issue_access_token(auth_db, demo_id, "owner")}'}

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        for path in (
            '/sync/trigger',
            '/dashboard/plan/opz_fact',
            '/dashboard/payments/yandex_pay',
            '/onboarding/credentials',
            '/auth/logout-all',
        ):
            resp = await client.post(path, headers=headers, json={})
            assert resp.status_code == 403, (path, resp.status_code, resp.text)
            assert resp.json()['detail'] == 'Demo account is read-only'

        batch = await client.patch(
            '/dashboard/services',
            headers=headers,
            json={'row_changes': []},
        )
        assert batch.status_code == 403
        assert batch.json()['detail'] == 'Demo account is read-only'

        me = await client.get('/auth/me', headers=headers)
        assert me.status_code == 200
        assert me.json()['data']['is_demo'] is True

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_forbid_demo_does_not_block_non_demo_owner(auth_db):
    headers = {'Authorization': f'Bearer {await issue_access_token(auth_db, 1, "owner")}'}

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

    # A manager gets the average check by default, but never the branch revenue — and
    # `can_view_financials` asks about revenue, so it stays False.
    manager = AccessContext.from_user(1, 'manager', 1, [1])
    assert manager.money_metrics == frozenset({'avg_check'})
    assert can_view_financials(manager) is False
    assert hidden_money_codes(manager) == ALL_MONEY_CODES - {'avg_check'}

    barber = AccessContext.from_user(4, 'barber', 1, [1])
    admin = AccessContext.from_user(5, 'admin', 1, [1])
    assert barber.money_metrics == frozenset()
    assert admin.money_metrics == barber.money_metrics

    branch_admin = AccessContext.from_user(2, 'branch_admin', 1, [1])
    assert branch_admin.money_metrics == ALL_MONEY_CODES
    assert can_view_financials(branch_admin) is True

    partial = AccessContext.from_user(3, 'manager', 1, [1], money_metrics=frozenset({'avg_check'}))
    assert can_view_money_metric(partial, 'avg_check') is True
    assert can_view_money_metric(partial, 'revenue') is False
    assert can_view_financials(partial) is False
    assert hidden_money_codes(partial) == ALL_MONEY_CODES - {'avg_check'}


@pytest.mark.asyncio
async def test_admin_and_barber_are_one_rank_under_two_names(auth_db, monkeypatch):
    """Two labels, one level of access: the picker names the job, it does not grant anything."""
    from auth_hierarchy import ROLE_LEVEL, assignable_roles
    from auth_scope import BRANCH_SCOPE_ROLES
    from plan_config import DEFAULT_ROLE_MONEY_CODES

    assert ROLE_LEVEL['admin'] == ROLE_LEVEL['barber']
    # A tie means neither can create the other — `assignable_roles` is strictly-lower only.
    assert assignable_roles('admin') == [] and assignable_roles('barber') == []
    assert assignable_roles('manager') == ['admin', 'barber']
    assert DEFAULT_ROLE_MONEY_CODES['admin'] == DEFAULT_ROLE_MONEY_CODES['barber']
    assert 'admin' not in BRANCH_SCOPE_ROLES and 'barber' not in BRANCH_SCOPE_ROLES

    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = await issue_access_token(auth_db, 1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    created = {}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        for role in ('admin', 'barber'):
            response = await client.post(
                '/auth/admin/users',
                headers={'Authorization': f'Bearer {token}'},
                json={
                    'email': f'{role}.pair@example.com',
                    'password': 'PairTest12345!',
                    'full_name': f'Pair {role}',
                    'role': role,
                    'company_ids': [1],
                },
            )
            assert response.status_code == 200, response.text
            created[role] = response.json()['data']

        # The rank tie is what makes the pair equal, so nothing about the name `admin` may
        # read as an administrator: the gates are membership in USER_ADMIN_ROLES, not a string.
        admin_headers = {
            'Authorization': f'Bearer {await issue_access_token(auth_db, created["admin"]["id"], "admin")}'
        }
        refusals = {
            '/auth/admin/users': (await client.get('/auth/admin/users', headers=admin_headers)).status_code,
            '/auth/admin/meta': (await client.get('/auth/admin/meta', headers=admin_headers)).status_code,
            '/dashboard/plan/settings': (
                await client.get(
                    '/dashboard/plan/settings',
                    params={'month': '2025-01', 'company_id': 1},
                    headers=admin_headers,
                )
            ).status_code,
        }
    app.dependency_overrides.clear()

    assert created['admin']['company_ids'] == created['barber']['company_ids'] == [1]
    # Both appear in the «Работник» filter; only the label they carry differs.
    positions = {
        row.position: row.portal_user_id
        for row in (
            await auth_db.execute(
                select(Staff).where(Staff.portal_user_id.in_([created['admin']['id'], created['barber']['id']]))
            )
        ).scalars().all()
    }
    assert set(positions) == {'admin', 'barber'}
    assert refusals == {
        '/auth/admin/users': 403,
        '/auth/admin/meta': 403,
        '/dashboard/plan/settings': 403,
    }


@pytest.mark.asyncio
async def test_the_label_is_what_opens_the_manual_fact_tabs(auth_db, monkeypatch):
    """The pair's only asymmetry, end to end.

    The role lands in `staff.position` (portal_staff_sync), `normalize_staff_category` reads
    `admin` as administrator, and that is what `manual_fact_scope` answers from. The chain runs
    through three modules and none of them mentions the other two, so it is exactly the kind of
    rule a readability cleanup — "write a human label into `position`" — breaks in silence.
    """
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    owner_token = await issue_access_token(auth_db, 1, 'owner')

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    scopes = {}
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        for role in ('admin', 'barber'):
            created = await client.post(
                '/auth/admin/users',
                headers={'Authorization': f'Bearer {owner_token}'},
                json={
                    'email': f'{role}.tabs@example.com',
                    'password': 'TabsTest12345!',
                    'full_name': f'Tabs {role}',
                    'role': role,
                    'company_ids': [1],
                },
            )
            assert created.status_code == 200, created.text
            user_id = created.json()['data']['id']
            me = await client.get(
                '/auth/me',
                headers={'Authorization': f'Bearer {await issue_access_token(auth_db, user_id, role)}'},
            )
            assert me.status_code == 200
            scopes[role] = me.json()['data']['manual_fact_scope']
    app.dependency_overrides.clear()

    # Same rank, same gates, same money — and still only one of them may enter reviews / OPZ.
    assert scopes == {'admin': 'self', 'barber': 'none'}


@pytest.mark.asyncio
async def test_portal_staff_sync_keeps_yclients_row_of_out_of_scope_branch(auth_db):
    """Branch scoping must not fire the CRM record the portal account was made from."""
    from portal_staff_sync import sync_portal_user_staff

    user = PortalUser(
        id=9101,
        portal_account_id=1,
        email='worker9101@example.com',
        password_hash=hash_password('Worker12345!'),
        full_name='Worker',
        role='manager',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    auth_db.add(user)
    auth_db.add_all([
        # CRM record the account was provisioned from: staff id == portal user id.
        Staff(id=9101, name='Worker', position='Администратор', company_id=1,
              fired=0, bookable=True, portal_user_id=9101, external_id=9101),
        # Mirror row created by the portal for a branch that is being dropped.
        Staff(id=9102, name='Worker', position='manager', company_id=2,
              fired=0, bookable=True, portal_user_id=9101),
    ])
    await auth_db.commit()

    await sync_portal_user_staff(auth_db, user, [])
    await auth_db.commit()

    rows = (
        await auth_db.execute(select(Staff).where(Staff.portal_user_id == 9101).order_by(Staff.id))
    ).scalars().all()
    assert [(row.id, row.fired) for row in rows] == [(9101, 0), (9102, 1)]


@pytest.mark.asyncio
async def test_me_reports_the_manual_fact_scope(auth_db, monkeypatch):
    """The portal draws the reviews / additional-OPZ tabs from this field alone."""
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    for user_id, position in ((9101, 'Администратор'), (9102, 'Барбер')):
        auth_db.add(
            PortalUser(
                id=user_id,
                portal_account_id=1,
                email=f'staff{user_id}@example.com',
                password_hash=hash_password('Staff12345!'),
                full_name=f'Staff {user_id}',
                role='barber',
                is_active=True,
                email_verified_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
        )
        auth_db.add(
            Staff(
                id=user_id,
                name=f'Staff {user_id}',
                position=position,
                company_id=1,
                fired=0,
                portal_user_id=user_id,
            )
        )
    auth_db.add(PortalUserBranch(user_id=9101, company_id=1))
    auth_db.add(PortalUserBranch(user_id=9102, company_id=1))
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        payloads = {}
        for user_id, role in (
            (1, 'owner'),
            (2, 'manager'),
            (3, 'branch_admin'),
            (9101, 'admin'),
            (9102, 'barber'),
        ):
            response = await client.get(
                '/auth/me',
                headers={'Authorization': f'Bearer {await issue_access_token(auth_db, user_id, role)}'},
            )
            assert response.status_code == 200
            payloads[user_id] = response.json()['data']
    app.dependency_overrides.clear()

    # Every branch-level role answers the same way, without touching the staff tables.
    assert [payloads[user_id]['manual_fact_scope'] for user_id in (1, 2, 3)] == ['branch'] * 3
    assert [payloads[user_id]['staff_id'] for user_id in (1, 2, 3)] == [None] * 3
    assert payloads[9101]['manual_fact_scope'] == 'self'
    assert payloads[9101]['staff_id'] == 9101
    # A barber has nothing to enter: both manual metrics are administrator ones.
    assert payloads[9102]['manual_fact_scope'] == 'none'


@pytest.mark.asyncio
async def test_me_ignores_a_staff_row_outside_the_assigned_branches(auth_db, monkeypatch):
    """The tabs and the endpoints must answer the same question.

    A provisioned employee keeps the CRM staff row of a branch they were moved away from
    (portal_staff_sync keeps it alive on purpose). That row is out of their branch scope, so
    it may not open a tab the editors would then refuse to fill.
    """
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        PortalUser(
            id=9103,
            portal_account_id=1,
            email='moved@example.com',
            password_hash=hash_password('Staff12345!'),
            full_name='Moved administrator',
            role='barber',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    # The CRM row of the branch they left: still an administrator, still not fired.
    auth_db.add(
        Staff(id=9103, name='Moved', position='Администратор', company_id=1, fired=0, portal_user_id=9103)
    )
    # The row the portal created in the branch they were moved to.
    auth_db.add(
        Staff(id=9203, name='Moved', position='barber', company_id=2, fired=0, portal_user_id=9103)
    )
    auth_db.add(PortalUserBranch(user_id=9103, company_id=2))
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        headers = {'Authorization': f'Bearer {await issue_access_token(auth_db, 9103, "barber")}'}
        me = await client.get('/auth/me', headers=headers)
        editor = await client.get('/dashboard/plan/reviews_fact', params={'month': '2025-01'}, headers=headers)
        summary = await get_summary(
            client,
            params={'start_date': '2025-01-01', 'end_date': '2025-01-31'},
            headers=headers,
        )
    app.dependency_overrides.clear()

    assert me.status_code == 200
    assert me.json()['data']['manual_fact_scope'] == 'none'
    # The self-scope clamp must land on the row inside the assigned branch, not on the
    # stale one the CRM keeps alive — otherwise the whole dashboard answers 'unknown staff_id'.
    assert me.json()['data']['staff_id'] == 9203
    assert summary.status_code == 200
    # Whatever /auth/me promises, the editor must agree: no rows out of branch scope.
    assert editor.status_code == 200
    assert editor.json()['data']['rows'] == []


@pytest.mark.asyncio
async def test_unonboarded_owner_sees_no_users_from_other_tenants(auth_db, monkeypatch):
    """`portal_account_id IS NULL` must not become a shared bucket for every fresh owner.

    An owner who registered but has not onboarded yet has `portal_account_id = None`. A tenant
    filter pushed into SQL as a plain `== active_portal_account_id` would compile to `IS NULL`
    for this actor and match every other not-yet-onboarded owner across every tenant — the leak
    this test pins closed for both admin listing endpoints.
    """
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        PortalUser(
            id=55,
            portal_account_id=None,
            email='fresh.owner@example.com',
            password_hash=hash_password('Fresh12345!'),
            full_name='Fresh Owner',
            role='owner',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(
        PortalUser(
            id=56,
            portal_account_id=None,
            email='other.fresh.owner@example.com',
            password_hash=hash_password('OtherFresh12345!'),
            full_name='Other Fresh Owner',
            role='owner',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    token = await issue_access_token(auth_db, 55, 'owner')
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        headers = {'Authorization': f'Bearer {token}'}
        users = await client.get('/auth/admin/users', headers=headers)
        passwords = await client.get('/auth/admin/initial-passwords', headers=headers)

    app.dependency_overrides.clear()

    assert users.status_code == 200
    assert users.json()['data'] == []
    assert passwords.status_code == 200
    assert passwords.json()['data'] == []


@pytest.mark.asyncio
async def test_admin_list_users_staff_repair_is_scoped_to_its_own_tenant(auth_db, monkeypatch):
    """The Staff mirror-row repair on GET /admin/users must not reach across tenants.

    `provision_staff_account` can assign a staff member to multiple branches without syncing
    the synthetic Staff mirror rows itself (see portal_account_provision.py); today that drift
    is repaired by GET /admin/users. Scoping the repair to the caller's own tenant must not
    strand tenant B's drift unrepaired forever — it stays reachable the next time tenant B's
    own admin opens tenant B's own Users page.
    """
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Company(id=3, title='Branch 3', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Second tenant', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=60,
            portal_account_id=2,
            email='tenant-b-owner@example.com',
            password_hash=hash_password('TenantBOwner12345!'),
            full_name='Tenant B Owner',
            role='owner',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(
        PortalUser(
            id=62,
            portal_account_id=2,
            email='tenant-b-manager@example.com',
            password_hash=hash_password('TenantBManager12345!'),
            full_name='Tenant B Manager',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    # No Staff row yet — the drift `provision_staff_account` leaves behind for a
    # multi-branch assignment, waiting on the next sync to create the mirror row.
    auth_db.add(PortalUserBranch(user_id=62, company_id=3))
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    tenant_a_token = await issue_access_token(auth_db, 1, 'owner')
    tenant_b_token = await issue_access_token(auth_db, 60, 'owner')
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        from_tenant_a = await client.get(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {tenant_a_token}'},
        )
        repaired_by_a = await auth_db.scalar(
            select(func.count()).select_from(Staff).where(Staff.portal_user_id == 62)
        )

        from_tenant_b = await client.get(
            '/auth/admin/users',
            headers={'Authorization': f'Bearer {tenant_b_token}'},
        )

    app.dependency_overrides.clear()

    assert from_tenant_a.status_code == 200
    # Tenant A's admin opening their own page must not write tenant B's Staff mirror row.
    assert repaired_by_a == 0

    assert from_tenant_b.status_code == 200
    repaired_row = (
        await auth_db.execute(select(Staff).where(Staff.portal_user_id == 62))
    ).scalar_one()
    # Tenant B's own admin opening their own page still repairs tenant B's own drift.
    assert repaired_row.company_id == 3
    assert repaired_row.fired == 0


@pytest.mark.asyncio
async def test_delete_session_is_scoped_to_the_owning_user(auth_db, monkeypatch):
    """DELETE /sessions/{id} must not let one user revoke another user's session."""
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    now = datetime.utcnow()
    victim_session = PortalRefreshToken(
        id=501,
        user_id=1,
        token_hash='victim-session-hash',
        expires_at=now + timedelta(days=10),
        last_used_at=now,
        created_at=now,
    )
    auth_db.add(victim_session)
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    attacker_token = await issue_access_token(auth_db, 2, 'manager')  # a different user
    owner_token = await issue_access_token(auth_db, 1, 'owner')  # the session's actual owner
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        attack = await client.delete('/auth/sessions/501', headers={'Authorization': f'Bearer {attacker_token}'})
        still_present = await auth_db.scalar(
            select(func.count()).select_from(PortalRefreshToken).where(PortalRefreshToken.id == 501)
        )
        own_delete = await client.delete('/auth/sessions/501', headers={'Authorization': f'Bearer {owner_token}'})

    app.dependency_overrides.clear()

    assert attack.status_code == 404
    assert still_present == 1
    assert own_delete.status_code == 200
    removed = await auth_db.scalar(
        select(func.count()).select_from(PortalRefreshToken).where(PortalRefreshToken.id == 501)
    )
    assert removed == 0


@pytest.mark.asyncio
async def test_logout_others_revokes_only_the_callers_own_other_sessions(auth_db, monkeypatch):
    """POST /logout-others must only ever touch the caller's own sessions."""
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    now = datetime.utcnow()
    other_own_session = PortalRefreshToken(
        id=602,
        user_id=1,
        token_hash='other-own-device-hash',
        expires_at=now + timedelta(days=10),
        last_used_at=now,
        created_at=now,
    )
    other_users_session = PortalRefreshToken(
        id=603,
        user_id=2,
        token_hash='other-user-hash',
        expires_at=now + timedelta(days=10),
        last_used_at=now,
        created_at=now,
    )
    auth_db.add_all([other_own_session, other_users_session])
    await auth_db.commit()

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        login = await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'Admin12345!'})
        assert login.status_code == 200
        csrf = client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        response = await client.post('/auth/logout-others', headers={'X-CSRF-Token': csrf})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    remaining_ids = set((await auth_db.scalars(select(PortalRefreshToken.id))).all())
    # The session /auth/login just created survives (it is "current"); the pre-existing other
    # device belonging to the same user is revoked; user 2's unrelated session is untouched.
    assert 602 not in remaining_ids
    assert 603 in remaining_ids
    assert len(remaining_ids) == 2


@pytest.mark.asyncio
async def test_resend_verification_only_emails_pending_unverified_accounts(auth_db, monkeypatch):
    """POST /resend-verification must not leak whether an account exists or is verified."""
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    pending = PortalUser(
        id=70,
        portal_account_id=1,
        email='pending@example.com',
        password_hash=hash_password('Pending12345!'),
        full_name='Pending User',
        role='owner',
        is_active=True,
        email_verified_at=None,
        created_at=datetime.utcnow(),
    )
    auth_db.add(pending)
    await auth_db.commit()
    # The mail goes out on the background task's own session, so the cooldown stamp it writes must be
    # read back from the database; a copy held by this session would never see it.
    auth_db.expunge(pending)

    sent = []

    def _capture_email(to_email, subject, body):
        sent.append(to_email)

    monkeypatch.setattr('auth_service.send_auth_email', _capture_email)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        first = await client.post('/auth/resend-verification', json={'email': 'pending@example.com'})
        second = await client.post('/auth/resend-verification', json={'email': 'pending@example.com'})
        already_verified = await client.post('/auth/resend-verification', json={'email': 'admin@example.com'})
        unknown = await client.post('/auth/resend-verification', json={'email': 'nobody@example.com'})

    app.dependency_overrides.clear()

    responses = [first, second, already_verified, unknown]
    assert [r.status_code for r in responses] == [200, 200, 200, 200]
    # Same generic message every time: the response must not reveal whether the account
    # exists, is already verified, or was skipped by the resend cooldown.
    messages = {r.json()['message'] for r in responses}
    assert len(messages) == 1
    # Only the first call for the pending account actually sends: the second is inside the
    # resend cooldown, the verified account has nothing to verify, and the unknown email
    # does not exist.
    assert sent == ['pending@example.com']


@pytest.mark.asyncio
async def test_a_burst_of_mail_requests_queues_one_mail_until_it_has_gone_out(auth_db):
    """The cooldown stamp lands only after the SMTP round trip, so requests inside it must not queue more mail."""
    from fastapi import BackgroundTasks

    async def sender(_session, _user):
        raise AssertionError('no such user: nothing may be sent')

    tasks = BackgroundTasks()
    auth_routes._queue_auth_email(tasks, 4242, sender)
    auth_routes._queue_auth_email(tasks, 4242, sender)
    auth_routes._queue_auth_email(tasks, 4243, sender)
    assert len(tasks.tasks) == 2

    await tasks()
    assert auth_routes._auth_emails_in_flight == {}
    again = BackgroundTasks()
    auth_routes._queue_auth_email(again, 4242, sender)
    assert len(again.tasks) == 1
    await again()


# ---------- password length: bcrypt only reads 72 bytes ----------

LONG_PASSWORD = 'Пароль' * 7  # 42 characters, 84 bytes: inside max_length, over the bcrypt limit


class _Api:
    """Context manager: the app wired to the test session, one client."""

    def __init__(self, auth_db):
        self.auth_db = auth_db

    async def __aenter__(self):
        async def override_db():
            yield self.auth_db

        app.dependency_overrides[api.get_async_db] = override_db
        self.client = AsyncClient(transport=ASGITransport(app=app), base_url='http://test')
        return await self.client.__aenter__()

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)
        app.dependency_overrides.clear()


async def _bearer(auth_db, user_id, role):
    return {'Authorization': f'Bearer {await issue_access_token(auth_db, user_id, role)}'}


def test_verify_password_never_raises_on_long_input_or_a_broken_hash():
    from auth_service import verify_password

    stored = hash_password('Admin12345!')
    assert verify_password('Admin12345!', stored) is True
    assert verify_password('a' * 100, stored) is False
    assert verify_password(LONG_PASSWORD, stored) is False
    assert verify_password('Admin12345!', 'not-a-bcrypt-hash') is False


@pytest.mark.asyncio
async def test_login_with_over_long_password_is_401_for_known_and_unknown_email(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    async with _Api(auth_db) as client:
        known = await client.post('/auth/login', json={'email': 'admin@example.com', 'password': LONG_PASSWORD})
        unknown = await client.post('/auth/login', json={'email': 'nobody@example.com', 'password': LONG_PASSWORD})
        huge = await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'x' * 129})

    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()
    assert huge.status_code == 422


@pytest.mark.asyncio
async def test_login_checks_a_dummy_hash_when_the_email_is_unknown(auth_db, monkeypatch):
    from auth_service import DUMMY_PASSWORD_HASH

    seen = []

    def record(password, password_hash):
        seen.append(password_hash)
        return False

    monkeypatch.setattr(auth_routes, 'verify_password', record)
    async with _Api(auth_db) as client:
        await client.post('/auth/login', json={'email': 'nobody@example.com', 'password': 'Whatever123!'})
        await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'Whatever123!'})

    assert seen[0] == DUMMY_PASSWORD_HASH
    assert seen[1] != DUMMY_PASSWORD_HASH


@pytest.mark.asyncio
async def test_every_password_entry_point_rejects_over_long_passwords_with_422(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr(auth_routes, 'AUTH_PUBLIC_REGISTRATION_ENABLED', True)
    reset_token = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)
    async with _Api(auth_db) as client:
        register = await client.post(
            '/auth/register', json={'email': 'long@example.com', 'password': LONG_PASSWORD}
        )
        reset = await client.post('/auth/reset-password', json={'token': reset_token, 'password': LONG_PASSWORD})
        change = await client.post(
            '/auth/change-password',
            headers=await _bearer(auth_db, 1, 'owner'),
            json={'current_password': 'Admin12345!', 'new_password': LONG_PASSWORD},
        )
        create = await client.post(
            '/auth/admin/users',
            headers=await _bearer(auth_db, 1, 'owner'),
            json={'email': 'created@example.com', 'password': LONG_PASSWORD, 'role': 'manager', 'company_ids': [1]},
        )
        # The longest ASCII password bcrypt can take in full is still accepted.
        ok = await client.post(
            '/auth/admin/users',
            headers=await _bearer(auth_db, 1, 'owner'),
            json={'email': 'fits@example.com', 'password': 'a' * 72, 'role': 'manager', 'company_ids': [1]},
        )

    for response in (register, reset, change, create):
        assert response.status_code == 422
        assert 'bytes' in response.text
    assert ok.status_code == 200
    # The rejected reset did not spend the token.
    assert await auth_db.scalar(
        select(PortalEmailToken.used_at).where(PortalEmailToken.user_id == 1)
    ) is None


# ---------- AUTH-6/21: logging of failures ----------

@pytest.mark.asyncio
async def test_failed_login_and_rate_limit_are_logged_without_the_email(auth_db, monkeypatch, caplog):
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_MAX_REQUESTS', 1)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_IP_MAX_REQUESTS', 50)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)
    auth_routes._auth_rate_limit_hits.clear()
    # The `yclients` logger does not propagate to root (see api.py), so attach the capture handler directly.
    auth_logger = logging.getLogger('yclients.auth')
    auth_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level('WARNING', logger='yclients.auth'):
            async with _Api(auth_db) as client:
                first = await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'Wrong12345!'})
                second = await client.post(
                    '/auth/login', json={'email': 'admin@example.com', 'password': 'Wrong12345!'}
                )
    finally:
        auth_logger.removeHandler(caplog.handler)
        auth_routes._auth_rate_limit_hits.clear()

    assert (first.status_code, second.status_code) == (401, 429)
    messages = [record.getMessage() for record in caplog.records if record.name == 'yclients.auth']
    assert messages == ['login failed user_id=1', 'auth rate limit hit route=/auth/login bucket=email_ip']
    assert all('admin@example.com' not in message and 'Wrong12345!' not in message for message in messages)


# ---------- AUTH-9: comparisons that cannot raise ----------

def test_secure_equals_accepts_non_ascii_and_missing_input():
    from sync_security import secure_equals, validate_sync_token

    assert secure_equals('secret', 'secret') is True
    assert secure_equals('secrét', 'secret') is False
    assert secure_equals(None, 'secret') is False
    assert secure_equals('секрет', 'секрет') is True
    with pytest.raises(HTTPException) as exc_info:
        validate_sync_token('é', 'configured')
    assert exc_info.value.status_code == 401


def test_csrf_check_answers_403_for_non_ascii_tokens():
    class _Req:
        method = 'POST'
        cookies = {ACCESS_COOKIE_NAME: 'x', AUTH_CSRF_COOKIE_NAME: 'é'}
        headers = {'x-csrf-token': 'abc'}

    with pytest.raises(HTTPException) as exc_info:
        auth_sessions.enforce_csrf(_Req())
    assert exc_info.value.status_code == 403


# ---------- AUTH-2: peers are managed only inside the actor's own branches ----------

def test_can_manage_peer_only_when_every_peer_branch_is_the_actors():
    from auth_hierarchy import can_list_user, can_manage_user

    # Overlap alone still lists the peer ...
    assert can_list_user('branch_admin', [1, 2], 'branch_admin', [2, 3]) is True
    # ... but a peer with a branch the actor does not hold cannot be changed or deleted.
    assert can_manage_user('branch_admin', [1, 2], 'branch_admin', [2, 3]) is False
    assert can_manage_user('branch_admin', [1, 2], 'branch_admin', [2]) is True
    assert can_manage_user('branch_admin', [1, 2], 'manager', [3]) is False
    assert can_manage_user('branch_admin', [1, 2], 'manager', [1]) is True
    assert can_manage_user('owner', [1], 'owner', [2]) is True
    assert can_manage_user('platform_admin', [], 'platform_admin', []) is True
    assert can_manage_user('manager', [1], 'manager', [1]) is False


@pytest.mark.asyncio
async def test_branch_admin_cannot_take_over_a_peer_with_extra_branches(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(
        PortalUser(
            id=30,
            portal_account_id=1,
            email='peer@example.com',
            password_hash=hash_password('Peer12345!'),
            full_name='Peer',
            role='branch_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add_all([PortalUserBranch(user_id=30, company_id=1), PortalUserBranch(user_id=30, company_id=2)])
    await auth_db.commit()
    headers = await _bearer(auth_db, 3, 'branch_admin')  # holds branch 1 only

    async with _Api(auth_db) as client:
        listed = await client.get('/auth/admin/users', headers=headers)
        patched = await client.patch('/auth/admin/users/30', headers=headers, json={'email': 'attacker@example.com'})
        deleted = await client.delete('/auth/admin/users/30', headers=headers)

    peer_row = next(item for item in listed.json()['data'] if item['id'] == 30)
    assert peer_row['manageable'] is False
    assert patched.status_code == 403
    assert deleted.status_code == 403
    assert await auth_db.scalar(select(PortalUser.email).where(PortalUser.id == 30)) == 'peer@example.com'


# ---------- AUTH-12: branches handed to a user stay inside that user's tenant ----------

@pytest_asyncio.fixture
async def two_tenants(auth_db):
    auth_db.add(Company(id=3, title='Other tenant branch', group_id=1))
    auth_db.add(PortalAccount(id=2, label='Tenant B', created_at=datetime.utcnow()))
    auth_db.add(PortalBranch(portal_account_id=2, company_id=3))
    auth_db.add(
        PortalUser(
            id=53,
            portal_account_id=None,
            email='platform.tenancy@example.com',
            password_hash=hash_password('Platform12345!'),
            full_name='Platform Admin',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    auth_db.add(Staff(id=9300, name='Floater', position='master', company_id=1, fired=0, bookable=True))
    await auth_db.commit()
    return auth_db


@pytest.mark.asyncio
async def test_platform_admin_cannot_attach_another_tenants_branch_to_a_user(two_tenants, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    headers = await _bearer(two_tenants, 53, 'platform_admin')
    async with _Api(two_tenants) as client:
        foreign = await client.patch('/auth/admin/users/2', headers=headers, json={'company_ids': [3]})
        mixed = await client.patch('/auth/admin/users/2', headers=headers, json={'company_ids': [1, 3]})
        own = await client.patch('/auth/admin/users/2', headers=headers, json={'company_ids': [2]})

    assert foreign.status_code == 403
    assert mixed.status_code == 403
    assert own.status_code == 200
    rows = (
        await two_tenants.execute(select(PortalUserBranch.company_id).where(PortalUserBranch.user_id == 2))
    ).scalars()
    assert sorted(rows) == [2]


@pytest.mark.asyncio
async def test_platform_admin_cannot_move_a_staff_row_to_another_tenant(two_tenants, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    headers = {**(await _bearer(two_tenants, 53, 'platform_admin')), 'X-Portal-Account-Id': '1'}
    async with _Api(two_tenants) as client:
        foreign = await client.patch(
            '/auth/admin/staff/9300', headers=headers, json={'full_name': 'Floater', 'company_id': 3}
        )
        own = await client.patch(
            '/auth/admin/staff/9300', headers=headers, json={'full_name': 'Floater', 'company_id': 2}
        )

    assert foreign.status_code == 403
    assert own.status_code == 200
    assert await two_tenants.scalar(select(Staff.company_id).where(Staff.id == 9300)) == 2


@pytest.mark.asyncio
async def test_owner_cannot_attach_a_branch_outside_the_tenant_on_update(two_tenants, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    async with _Api(two_tenants) as client:
        response = await client.patch(
            '/auth/admin/users/2', headers=await _bearer(two_tenants, 1, 'owner'), json={'company_ids': [3]}
        )

    assert response.status_code == 403


# ---------- AUTH-20: bounds ----------

@pytest.mark.asyncio
async def test_admin_update_rejects_oversized_inputs_with_422(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    async with _Api(auth_db) as client:
        long_name = await client.patch(
            '/auth/admin/users/2', headers=await _bearer(auth_db, 1, 'owner'), json={'full_name': 'x' * 256}
        )
        many_ids = await client.patch(
            '/auth/admin/users/2', headers=await _bearer(auth_db, 1, 'owner'), json={'company_ids': list(range(1, 202))}
        )

    assert long_name.status_code == 422
    assert many_ids.status_code == 422


# ---------- AUTH-5: e-mail tokens are single-use and only the newest one lives ----------

@pytest.mark.asyncio
async def test_email_token_is_single_use(auth_db):
    from auth_service import consume_email_token

    raw = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)
    first = await consume_email_token(auth_db, raw, TOKEN_PURPOSE_RESET)
    second = await consume_email_token(auth_db, raw, TOKEN_PURPOSE_RESET)

    assert first is not None and first.id == 1
    assert second is None


@pytest.mark.asyncio
async def test_a_failure_after_spending_the_token_does_not_burn_it(auth_db):
    from auth_service import consume_email_token

    raw = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)
    assert await consume_email_token(auth_db, raw, TOKEN_PURPOSE_RESET) is not None
    await auth_db.rollback()  # what closing the request session does when the follow-up write fails

    assert await consume_email_token(auth_db, raw, TOKEN_PURPOSE_RESET) is not None


@pytest.mark.asyncio
async def test_email_token_purpose_expiry_and_inactive_user_are_refused(auth_db):
    from auth_service import consume_email_token

    raw = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)
    assert await consume_email_token(auth_db, raw, 'verify') is None

    await auth_db.execute(
        PortalEmailToken.__table__.update().where(PortalEmailToken.user_id == 1).values(expires_at=datetime.utcnow())
    )
    await auth_db.commit()
    assert await consume_email_token(auth_db, raw, TOKEN_PURPOSE_RESET) is None

    fresh = await create_email_token(auth_db, 2, TOKEN_PURPOSE_RESET)
    manager = await auth_db.get(PortalUser, 2)
    manager.is_active = False
    await auth_db.commit()
    assert await consume_email_token(auth_db, fresh, TOKEN_PURPOSE_RESET) is None
    # A refused consume leaves the token untouched.
    assert await auth_db.scalar(select(PortalEmailToken.used_at).where(PortalEmailToken.user_id == 2)) is None


@pytest.mark.asyncio
async def test_issuing_a_new_token_invalidates_older_ones_of_the_same_purpose(auth_db):
    from auth_service import consume_email_token

    old_reset = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)
    verify = await create_email_token(auth_db, 1, 'verify')
    other_user = await create_email_token(auth_db, 2, TOKEN_PURPOSE_RESET)
    new_reset = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)

    assert await consume_email_token(auth_db, old_reset, TOKEN_PURPOSE_RESET) is None
    assert await consume_email_token(auth_db, new_reset, TOKEN_PURPOSE_RESET) is not None
    assert await consume_email_token(auth_db, verify, 'verify') is not None
    assert await consume_email_token(auth_db, other_user, TOKEN_PURPOSE_RESET) is not None


@pytest.mark.asyncio
async def test_successful_reset_invalidates_the_other_reset_tokens(auth_db, monkeypatch):
    from auth_service import _token_hash

    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    raw = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)
    # A second live token can only come from another path than create_email_token.
    auth_db.add(
        PortalEmailToken(
            user_id=1,
            token_hash=_token_hash('stray-reset-token-1234'),
            purpose=TOKEN_PURPOSE_RESET,
            expires_at=datetime.utcnow() + timedelta(hours=1),
            created_at=datetime.utcnow(),
        )
    )
    await auth_db.commit()
    async with _Api(auth_db) as client:
        reset = await client.post('/auth/reset-password', json={'token': raw, 'password': 'Changed12345!'})
        replay = await client.post('/auth/reset-password', json={'token': raw, 'password': 'Another12345!'})
        stray = await client.post(
            '/auth/reset-password', json={'token': 'stray-reset-token-1234', 'password': 'Another12345!'}
        )

    assert reset.status_code == 200
    assert replay.status_code == 400
    assert stray.status_code == 400


@pytest.mark.asyncio
async def test_change_password_invalidates_outstanding_reset_tokens(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    raw = await create_email_token(auth_db, 1, TOKEN_PURPOSE_RESET)
    async with _Api(auth_db) as client:
        changed = await client.post(
            '/auth/change-password',
            headers=await _bearer(auth_db, 1, 'owner'),
            json={'current_password': 'Admin12345!', 'new_password': 'Changed12345!'},
        )
        reset = await client.post('/auth/reset-password', json={'token': raw, 'password': 'Another12345!'})

    assert changed.status_code == 200
    assert reset.status_code == 400


# ---------- AUTH-4: SMTP leaves the event loop ----------

@pytest.mark.asyncio
async def test_email_is_sent_off_the_event_loop_thread(auth_db, monkeypatch):
    import threading

    import auth_service

    sent_from = []
    monkeypatch.setattr(auth_service, 'send_auth_email', lambda *args: sent_from.append(threading.get_ident()))
    user = await auth_db.get(PortalUser, 1)
    await auth_service.send_password_reset_email(auth_db, user)
    await auth_service.send_account_invite_email(auth_db, user)

    assert len(sent_from) == 2
    assert threading.get_ident() not in sent_from


# ---------- AUTH-19: the password CLI signs the user out everywhere ----------

def test_set_portal_password_script_revokes_sessions(monkeypatch):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    import set_portal_password

    # One shared connection, so the attached `system` schema survives between sessions.
    engine = create_engine('sqlite:///:memory:', poolclass=StaticPool, connect_args={'check_same_thread': False})
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
        PortalUser.__table__.create(conn)
        PortalRefreshToken.__table__.create(conn)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            PortalUser(
                id=1,
                portal_account_id=None,
                email='cli@example.com',
                password_hash=hash_password('OldPassword123!'),
                role='platform_admin',
                is_active=True,
                token_version=4,
                created_at=datetime.utcnow(),
            )
        )
        session.add(
            PortalRefreshToken(
                user_id=1,
                token_hash='h' * 64,
                expires_at=datetime.utcnow() + timedelta(days=1),
                created_at=datetime.utcnow(),
            )
        )
        session.commit()

    class _Database:
        def test_connection(self):
            return True

        def get_db(self):
            return factory()

    monkeypatch.setattr(set_portal_password, 'init_database', lambda *args: _Database())
    monkeypatch.setattr(
        'sys.argv', ['set_portal_password', '--email', 'CLI@example.com', '--password', 'NewPassword123!']
    )
    assert set_portal_password.main() == 0

    with factory() as session:
        user = session.get(PortalUser, 1)
        assert user.token_version == 5
        assert user.password_changed_at is not None
        assert session.scalar(select(func.count()).select_from(PortalRefreshToken)) == 0
    from auth_service import verify_password

    assert verify_password('NewPassword123!', user.password_hash)

    monkeypatch.setattr('sys.argv', ['set_portal_password', '--email', 'cli@example.com', '--password', LONG_PASSWORD])
    assert set_portal_password.main() == 2


# --------------------------------------------------------------------------- #
# W3-A1: trusted client IP, rate-limit bookkeeping, session hardening, open paths #
# --------------------------------------------------------------------------- #

PROXY_SECRET = 'p' * 40


def _request(headers: dict[str, str] | None = None, client_host: str = '198.51.100.10') -> Request:
    return Request({
        'type': 'http',
        'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        'client': (client_host, 12345),
        'scheme': 'https',
        'server': ('test', 443),
    })


def test_client_ip_ignores_forwarded_for_without_the_proxy_secret(monkeypatch):
    monkeypatch.setattr(auth_sessions, 'PROXY_SHARED_SECRET', '')
    assert auth_sessions.client_ip(_request({'x-forwarded-for': '203.0.113.9'})) == '198.51.100.10'
    # A secret presented while none is configured must not unlock the header either.
    assert auth_sessions.client_ip(
        _request({'x-forwarded-for': '203.0.113.9', 'x-proxy-secret': PROXY_SECRET})
    ) == '198.51.100.10'

    monkeypatch.setattr(auth_sessions, 'PROXY_SHARED_SECRET', PROXY_SECRET)
    assert auth_sessions.client_ip(_request({'x-forwarded-for': '203.0.113.9'})) == '198.51.100.10'


def test_client_ip_honours_forwarded_for_with_the_proxy_secret(monkeypatch):
    monkeypatch.setattr(auth_sessions, 'PROXY_SHARED_SECRET', PROXY_SECRET)
    request = _request({'x-forwarded-for': '203.0.113.9, 10.0.0.1', 'x-proxy-secret': PROXY_SECRET})
    assert auth_sessions.client_ip(request) == '203.0.113.9'
    # Secret right but nothing forwarded: fall back to the peer.
    assert auth_sessions.client_ip(_request({'x-proxy-secret': PROXY_SECRET})) == '198.51.100.10'


def test_client_ip_ignores_forwarded_for_with_a_wrong_or_non_ascii_secret(monkeypatch):
    monkeypatch.setattr(auth_sessions, 'PROXY_SHARED_SECRET', PROXY_SECRET)
    for wrong in ('q' * 40, PROXY_SECRET[:-1], 'é' * 40):
        request = _request({'x-forwarded-for': '203.0.113.9', 'x-proxy-secret': wrong})
        assert auth_sessions.client_ip(request) == '198.51.100.10'


@pytest.mark.asyncio
async def test_session_ip_hash_follows_the_trusted_client_ip(auth_db, monkeypatch):
    monkeypatch.setattr(auth_sessions, 'PROXY_SHARED_SECRET', PROXY_SECRET)
    user = await auth_db.get(PortalUser, 1)
    forged = _request({'x-forwarded-for': '203.0.113.9', 'x-proxy-secret': 'wrong'})
    trusted = _request({'x-forwarded-for': '203.0.113.9', 'x-proxy-secret': PROXY_SECRET})
    await auth_sessions.issue_session(auth_db, user, forged, access_token_fn=lambda *_: 'a')
    await auth_sessions.issue_session(auth_db, user, trusted, access_token_fn=lambda *_: 'a')
    hashes = set((await auth_db.scalars(select(PortalRefreshToken.ip_hash))).all())
    assert hashes == {auth_sessions._hash_ip('198.51.100.10'), auth_sessions._hash_ip('203.0.113.9')}


@pytest.mark.asyncio
async def test_login_rate_limit_buckets_visitors_behind_the_trusted_proxy_separately(auth_db, monkeypatch):
    monkeypatch.setattr(auth_sessions, 'PROXY_SHARED_SECRET', PROXY_SECRET)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_MAX_REQUESTS', 100)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_IP_MAX_REQUESTS', 2)
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app, client=('198.51.100.10', 12345))

    async def login(visitor: str, secret: str = PROXY_SECRET):
        return await client.post(
            '/auth/login',
            json={'email': 'nobody@example.com', 'password': 'wrong'},
            headers={'X-Forwarded-For': visitor, 'X-Proxy-Secret': secret},
        )

    async with AsyncClient(transport=transport, base_url='http://test') as client:
        noisy = [(await login('203.0.113.1')).status_code for _ in range(3)]
        other_visitor = await login('203.0.113.2')
        # A wrong secret falls back to the socket peer, whatever X-Forwarded-For claims.
        forged = [(await login(f'203.0.113.{n}', 'wrong')).status_code for n in range(10, 13)]

    app.dependency_overrides.clear()
    assert noisy == [401, 401, 429]
    assert other_visitor.status_code == 401
    assert forged == [401, 401, 429]


def test_rate_limit_check_prunes_only_the_touched_key(monkeypatch):
    auth_routes._auth_rate_limit_hits.clear()
    auth_routes._demo_login_hits.clear()
    monkeypatch.setattr(auth_routes, '_last_rate_limit_sweep', 1000.0)
    idle_key = ('ip', 'login', '203.0.113.50', '')
    auth_routes._auth_rate_limit_hits[idle_key] = [1.0]
    touched = ('ip', 'login', '203.0.113.51', '')
    auth_routes._auth_rate_limit_hits[touched] = [1.0, 1005.0]

    # Inside the sweep interval: the untouched stale key stays, the touched one is trimmed.
    auth_routes._check_rate_limit_bucket(touched, 5, 60.0, 1010.0)
    auth_routes._sweep_idle_rate_limit_keys(1010.0)

    assert idle_key in auth_routes._auth_rate_limit_hits
    assert auth_routes._auth_rate_limit_hits[touched] == [1005.0, 1010.0]


def test_rate_limit_sweep_drops_idle_keys_of_both_limiters_periodically(monkeypatch):
    auth_routes._auth_rate_limit_hits.clear()
    auth_routes._demo_login_hits.clear()
    monkeypatch.setattr('auth_routes.AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)
    monkeypatch.setattr(auth_routes, '_last_rate_limit_sweep', 0.0)
    auth_routes._auth_rate_limit_hits[('ip', 'login', 'old', '')] = [100.0]
    auth_routes._auth_rate_limit_hits[('ip', 'login', 'fresh', '')] = [990.0]
    auth_routes._demo_login_hits['old'] = [100.0]
    auth_routes._demo_login_hits['fresh'] = [995.0]

    auth_routes._sweep_idle_rate_limit_keys(1000.0)

    assert list(auth_routes._auth_rate_limit_hits) == [('ip', 'login', 'fresh', '')]
    assert list(auth_routes._demo_login_hits) == ['fresh']
    # A second call inside the interval does no work (a key added now survives untouched).
    auth_routes._demo_login_hits['stale-again'] = [1.0]
    auth_routes._sweep_idle_rate_limit_keys(1001.0)
    assert 'stale-again' in auth_routes._demo_login_hits


@pytest.mark.asyncio
async def test_demo_sessions_are_capped_to_the_newest(auth_db, monkeypatch):
    monkeypatch.setattr(auth_sessions, 'DEMO_MAX_ACTIVE_SESSIONS', 3)
    demo_id = await _seed_demo_owner(auth_db)
    demo = await auth_db.get(PortalUser, demo_id)
    ids = []
    for _ in range(5):
        await auth_sessions.issue_session(auth_db, demo, _request(), access_token_fn=lambda *_: 'a')
        await auth_db.commit()
        ids.append(max((await auth_db.scalars(select(PortalRefreshToken.id))).all()))
    remaining = sorted((await auth_db.scalars(
        select(PortalRefreshToken.id).where(PortalRefreshToken.user_id == demo_id)
    )).all())
    assert remaining == ids[-3:]


async def _issue(auth_db, user_id: int = 1):
    user = await auth_db.get(PortalUser, user_id)
    issued = await auth_sessions.issue_session(auth_db, user, _request(), access_token_fn=lambda *_: 'a')
    await auth_db.commit()
    return issued.refresh_token


async def _rotate(auth_db, raw: str):
    _, issued = await auth_sessions.rotate_session(
        auth_db, raw, _request(), access_token_fn=lambda *_: 'a'
    )
    await auth_db.commit()
    return issued.refresh_token


async def _session_row(auth_db):
    await auth_db.rollback()
    return (await auth_db.scalars(select(PortalRefreshToken))).one()


@pytest.mark.asyncio
async def test_refresh_accepts_the_previous_token_once_more_within_the_grace_window(auth_db):
    first = await _issue(auth_db)
    second = await _rotate(auth_db, first)
    # The response carrying `second` was lost: the client retries with `first`.
    third = await _rotate(auth_db, first)

    row = await _session_row(auth_db)
    assert third not in (first, second)
    assert row.token_hash == auth_sessions._hash_token(third)
    assert row.previous_token_hash == auth_sessions._hash_token(first)
    # The lost-response token is gone, the one the client holds works.
    with pytest.raises(HTTPException):
        await _rotate(auth_db, second)
    assert await _rotate(auth_db, third)


@pytest.mark.asyncio
async def test_refresh_replay_after_the_grace_window_revokes_the_session(auth_db, caplog):
    first = await _issue(auth_db)
    second = await _rotate(auth_db, first)
    row = await _session_row(auth_db)
    row.rotated_at = datetime.utcnow() - auth_sessions.REFRESH_ROTATION_GRACE - timedelta(seconds=1)
    await auth_db.commit()

    # The `yclients` logger does not propagate to root (see api.py), so attach the capture handler directly.
    auth_logger = logging.getLogger('yclients.auth')
    auth_logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger='yclients.auth'):
            with pytest.raises(HTTPException) as exc_info:
                await _rotate(auth_db, first)
    finally:
        auth_logger.removeHandler(caplog.handler)

    assert exc_info.value.status_code == 401
    assert (await auth_db.scalar(select(func.count()).select_from(PortalRefreshToken))) == 0
    messages = ' '.join(record.getMessage() for record in caplog.records)
    assert 'replayed' in messages
    assert first not in messages and second not in messages
    # The thief's rotated token died with the session.
    with pytest.raises(HTTPException):
        await _rotate(auth_db, second)


@pytest.mark.asyncio
async def test_refresh_rotation_stops_at_the_absolute_session_age(auth_db, monkeypatch):
    monkeypatch.setattr(auth_sessions, 'AUTH_SESSION_MAX_AGE_DAYS', 90)
    now = datetime.utcnow()
    auth_db.add_all([
        PortalRefreshToken(
            id=701, user_id=1, token_hash=auth_sessions._hash_token('too-old'),
            expires_at=now + timedelta(days=10), last_used_at=now, created_at=now - timedelta(days=91),
        ),
        PortalRefreshToken(
            id=702, user_id=1, token_hash=auth_sessions._hash_token('nearly-old'),
            expires_at=now + timedelta(days=10), last_used_at=now, created_at=now - timedelta(days=80),
        ),
    ])
    await auth_db.commit()

    with pytest.raises(HTTPException) as exc_info:
        await _rotate(auth_db, 'too-old')
    assert exc_info.value.status_code == 401
    assert await auth_db.get(PortalRefreshToken, 701) is None

    await _rotate(auth_db, 'nearly-old')
    row = await auth_db.get(PortalRefreshToken, 702)
    await auth_db.refresh(row)
    # Sliding expiry would be 30 days out; the cap is 10 days out.
    assert abs((row.expires_at - (row.created_at + timedelta(days=90))).total_seconds()) < 1


@pytest.mark.asyncio
async def test_issue_session_never_expires_past_the_absolute_cap(auth_db, monkeypatch):
    monkeypatch.setattr(auth_sessions, 'AUTH_SESSION_MAX_AGE_DAYS', 7)
    await _issue(auth_db)
    row = await _session_row(auth_db)
    assert abs((row.expires_at - (row.created_at + timedelta(days=7))).total_seconds()) < 1


@pytest.mark.asyncio
async def test_logout_others_without_a_refresh_cookie_is_a_400_and_keeps_every_session(auth_db):
    now = datetime.utcnow()
    auth_db.add(PortalRefreshToken(
        id=801, user_id=1, token_hash='bearer-clients-own-session',
        expires_at=now + timedelta(days=10), last_used_at=now, created_at=now,
    ))
    await auth_db.commit()
    token = create_access_token(1, 'owner', 0, 801)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post('/auth/logout-others', headers={'Authorization': f'Bearer {token}'})
    app.dependency_overrides.clear()

    assert response.status_code == 400
    assert await auth_db.get(PortalRefreshToken, 801) is not None


@pytest.mark.asyncio
async def test_refresh_route_retries_with_the_previous_cookie_within_grace(auth_db):
    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        assert (
            await client.post('/auth/login', json={'email': 'admin@example.com', 'password': 'Admin12345!'})
        ).status_code == 200
        first_refresh = client.cookies.get(REFRESH_COOKIE_NAME)
        csrf = client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        assert (await client.post('/auth/refresh', headers={'X-CSRF-Token': csrf})).status_code == 200
        client.cookies.set(REFRESH_COOKIE_NAME, first_refresh, path='/auth')
        csrf = client.cookies.get(AUTH_CSRF_COOKIE_NAME)
        retry = await client.post('/auth/refresh', headers={'X-CSRF-Token': csrf})
    app.dependency_overrides.clear()
    assert retry.status_code == 200


@pytest.mark.asyncio
async def test_authenticated_requests_load_the_user_once_and_know_if_it_is_demo(auth_db):
    demo_id = await _seed_demo_owner(auth_db)
    # Issued before the statement recorder starts: issuing a session writes rows of its own.
    owner = {'Authorization': f'Bearer {await issue_access_token(auth_db, 1, "owner")}'}
    demo = {'Authorization': f'Bearer {await issue_access_token(auth_db, demo_id, "owner")}'}
    statements = []

    def record(_conn, _cursor, statement, _params, _context, _executemany):
        statements.append(statement)

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    event.listen(auth_db.bind.sync_engine, 'before_cursor_execute', record)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            me = await client.get('/auth/me', headers=owner)
            user_selects = [s for s in statements if s.startswith('SELECT') and 'FROM system.portal_users' in s]
            me_statements = len(statements)

            statements.clear()
            logout_all = await client.post('/auth/logout-all', headers=owner)
            logout_statements = len(statements)

            statements.clear()
            blocked = await client.post('/auth/change-password', headers=demo, json={})
            demo_user_selects = [s for s in statements if 'FROM system.portal_users' in s]
    finally:
        event.remove(auth_db.bind.sync_engine, 'before_cursor_execute', record)
        app.dependency_overrides.clear()

    assert me.status_code == 200
    # Was 6 statements (the context and the user row loaded three times); now one user select.
    assert len(user_selects) == 1
    assert me_statements == 3
    assert logout_all.status_code == 200
    # Was 6: forbid_demo decoded the token and selected is_demo, get_current_user resolved again.
    assert logout_statements == 4
    assert blocked.status_code == 403
    assert len(demo_user_selects) == 1


@pytest.mark.asyncio
async def test_access_context_carries_is_demo(auth_db):
    demo_id = await _seed_demo_owner(auth_db)
    from auth_deps import _user_from_token

    demo_user, demo_ctx = await _user_from_token(await issue_access_token(auth_db, demo_id, 'owner'), auth_db)
    owner_user, owner_ctx = await _user_from_token(await issue_access_token(auth_db, 1, 'owner'), auth_db)
    assert demo_ctx.is_demo is True and demo_user.id == demo_id
    assert owner_ctx.is_demo is False and owner_user.id == 1


def test_open_paths_are_exact_and_unchanged():
    from open_paths import OPEN_PATHS

    public_actions = (
        'register', 'login', 'demo-login', 'refresh', 'logout',
        'verify-email', 'forgot-password', 'reset-password', 'resend-verification',
    )
    expected = {'/health', '/health/ready', '/openapi.json', '/docs', '/redoc'} | {
        f'{prefix}/{action}' for prefix in ('/auth', '/dashboard/auth') for action in public_actions
    }
    assert OPEN_PATHS == expected
    # The two sessions routes that used to ride on the `/auth/logout` prefix are protected now.
    for path in ('/auth/logout-all', '/auth/logout-others', '/dashboard/auth/logout-all', '/auth/login/extra'):
        assert path not in OPEN_PATHS


@pytest.mark.asyncio
async def test_every_route_open_before_stays_open_and_nothing_else_opens(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr('auth_deps.API_KEY', '')
    from open_paths import OPEN_PATHS

    # Routes that answered without credentials before the change: the prefix list, minus the
    # two logout variants that always needed a user anyway.
    open_before = {
        route.path
        for route in app.routes
        if getattr(route, 'path', '') in OPEN_PATHS
    }
    assert {'/auth/login', '/auth/refresh', '/auth/logout', '/dashboard/auth/demo-login', '/health'} <= open_before

    async def override_db():
        yield auth_db

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        for path in (
            '/auth/logout-all',
            '/auth/logout-others',
            '/dashboard/auth/logout-all',
            '/auth/me',
            '/auth/sessions',
        ):
            method = client.get if path in ('/auth/me', '/auth/sessions') else client.post
            response = await method(path)
            assert response.status_code == 401, (path, response.status_code)
        # `/auth/logout` stays open: a client with an expired session can still clear cookies.
        assert (await client.post('/auth/logout')).status_code == 200
    app.dependency_overrides.clear()


# ---------- Admin lifecycle: deleting, demoting and re-addressing users ----------

@pytest.mark.asyncio
async def test_deleting_a_provisioned_user_releases_the_crm_staff_row(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    auth_db.add(Staff(id=9003, name='Worker', position='master', company_id=1, fired=0, bookable=True))
    await auth_db.commit()
    headers = await _bearer(auth_db, 1, 'owner')
    invite = {'role': 'barber', 'email': 'worker9003@example.com'}

    async with _Api(auth_db) as client:
        created = await client.post('/auth/admin/staff/9003/create-account', headers=headers, json=invite)
        assert created.status_code == 200
        assert (await client.delete('/auth/admin/users/9003', headers=headers)).status_code == 200
        released = (await auth_db.execute(select(Staff.portal_user_id, Staff.fired).where(Staff.id == 9003))).one()
        listed = await client.get('/auth/admin/users', headers=headers)
        again = await client.post('/auth/admin/staff/9003/create-account', headers=headers, json=invite)

    assert tuple(released) == (None, 0)
    assert any(row.get('staff_id') == 9003 and not row['is_portal_user'] for row in listed.json()['data'])
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_demoting_an_owner_needs_explicit_branches(two_tenants, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    headers = await _bearer(two_tenants, 53, 'platform_admin')
    async with _Api(two_tenants) as client:
        bare = await client.patch('/auth/admin/users/1', headers=headers, json={'role': 'manager'})
        assert (await two_tenants.scalar(select(PortalUser.role).where(PortalUser.id == 1))) == 'owner'
        with_branches = await client.patch(
            '/auth/admin/users/1', headers=headers, json={'role': 'manager', 'company_ids': [1]}
        )

    assert bare.status_code == 400
    assert with_branches.status_code == 200
    assert await two_tenants.scalar(select(PortalUserBranch.company_id).where(PortalUserBranch.user_id == 1)) == 1


@pytest.mark.asyncio
async def test_staff_outside_the_actors_branches_is_not_found_for_every_role(two_tenants, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    two_tenants.add_all([
        Staff(id=9301, name='Elsewhere', position='master', company_id=2, fired=0, bookable=True),
        Staff(id=9302, name='Other tenant', position='master', company_id=3, fired=0, bookable=True),
    ])
    await two_tenants.commit()
    branch_admin = await _bearer(two_tenants, 3, 'branch_admin')  # branch 1 only
    owner = await _bearer(two_tenants, 1, 'owner')  # branches 1 and 2
    async with _Api(two_tenants) as client:
        results = [
            await client.delete('/auth/admin/staff/9301', headers=branch_admin),
            await client.post('/auth/admin/staff/9301/create-account', headers=branch_admin, json={'role': 'barber'}),
            await client.delete('/auth/admin/staff/9302', headers=owner),
            await client.delete('/auth/admin/staff/9999', headers=owner),
        ]

    assert [response.status_code for response in results] == [404, 404, 404, 404]
    assert len({response.json()['detail'] for response in results}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [{'email': 'new.manager@example.com'}, {'is_active': False}])
async def test_changing_a_users_address_or_disabling_them_ends_their_sessions(auth_db, monkeypatch, change):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    old_token = await issue_access_token(auth_db, 2, 'manager')
    reset_token = await create_email_token(auth_db, 2, TOKEN_PURPOSE_RESET)
    async with _Api(auth_db) as client:
        assert (await client.get('/auth/me', headers={'Authorization': f'Bearer {old_token}'})).status_code == 200
        patched = await client.patch('/auth/admin/users/2', headers=await _bearer(auth_db, 1, 'owner'), json=change)
        after = await client.get('/auth/me', headers={'Authorization': f'Bearer {old_token}'})

    assert patched.status_code == 200
    assert after.status_code == 401
    assert await auth_db.scalar(select(PortalRefreshToken.id).where(PortalRefreshToken.user_id == 2)) is None
    if 'email' in change:
        from auth_service import consume_email_token
        assert await consume_email_token(auth_db, reset_token, TOKEN_PURPOSE_RESET) is None


@pytest.mark.asyncio
async def test_a_role_or_branch_change_keeps_the_users_sessions(auth_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    token = await issue_access_token(auth_db, 2, 'manager')
    async with _Api(auth_db) as client:
        patched = await client.patch(
            '/auth/admin/users/2', headers=await _bearer(auth_db, 1, 'owner'), json={'company_ids': [1, 2]}
        )
        after = await client.get('/auth/me', headers={'Authorization': f'Bearer {token}'})

    assert patched.status_code == 200
    assert after.status_code == 200


@pytest.mark.asyncio
async def test_the_user_list_names_the_live_staff_row_of_a_user_with_several(auth_db):
    from auth_routes import _load_staff_ids_by_portal_user

    auth_db.add_all([
        Staff(id=9400, name='Left', position='master', company_id=1, fired=1, portal_user_id=2),
        Staff(id=9600, name='Second branch', position='master', company_id=2, fired=0, portal_user_id=2),
        Staff(id=9500, name='First branch', position='master', company_id=1, fired=0, portal_user_id=2),
    ])
    await auth_db.commit()

    assert await _load_staff_ids_by_portal_user(auth_db, [2]) == {2: 9500}
