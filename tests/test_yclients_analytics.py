from datetime import date

import httpx
import pytest

import yclients_analytics


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
            1: (10, 2, 3, 15),
            2: (20, 4, 1, 25),
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


@pytest.mark.asyncio
async def test_fetch_record_stats_aggregates_companies_and_passes_staff(monkeypatch):
    clients = []

    def client_factory(*args, **kwargs):
        client = FakeAsyncClient(*args, **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(yclients_analytics, 'PARTNER_TOKEN', 'partner')
    monkeypatch.setattr(yclients_analytics, 'LOGIN', 'login')
    monkeypatch.setattr(yclients_analytics, 'PASSWORD', 'password')
    monkeypatch.setattr(yclients_analytics.httpx, 'AsyncClient', client_factory)

    result = await yclients_analytics.fetch_record_stats(
        [1, 2, 1],
        date(2026, 6, 1),
        date(2026, 6, 30),
        staff_id=77,
    )

    assert result == {'completed': 30, 'incomplete': 6, 'cancelled': 4, 'total': 40}
    assert len(clients[0].get_calls) == 2
    assert all(call[1]['staff_id'] == 77 for call in clients[0].get_calls)
    assert all(call[1]['date_from'] == '2026-06-01' for call in clients[0].get_calls)
    assert all(call[1]['date_to'] == '2026-06-30' for call in clients[0].get_calls)


@pytest.mark.asyncio
async def test_fetch_record_stats_requires_credentials(monkeypatch):
    monkeypatch.setattr(yclients_analytics, 'PARTNER_TOKEN', '')

    with pytest.raises(yclients_analytics.YClientsAnalyticsError):
        await yclients_analytics.fetch_record_stats(
            [1],
            date(2026, 6, 1),
            date(2026, 6, 30),
        )


@pytest.mark.asyncio
async def test_fetch_record_stats_rejects_invalid_payload(monkeypatch):
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

    monkeypatch.setattr(yclients_analytics, 'PARTNER_TOKEN', 'partner')
    monkeypatch.setattr(yclients_analytics, 'LOGIN', 'login')
    monkeypatch.setattr(yclients_analytics, 'PASSWORD', 'password')
    monkeypatch.setattr(yclients_analytics.httpx, 'AsyncClient', InvalidPayloadClient)

    with pytest.raises(yclients_analytics.YClientsAnalyticsError):
        await yclients_analytics.fetch_record_stats(
            [1],
            date(2026, 6, 1),
            date(2026, 6, 30),
        )


@pytest.mark.asyncio
async def test_fetch_record_stats_wraps_timeout(monkeypatch):
    class TimeoutClient(FakeAsyncClient):
        async def get(self, url, *, headers, params):
            raise httpx.ReadTimeout('timed out', request=httpx.Request('GET', url))

    monkeypatch.setattr(yclients_analytics, 'PARTNER_TOKEN', 'partner')
    monkeypatch.setattr(yclients_analytics, 'LOGIN', 'login')
    monkeypatch.setattr(yclients_analytics, 'PASSWORD', 'password')
    monkeypatch.setattr(yclients_analytics.httpx, 'AsyncClient', TimeoutClient)

    with pytest.raises(yclients_analytics.YClientsAnalyticsError):
        await yclients_analytics.fetch_record_stats(
            [1],
            date(2026, 6, 1),
            date(2026, 6, 30),
        )
