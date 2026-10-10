from datetime import date, datetime

import httpx
import pytest

import yclients_analytics
from models import Company, Group, PortalAccount, PortalBranch, Staff, YClientsCredentialCompany
from yclients_credentials import new_credential


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.request = httpx.Request('GET', 'https://api.yclients.test')

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                'request failed',
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        self.get_calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, *args, **kwargs):
        return FakeResponse({'success': True, 'data': {'user_token': 'user-token'}})

    async def get(self, url, *, headers, params):
        self.get_calls.append((url, params))
        company_id = int(url.split('/company/', 1)[1].split('/', 1)[0])
        stats = {
            101: (10, 2, 3, 15),
            102: (20, 4, 1, 25),
        }[company_id]
        completed, pending, cancelled, total = stats
        return FakeResponse({
            'success': True,
            'data': {
                'record_stats': {
                    'current_completed_count': completed,
                    'current_pending_count': pending,
                    'current_canceled_count': cancelled,
                    'current_total_count': total,
                },
            },
        })


async def _seed_db_credentials(async_session, monkeypatch, company_ids=(1, 2)):
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-analytics-key')
    async_session.add(Group(id=1, title='Group'))
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime.utcnow()))
    for company_id in company_ids:
        async_session.add(Company(id=company_id, external_id=100 + company_id, title=f'Company {company_id}', group_id=1))
        async_session.add(PortalBranch(portal_account_id=1, company_id=company_id))
    async_session.add(Staff(id=7, external_id=77, source_type='yclients', name='Master', company_id=1))
    credential = new_credential(1, 'Analytics', 'partner', 'login', 'password')
    async_session.add(credential)
    await async_session.flush()
    for company_id in company_ids:
        async_session.add(YClientsCredentialCompany(credential_id=credential.id, company_id=company_id))
    await async_session.commit()


@pytest.mark.asyncio
async def test_fetch_record_stats_aggregates_companies_and_passes_staff(async_session, monkeypatch):
    clients = []

    def client_factory(*args, **kwargs):
        client = FakeAsyncClient(*args, **kwargs)
        clients.append(client)
        return client

    await _seed_db_credentials(async_session, monkeypatch)
    monkeypatch.setattr(yclients_analytics.httpx, 'AsyncClient', client_factory)

    result = await yclients_analytics.fetch_record_stats(
        [1, 2, 1],
        date(2026, 6, 1),
        date(2026, 6, 30),
        staff_id=7,
        db=async_session,
    )

    assert result == {'completed': 30, 'incomplete': 6, 'cancelled': 4, 'total': 40}
    assert len(clients[0].get_calls) == 2
    assert all(call[1]['staff_id'] == 77 for call in clients[0].get_calls)
    assert {int(call[0].split('/company/', 1)[1].split('/', 1)[0]) for call in clients[0].get_calls} == {101, 102}
    assert all(call[1]['date_from'] == '2026-06-01' for call in clients[0].get_calls)
    assert all(call[1]['date_to'] == '2026-06-30' for call in clients[0].get_calls)


@pytest.mark.asyncio
async def test_fetch_record_stats_requires_credentials(async_session):
    with pytest.raises(
        yclients_analytics.YClientsAnalyticsError,
        match='No YClients credentials configured for company 1',
    ):
        await yclients_analytics.fetch_record_stats(
            [1],
            date(2026, 6, 1),
            date(2026, 6, 30),
            db=async_session,
        )


@pytest.mark.asyncio
async def test_fetch_record_stats_requires_database_session():
    with pytest.raises(yclients_analytics.YClientsAnalyticsError, match='Database session is required'):
        await yclients_analytics.fetch_record_stats(
            [1],
            date(2026, 6, 1),
            date(2026, 6, 30),
        )


@pytest.mark.asyncio
async def test_fetch_record_stats_rejects_invalid_payload(async_session, monkeypatch):
    class InvalidPayloadClient(FakeAsyncClient):
        async def get(self, url, *, headers, params):
            return FakeResponse({
                'success': True,
                'data': {
                    'record_stats': {
                        'current_completed_count': 1,
                        'current_pending_count': 0,
                        'current_canceled_count': None,
                        'current_total_count': 1,
                    },
                },
            })

    await _seed_db_credentials(async_session, monkeypatch, company_ids=(1,))
    monkeypatch.setattr(yclients_analytics.httpx, 'AsyncClient', InvalidPayloadClient)

    with pytest.raises(yclients_analytics.YClientsAnalyticsError):
        await yclients_analytics.fetch_record_stats(
            [1],
            date(2026, 6, 1),
            date(2026, 6, 30),
            db=async_session,
        )


@pytest.mark.asyncio
async def test_fetch_record_stats_wraps_timeout(async_session, monkeypatch):
    class TimeoutClient(FakeAsyncClient):
        async def get(self, url, *, headers, params):
            raise httpx.ReadTimeout('timed out', request=httpx.Request('GET', url))

    await _seed_db_credentials(async_session, monkeypatch, company_ids=(1,))
    monkeypatch.setattr(yclients_analytics.httpx, 'AsyncClient', TimeoutClient)

    with pytest.raises(yclients_analytics.YClientsAnalyticsError):
        await yclients_analytics.fetch_record_stats(
            [1],
            date(2026, 6, 1),
            date(2026, 6, 30),
            db=async_session,
        )


@pytest.fixture(autouse=True)
def _fresh_token_cache(monkeypatch):
    monkeypatch.setattr(yclients_analytics, '_user_tokens', {})


class CountingClient(FakeAsyncClient):
    """Counts logins and lets a test script the statuses `get` answers with."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.posts = 0
        self.statuses = list(CountingClient.script)
        self.tokens_seen = []

    script = []
    token_base = 0

    async def post(self, *args, **kwargs):
        self.posts += 1
        return FakeResponse({'success': True, 'data': {'user_token': f'token-{self.token_base + self.posts}'}})

    async def get(self, url, *, headers, params):
        self.tokens_seen.append(headers['Authorization'])
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            response = FakeResponse({}, status_code=status)
            response.headers = {'Retry-After': '1'} if status == 429 else {}
            return response
        return await super().get(url, headers=headers, params=params)


def _use_counting_client(monkeypatch, script=()):
    clients = []
    CountingClient.script = list(script)

    def factory(*args, **kwargs):
        client = CountingClient(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(yclients_analytics.httpx, 'AsyncClient', factory)
    monkeypatch.setattr(yclients_analytics.asyncio, 'sleep', _no_sleep)
    return clients


async def _no_sleep(_seconds):
    return None


async def _record_stats(async_session, company_ids=(1,)):
    return await yclients_analytics.fetch_record_stats(
        list(company_ids), date(2026, 6, 1), date(2026, 6, 30), db=async_session,
    )


@pytest.mark.asyncio
async def test_user_token_is_reused_across_requests(async_session, monkeypatch):
    await _seed_db_credentials(async_session, monkeypatch, company_ids=(1,))
    clients = _use_counting_client(monkeypatch)

    await _record_stats(async_session)
    await _record_stats(async_session)

    assert [client.posts for client in clients] == [1, 0]
    assert all('User token-1' in header for client in clients for header in client.tokens_seen)


@pytest.mark.asyncio
async def test_expired_user_token_is_requested_again(async_session, monkeypatch):
    await _seed_db_credentials(async_session, monkeypatch, company_ids=(1,))
    clients = _use_counting_client(monkeypatch)
    now = [1000.0]
    monkeypatch.setattr(yclients_analytics.time, 'monotonic', lambda: now[0])

    await _record_stats(async_session)
    now[0] += yclients_analytics.USER_TOKEN_TTL_SECONDS + 1
    await _record_stats(async_session)

    assert [client.posts for client in clients] == [1, 1]


@pytest.mark.asyncio
async def test_rejected_cached_token_is_replaced_once(async_session, monkeypatch):
    await _seed_db_credentials(async_session, monkeypatch, company_ids=(1,))
    clients = _use_counting_client(monkeypatch)
    await _record_stats(async_session)

    clients = _use_counting_client(monkeypatch, script=[401])
    monkeypatch.setattr(CountingClient, 'token_base', 10)
    result = await _record_stats(async_session)

    assert result['total'] == 15
    assert clients[0].posts == 1
    assert clients[0].tokens_seen[0].endswith('User token-1')
    assert clients[0].tokens_seen[1].endswith('User token-11')


@pytest.mark.asyncio
async def test_rate_limited_request_is_retried_after_the_advertised_wait(async_session, monkeypatch):
    await _seed_db_credentials(async_session, monkeypatch, company_ids=(1,))
    clients = _use_counting_client(monkeypatch, script=[429, 503])

    result = await _record_stats(async_session)

    assert result['total'] == 15
    assert len(clients[0].tokens_seen) == 3


@pytest.mark.asyncio
async def test_retry_after_longer_than_a_page_load_can_wait_fails_fast(async_session, monkeypatch):
    await _seed_db_credentials(async_session, monkeypatch, company_ids=(1,))
    clients = _use_counting_client(monkeypatch, script=[429])

    def too_long(response, attempt):
        return None

    monkeypatch.setattr(yclients_analytics, '_retry_delay', too_long)

    with pytest.raises(yclients_analytics.YClientsAnalyticsError):
        await _record_stats(async_session)
    assert len(clients[0].tokens_seen) == 1


def test_retry_delay_follows_retry_after_within_the_cap():
    class Response:
        def __init__(self, headers):
            self.headers = headers

    assert yclients_analytics._retry_delay(Response({'Retry-After': '2'}), 0) == 2.0
    assert yclients_analytics._retry_delay(Response({'Retry-After': '600'}), 0) is None
    assert yclients_analytics._retry_delay(Response({}), 0) == yclients_analytics.YCLIENTS_RETRY_BACKOFF
    assert yclients_analytics._retry_delay(Response({'Retry-After': 'Wed, 21 Oct 2026 07:28:00 GMT'}), 1) == (
        yclients_analytics.YCLIENTS_RETRY_BACKOFF * 2
    )
