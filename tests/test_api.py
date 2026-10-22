import sys
import types

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from clock import utcnow
import api
from api import app
from conftest import issue_access_token
from helpers import cached_password_hash
from models import (
    Company,
    Group,
    PortalAccount,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    SyncJob,
    YClientsCredential,
)


@pytest.mark.asyncio
async def test_api_key_blocks_unauthorized_requests(async_session, monkeypatch):
    monkeypatch.setattr(api, 'API_KEY', 'secret123')
    import auth_deps

    monkeypatch.setattr(auth_deps, 'API_KEY', 'secret123')
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r1 = await client.get('/dashboard/branches')
        assert r1.status_code == 401

        r2 = await client.get('/dashboard/branches', headers={'X-API-Key': 'wrong'})
        assert r2.status_code == 401

        r3 = await client.get('/dashboard/branches', headers={'X-API-Key': 'secret123'})
        assert r3.status_code == 200

        r4 = await client.get('/health')
        assert r4.status_code == 200

    app.dependency_overrides.clear()
    monkeypatch.setattr(api, 'API_KEY', '')
    monkeypatch.setattr(auth_deps, 'API_KEY', '')
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['incremental', 'refresh', 'full'])
async def test_sync_trigger_queues_a_job_for_every_sync_mode(async_session, monkeypatch, mode):
    """The dashboard must be able to request each mode the pipeline implements."""

    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/sync/trigger',
                json={'mode': mode, 'initiator': 'dashboard'},
                headers={'X-Sync-Token': 'test-sync-token'},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    job = (await async_session.execute(select(SyncJob))).scalar_one()
    assert response.json() == {
        'status': 'queued',
        'job_id': job.id,
        'mode': mode,
        'initiator': 'dashboard',
        'portal_account_id': None,
        'credential_id': None,
        'company_ids': [],
    }
    assert (job.mode, job.initiator, job.status) == (mode, 'dashboard', 'queued')


@pytest.mark.asyncio
async def test_sync_trigger_rejects_unconfigured_token(async_session, monkeypatch):
    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', '')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        no_header = await client.post('/sync/trigger', json={'mode': 'incremental'})
        empty_header = await client.post(
            '/sync/trigger',
            json={'mode': 'incremental'},
            headers={'X-Sync-Token': ''},
        )
        arbitrary_header = await client.post(
            '/sync/trigger',
            json={'mode': 'incremental'},
            headers={'X-Sync-Token': 'anything'},
        )

    app.dependency_overrides.clear()

    assert no_header.status_code == 401
    assert empty_header.status_code == 401
    assert arbitrary_header.status_code == 401


@pytest.mark.asyncio
async def test_sync_routes_reject_missing_or_wrong_token(async_session, monkeypatch):
    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        missing_trigger = await client.post('/sync/trigger', json={'mode': 'incremental'})
        wrong_trigger = await client.post(
            '/sync/trigger',
            json={'mode': 'incremental'},
            headers={'X-Sync-Token': 'wrong'},
        )
        missing_status = await client.get('/sync/status')
        wrong_status = await client.get('/sync/status', headers={'X-Sync-Token': 'wrong'})

    app.dependency_overrides.clear()

    assert missing_trigger.status_code == 401
    assert wrong_trigger.status_code == 401
    assert missing_status.status_code == 401
    assert wrong_status.status_code == 401


@pytest.mark.asyncio
async def test_sync_status_reports_the_sync_state_and_the_queue(async_session, monkeypatch):
    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    # The sync state lives behind a separate synchronous engine.
    monkeypatch.setattr(api, 'get_sync_status', lambda: {'status': 'idle'})
    app.dependency_overrides[api.get_async_db] = override_db
    headers = {'X-Sync-Token': 'test-sync-token'}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            empty = await client.get('/sync/status', headers=headers)
            await client.post('/sync/trigger', json={'mode': 'refresh'}, headers=headers)
            queued = await client.get('/sync/status', headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert empty.status_code == 200
    assert empty.json()['sync'] == {'status': 'idle'}
    assert empty.json()['queue']['queued_jobs'] == 0
    assert queued.json()['queue']['queued_jobs'] == 1
    assert queued.json()['queue']['current_job']['mode'] == 'refresh'


def test_cli_accepts_every_sync_mode(monkeypatch):
    """The runner systemd calls must understand the mode its unit passes."""
    import main

    for mode in ('incremental', 'refresh', 'full'):
        monkeypatch.setattr(sys, 'argv', ['main.py', '--mode', mode])
        assert main.parse_args().mode == mode

    monkeypatch.setattr(sys, 'argv', ['main.py', '--mode', 'bogus'])
    with pytest.raises(SystemExit) as exit_info:
        main.parse_args()
    # argparse exits 2 for usage errors, so "already running" must not also be 2.
    assert exit_info.value.code == 2
    assert main.ALREADY_RUNNING_EXIT_CODE != 2


@pytest.mark.asyncio
async def test_liveness_answers_without_touching_the_database():
    """Liveness must stay cheap: the container healthcheck should not restart a working
    API because the database blinked, and CI has no database at all."""
    app.dependency_overrides.pop(api.get_async_db, None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/health')

    assert response.status_code == 200
    assert response.json() == {'status': 'ok'}


@pytest.mark.asyncio
async def test_readiness_reports_the_database(async_session):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/health/ready')
    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {'status': 'ok', 'database': 'ok'}


@pytest.mark.asyncio
async def test_readiness_fails_when_the_database_cannot_answer():
    """The async engine is lazy, so a broken connection setting leaves liveness green
    while every route returns 500. Readiness is what stops that build from shipping."""

    class DeadSession:
        async def execute(self, *args, **kwargs):
            raise OSError('connection refused')

    async def override_db():
        yield DeadSession()

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/health/ready')
    app.dependency_overrides.clear()

    assert response.status_code == 503
    assert response.json() == {'status': 'degraded', 'database': 'error'}


@pytest.mark.asyncio
async def test_dashboard_requests_are_timed_and_name_the_report(async_session, monkeypatch):
    """The timing log is the only way to answer "did anything get slower" after a change.
    It stayed silent for the whole of its existence because nothing configured a handler,
    and it named no report because every report shares one path."""
    import logging

    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger('yclients.api')
    handler = Collector()
    logger.addHandler(handler)
    monkeypatch.setattr(logger, 'level', logging.INFO)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url='http://test') as client:
            await client.get('/health')
            await client.get('/dashboard/branches')
            await client.get('/dashboard/reports/data', params={
                'report_id': 'staff_leaderboard',
                'start_date': '2025-01-01',
                'end_date': '2025-01-31',
            })
    finally:
        logger.removeHandler(handler)
        app.dependency_overrides.clear()

    messages = [record.getMessage() for record in records if 'dashboard_api_request' in record.getMessage()]
    assert any('path=/dashboard/branches report_id=-' in message for message in messages), messages
    assert any(
        'path=/dashboard/reports/data report_id=staff_leaderboard' in message
        for message in messages
    ), messages
    assert all('duration_ms=' in message for message in messages)
    assert not any('path=/health' in message for message in messages), messages


@pytest.mark.asyncio
async def test_sync_trigger_bounds_free_text_and_lists(async_session, monkeypatch):
    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    app.dependency_overrides[api.get_async_db] = override_db
    headers = {'X-Sync-Token': 'test-sync-token'}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            long_initiator = await client.post(
                '/sync/trigger', json={'initiator': 'x' * 10_000, 'global_sync': True}, headers=headers
            )
            long_list = await client.post(
                '/sync/trigger', json={'company_ids': list(range(10_000)), 'global_sync': True}, headers=headers
            )
    finally:
        app.dependency_overrides.clear()

    assert long_initiator.status_code == 422
    assert long_list.status_code == 422


def _portal_user(user_id: int, role: str, branches: list[int]) -> list:
    return [
        PortalUser(
            id=user_id,
            portal_account_id=1,
            email=f'user{user_id}@example.com',
            password_hash=cached_password_hash('Password123!'),
            full_name=f'User {user_id}',
            role=role,
            is_active=True,
            email_verified_at=utcnow(),
            created_at=utcnow(),
        ),
        *[PortalUserBranch(user_id=user_id, company_id=company_id) for company_id in branches],
    ]


def _credential(credential_id: int, portal_account_id: int) -> YClientsCredential:
    now = utcnow()
    return YClientsCredential(
        id=credential_id,
        portal_account_id=portal_account_id,
        title=f'Credential {credential_id}',
        partner_token_encrypted='x',
        login_encrypted='x',
        password_encrypted='x',
        created_at=now,
        updated_at=now,
    )


@pytest.mark.asyncio
async def test_sync_trigger_credential_must_belong_to_the_callers_tenant(async_session, monkeypatch):
    """A foreign or missing credential id used to reach the FK as a bare 500 (or sit in the audit trail)."""
    import auth_deps

    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    async_session.add_all([
        Group(id=1, title='G'),
        Company(id=1, title='A', group_id=1),
        Company(id=2, title='B', group_id=1),
        PortalAccount(id=1, label='Tenant A', created_at=utcnow()),
        PortalAccount(id=2, label='Tenant B', created_at=utcnow()),
        PortalBranch(portal_account_id=1, company_id=1),
        PortalBranch(portal_account_id=2, company_id=2),
        *_portal_user(210, 'owner', [1]),
        _credential(1, 1),
        _credential(2, 2),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    headers = {
        'Authorization': f'Bearer {await issue_access_token(async_session, 210, "owner")}',
        'X-Sync-Token': 'test-sync-token',
    }
    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            own = await client.post('/sync/trigger', json={'credential_id': 1}, headers=headers)
            foreign = await client.post('/sync/trigger', json={'credential_id': 2}, headers=headers)
            missing = await client.post('/sync/trigger', json={'credential_id': 999}, headers=headers)
    finally:
        app.dependency_overrides.clear()
        monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    assert own.status_code == 200, own.text
    assert own.json()['credential_id'] == 1
    assert foreign.status_code == 404
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_request_log_cannot_be_forged_through_the_path_or_report_id(async_session):
    import logging

    records = []

    class Collector(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger('yclients.api')
    handler = Collector()
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            await client.get(
                '/dashboard/reports/data',
                params={'report_id': 'a\n2026-10-11 00:00:00 INFO yclients.auth FORGED login ok'},
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        app.dependency_overrides.clear()

    timed = [message for message in records if 'dashboard_api_request' in message]
    assert len(timed) == 1, timed
    assert '\n' not in timed[0]
    assert 'report_id=-' in timed[0]


def test_logged_path_has_control_characters_escaped():
    class FakeRequest:
        url = types.SimpleNamespace(path='/dashboard/x\n2026-10-11 INFO yclients.auth FORGED\u2028')
        query_params = {}

    path, _ = api._timed_request_fields(FakeRequest())
    assert '\n' not in path and '\u2028' not in path
    assert path == '/dashboard/x\\n2026-10-11 INFO yclients.auth FORGED\\u2028'


@pytest.mark.asyncio
async def test_api_responses_are_not_cacheable_by_default(async_session):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            dashboard = await client.get('/dashboard/branches')
            health = await client.get('/health')
            missing = await client.get('/no-such-route')
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    for response in (dashboard, health, missing):
        assert response.headers['cache-control'] == 'private, no-store'


@pytest.mark.asyncio
@pytest.mark.parametrize('path', ['/groups', '/companies', '/clients', '/stats', '/export/csv/companies'])
async def test_the_raw_table_api_is_gone(async_session, path):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get(path)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 404
