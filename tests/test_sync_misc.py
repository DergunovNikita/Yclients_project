"""Small sync-side guards: notifier visibility, seed wipe host check, tenant sync-status payload."""
from datetime import datetime

import pytest
import requests
from httpx import ASGITransport, AsyncClient

import dashboard_routes
import seed_fake_data
import sync_notifier
from api import app
from auth_scope import AccessContext
from models import SyncState, YClientsCredential, YClientsCredentialCompany
from sync_pipeline import transactional_state_key


def test_notifier_logs_the_exception_class_but_not_the_token(monkeypatch):
    def boom(*_args, **_kwargs):
        raise requests.ConnectionError('https://api.telegram.org/botSECRET/sendMessage failed')

    printed = []
    monkeypatch.setattr(sync_notifier.requests, 'post', boom)
    monkeypatch.setattr(sync_notifier, 'print', lambda *args: printed.append(' '.join(map(str, args))), raising=False)

    assert sync_notifier.TelegramNotifier('SECRET', '1').send('hi') is False

    out = ' '.join(printed)
    assert 'ConnectionError' in out
    assert 'SECRET' not in out


@pytest.mark.parametrize('host', ['db.example.com', '10.0.0.5', ''])
def test_seed_wipe_refuses_a_non_local_database(monkeypatch, host):
    monkeypatch.setattr(seed_fake_data, 'DB_HOST', host)
    monkeypatch.setattr('sys.argv', ['seed_fake_data.py', '--wipe'])

    def must_not_connect(*_args, **_kwargs):
        raise AssertionError('connected before the host check')

    monkeypatch.setattr(seed_fake_data, 'init_database', must_not_connect)

    assert seed_fake_data.main() == 1


@pytest.mark.parametrize('host', ['localhost', '127.0.0.1', '::1'])
def test_seed_wipe_passes_the_host_check_for_local_hosts(monkeypatch, host):
    monkeypatch.setattr(seed_fake_data, 'DB_HOST', host)
    monkeypatch.setattr('sys.argv', ['seed_fake_data.py', '--wipe'])

    class Unreachable:
        def test_connection(self):
            return False

    monkeypatch.setattr(seed_fake_data, 'init_database', lambda *_a, **_k: Unreachable())

    assert seed_fake_data.main() == 1  # got past the guard and stopped at the connection test


FULL_SYNC_PAYLOAD = {
    'running': True,
    'current_run': {'id': 5, 'log_path': '/app/logs/x.log', 'message': 'secret', 'initiator': 'cli'},
    'last_run': {
        'id': 4,
        'mode': 'full',
        'trigger_type': 'queued',
        'status': 'failed',
        'initiator': 'auto-worker',
        'started_at': '2026-10-10T01:00:00',
        'finished_at': '2026-10-10T02:00:00',
        'log_path': '/app/logs/sync.log',
        'message': 'psycopg2.errors.UniqueViolation: INSERT INTO tenant_b ...',
    },
    'last_successful_sync_at': '2026-10-09T02:00:00',
}


def _tenant(**overrides):
    fields = dict(
        user_id=1, role='owner', portal_account_id=7, staff_id=None, is_platform_admin=False,
        full_access=False, company_ids=None,
    )
    return AccessContext(**{**fields, **overrides})


async def _get_sync_status(monkeypatch, async_session, ctx, payload=FULL_SYNC_PAYLOAD):
    monkeypatch.setattr(dashboard_routes, 'get_sync_status', lambda: payload)
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = lambda: ctx
    app.dependency_overrides[dashboard_routes.get_async_db] = lambda: async_session
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/dashboard/widget/sync_status')
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    return response.json()['data']['sync']


@pytest.mark.asyncio
@pytest.mark.parametrize('last_run', [FULL_SYNC_PAYLOAD['last_run'], None])
async def test_sync_status_widget_exposes_only_what_the_header_needs(monkeypatch, async_session, last_run):
    """A tenant with nothing to sync with (the demo account) sees the global run, trimmed."""
    payload = {**FULL_SYNC_PAYLOAD, 'last_run': last_run}

    sync = await _get_sync_status(monkeypatch, async_session, _tenant(company_ids=[1]), payload)

    assert sync == {
        'running': True,
        'last_successful_sync_at': '2026-10-09T02:00:00',
        'last_run': {'status': 'failed', 'finished_at': '2026-10-10T02:00:00'} if last_run else None,
        'tenant_sync_failed': False,
    }


async def _seed_tenant_sync(session, *, needs_reauth=False, checkpoints=None):
    await session.run_sync(lambda sync_session: SyncState.__table__.create(sync_session.connection(), checkfirst=True))
    now = datetime(2026, 10, 1, 10, 0)
    session.add(YClientsCredential(
        id=1, portal_account_id=7, title='Main', partner_token_encrypted='x', login_encrypted='x',
        password_encrypted='x', is_active=True, needs_reauth=needs_reauth, created_at=now, updated_at=now,
    ))
    session.add_all([YClientsCredentialCompany(credential_id=1, company_id=cid) for cid in (1, 2)])
    for company_id, stamp in (checkpoints or {}).items():
        session.add(SyncState(key=transactional_state_key(company_id), value='2026-10-09', updated_at=stamp))
    await session.commit()


@pytest.mark.asyncio
async def test_tenant_widget_reports_the_tenants_own_freshness_not_the_global_run(monkeypatch, async_session):
    await _seed_tenant_sync(async_session, checkpoints={
        1: datetime(2026, 10, 9, 20, 0),
        2: datetime(2026, 10, 7, 6, 0),
    })

    sync = await _get_sync_status(monkeypatch, async_session, _tenant())

    # The oldest branch decides, and the global run (failed here) is not offered as a fallback.
    assert sync['last_successful_sync_at'] == '2026-10-07T06:00:00'
    assert sync['last_run'] is None
    assert sync['tenant_sync_failed'] is False
    assert sync['running'] is True


@pytest.mark.asyncio
async def test_tenant_widget_flags_a_tenant_whose_credentials_broke_and_keeps_it_stale(monkeypatch, async_session):
    """The global run can finish `success` while skipping this tenant, so its own timestamp must not move."""
    await _seed_tenant_sync(async_session, needs_reauth=True, checkpoints={
        1: datetime(2026, 9, 20, 6, 0),
        2: datetime(2026, 9, 20, 6, 0),
    })
    payload = {**FULL_SYNC_PAYLOAD, 'last_successful_sync_at': '2026-10-10T02:00:00'}
    payload['last_run'] = {**FULL_SYNC_PAYLOAD['last_run'], 'status': 'success'}

    sync = await _get_sync_status(monkeypatch, async_session, _tenant(), payload)

    assert sync['tenant_sync_failed'] is True
    assert sync['last_successful_sync_at'] == '2026-09-20T06:00:00'
    assert sync['last_run'] is None


@pytest.mark.asyncio
async def test_tenant_widget_has_no_freshness_until_every_branch_completed_a_pass(monkeypatch, async_session):
    await _seed_tenant_sync(async_session, checkpoints={1: datetime(2026, 10, 9, 20, 0)})

    sync = await _get_sync_status(monkeypatch, async_session, _tenant())
    assert sync['last_successful_sync_at'] is None

    # A branch administrator is only answerable for their own branches.
    scoped = await _get_sync_status(monkeypatch, async_session, _tenant(role='branch_admin', company_ids=[1]))
    assert scoped['last_successful_sync_at'] == '2026-10-09T20:00:00'


@pytest.mark.asyncio
async def test_tenant_widget_ignores_inactive_credentials(monkeypatch, async_session):
    await _seed_tenant_sync(async_session, needs_reauth=True)
    credential = await async_session.get(YClientsCredential, 1)
    credential.is_active = False
    await async_session.commit()

    sync = await _get_sync_status(monkeypatch, async_session, _tenant())

    assert sync['tenant_sync_failed'] is False
    assert sync['last_successful_sync_at'] == FULL_SYNC_PAYLOAD['last_successful_sync_at']


@pytest.mark.asyncio
async def test_api_key_caller_sees_the_global_run(monkeypatch, async_session):
    sync = await _get_sync_status(monkeypatch, async_session, AccessContext.api_key())

    assert sync['last_successful_sync_at'] == FULL_SYNC_PAYLOAD['last_successful_sync_at']
    assert sync['tenant_sync_failed'] is False
