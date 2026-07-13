"""Tests for the YClients HTTP client retry hardening."""

from urllib3.util.retry import Retry

import pytest

from yclients_api import YClientsAPI


def test_client_overrides_dangerous_default_retry_after_max():
    # urllib3's default retry_after_max is 21600s (6h); a large 429 Retry-After would then stall a
    # sync step for hours. The client must mount a Retry with the configured, bounded cap instead.
    api = YClientsAPI('token', 'login', 'password', retry_total=2, retry_after_max=30)
    retry = api.session.get_adapter('https://api.yclients.com/api/v1/auth').max_retries
    assert isinstance(retry, Retry)
    assert retry.retry_after_max == 30
    assert retry.total == 2
    assert retry.respect_retry_after_header is True


def test_configured_cap_bounds_a_large_retry_after():
    # parse_retry_after applies the cap; a hostile 1-hour header collapses to the configured max.
    api = YClientsAPI('token', 'login', 'password', retry_after_max=45)
    retry = api.session.get_adapter('https://api.yclients.com').max_retries
    assert retry.parse_retry_after('3600') == 45
    assert retry.parse_retry_after('5') == 5


def test_cap_survives_retry_new_chain():
    # urllib3 rebuilds Retry between attempts via new(); the bounded cap must persist.
    api = YClientsAPI('token', 'login', 'password', retry_after_max=45)
    retry = api.session.get_adapter('https://api.yclients.com').max_retries
    assert retry.new().retry_after_max == 45


def test_paginated_fetch_raises_on_page_failure():
    api = YClientsAPI('token', 'login', 'password')
    calls = []

    def fake_get(_url, params):
        calls.append(params['page'])
        if params['page'] == 1:
            return {'data': [{'id': 1}] * api.MAX_PER_PAGE, 'meta': {'total_count': api.MAX_PER_PAGE + 1}}
        return None

    api._get = fake_get

    with pytest.raises(RuntimeError, match='page 2'):
        api._get_all_pages('https://api.example.test/items')

    assert calls == [1, 2]


@pytest.mark.parametrize(
    ('payload', 'message'),
    [
        ({'success': False, 'data': []}, 'success=false'),
        ({'success': True}, 'no data field'),
        ({'success': True, 'data': {'id': 1}}, 'non-list data'),
    ],
)
def test_paginated_fetch_rejects_malformed_success_payload(payload, message):
    api = YClientsAPI('token', 'login', 'password')
    api._get = lambda _url, _params: payload

    with pytest.raises(RuntimeError, match=message):
        api._get_all_pages('https://api.example.test/items')


@pytest.mark.parametrize(
    'method_name',
    [
        'get_records',
        'get_financial_transactions',
        'get_goods_transactions',
        'get_comments',
    ],
)
def test_transactional_paginated_endpoints_return_none_on_auth_failure(method_name):
    api = YClientsAPI('token', 'login', 'password')
    api.authenticate = lambda: False

    assert getattr(api, method_name)('1') is None
