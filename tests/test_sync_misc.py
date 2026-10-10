"""Small sync-side guards: notifier visibility, seed wipe host check, tenant sync-status payload."""
import pytest
import requests
from httpx import ASGITransport, AsyncClient

import dashboard_routes
import seed_fake_data
import sync_notifier
from api import app
from auth_scope import AccessContext


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


@pytest.mark.asyncio
@pytest.mark.parametrize('last_run', [FULL_SYNC_PAYLOAD['last_run'], None])
async def test_sync_status_widget_exposes_only_what_the_header_needs(monkeypatch, last_run):
    payload = {**FULL_SYNC_PAYLOAD, 'last_run': last_run}
    monkeypatch.setattr(dashboard_routes, 'get_sync_status', lambda: payload)
    tenant = AccessContext(
        user_id=1, role='owner', portal_account_id=7, staff_id=None, is_platform_admin=False,
        full_access=False, company_ids=[1],
    )
    app.dependency_overrides[dashboard_routes.get_dashboard_access] = lambda: tenant

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.get('/dashboard/widget/sync_status')
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {'success': True, 'data': {'sync': {
        'running': True,
        'last_successful_sync_at': '2026-10-09T02:00:00',
        'last_run': {'status': 'failed', 'finished_at': '2026-10-10T02:00:00'} if last_run else None,
    }}}
