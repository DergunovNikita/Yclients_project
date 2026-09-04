"""Tests for the YClients HTTP client retry hardening."""

from urllib3.util.retry import Retry

import pytest

from datetime import date, timedelta

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


@pytest.mark.parametrize(
    ('method_name', 'payload'),
    [
        ('get_services', {'success': False, 'data': []}),
        ('get_services', {'data': []}),
        ('get_services', {'success': True}),
        ('get_services', {'success': True, 'data': {'id': 1}}),
        ('get_staff_schedule', {'success': False, 'data': []}),
        ('get_staff_schedule', {'data': []}),
        ('get_staff_schedule', {'success': True}),
        ('get_staff_schedule', {'success': True, 'data': {'staff_id': 1}}),
    ],
)
def test_snapshot_endpoints_reject_logical_and_malformed_responses(method_name, payload):
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api._get = lambda *_args, **_kwargs: payload

    method = getattr(api, method_name)
    args = ('1', '2025-01-01', '2025-01-31') if method_name == 'get_staff_schedule' else ('1',)
    assert method(*args) is None


@pytest.mark.parametrize('method_name', ['get_services', 'get_staff_schedule'])
def test_snapshot_endpoints_preserve_successful_empty_lists(method_name):
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api._get = lambda *_args, **_kwargs: {'success': True, 'data': []}

    method = getattr(api, method_name)
    args = ('1', '2025-01-01', '2025-01-31') if method_name == 'get_staff_schedule' else ('1',)
    assert method(*args) == []


DATED_METHODS = [
    'get_records',
    'get_financial_transactions',
    'get_goods_transactions',
    'get_comments',
]


@pytest.mark.parametrize('method_name', DATED_METHODS)
def test_dated_fetch_keeps_a_quiet_range_to_a_single_request(method_name):
    """A range that fits one page has no boundary to lose rows across."""
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    windows = []

    def fake_get(_url, params):
        windows.append((params['start_date'], params['end_date']))
        return {'success': True, 'data': [{'id': 1}]}

    api._get = fake_get
    items = getattr(api, method_name)('1', start_date='2000-01-01', end_date='2026-09-02')

    # A decade before the branch opened must not be walked month by month.
    assert windows == [('2000-01-01', '2026-09-02')]
    assert items == [{'id': 1}]


@pytest.mark.parametrize('method_name', DATED_METHODS)
def test_dated_fetch_halves_a_range_that_needs_more_than_one_page(method_name):
    """A multi-page response drops rows at the page boundary, so it is split instead."""
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api.MAX_PER_PAGE = 2
    windows = []

    def fake_get(_url, params):
        window = (params['start_date'], params['end_date'])
        if params['page'] == 1:
            windows.append(window)
        span = date.fromisoformat(window[1]) - date.fromisoformat(window[0])
        # Only a single-day range fits one page; anything wider spills over.
        rows = 1 if span.days == 0 else 3
        return {'success': True, 'data': [{'id': f'{window[0]}:{n}'} for n in range(rows)][
            (params['page'] - 1) * api.MAX_PER_PAGE: params['page'] * api.MAX_PER_PAGE
        ]}

    api._get = fake_get
    items = getattr(api, method_name)('1', start_date='2026-07-01', end_date='2026-07-04')

    assert windows == [
        ('2026-07-01', '2026-07-04'),
        ('2026-07-01', '2026-07-02'),
        ('2026-07-01', '2026-07-01'),
        ('2026-07-02', '2026-07-02'),
        ('2026-07-03', '2026-07-04'),
        ('2026-07-03', '2026-07-03'),
        ('2026-07-04', '2026-07-04'),
    ]
    # Only the single-day leaves contribute; wider probes are discarded, not merged.
    assert sorted(item['id'] for item in items) == [
        '2026-07-01:0', '2026-07-02:0', '2026-07-03:0', '2026-07-04:0',
    ]


def test_dated_fetch_reads_only_one_page_before_deciding_to_split():
    """Probing must not download rows that the split throws away."""
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api.MAX_PER_PAGE = 2
    pages_by_window = {}

    def fake_get(_url, params):
        window = (params['start_date'], params['end_date'])
        pages_by_window.setdefault(window, []).append(params['page'])
        span = date.fromisoformat(window[1]) - date.fromisoformat(window[0])
        rows = 1 if span.days == 0 else 6
        return {'success': True, 'data': [{'id': f'{window[0]}:{n}'} for n in range(rows)][
            (params['page'] - 1) * api.MAX_PER_PAGE: params['page'] * api.MAX_PER_PAGE
        ]}

    api._get = fake_get
    api.get_financial_transactions('1', start_date='2026-07-01', end_date='2026-07-02')

    assert pages_by_window[('2026-07-01', '2026-07-02')] == [1]


def test_dated_fetch_accepts_a_single_day_busier_than_one_page():
    """A day cannot be split further, so its pages are taken as they come."""
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api.MAX_PER_PAGE = 2
    calls = []

    def fake_get(_url, params):
        calls.append(params['page'])
        return {'success': True, 'data': [{'id': n} for n in range(3)][
            (params['page'] - 1) * api.MAX_PER_PAGE: params['page'] * api.MAX_PER_PAGE
        ]}

    api._get = fake_get
    items = api.get_financial_transactions('1', start_date='2026-07-01', end_date='2026-07-01')

    # Page 1 is the probe; the same day is then read in full, and ids stay distinct.
    assert [item['id'] for item in items] == [0, 1, 2]
    assert calls == [1, 1, 2]


@pytest.mark.parametrize('method_name', DATED_METHODS)
def test_dated_fetch_without_both_bounds_keeps_a_single_request(method_name):
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    calls = []

    def fake_get(_url, params):
        calls.append(params)
        return {'success': True, 'data': [{'id': 1}]}

    api._get = fake_get
    assert getattr(api, method_name)('1') == [{'id': 1}]
    assert getattr(api, method_name)('1', start_date='2026-07-01') == [{'id': 1}]

    assert len(calls) == 2
    assert 'start_date' not in calls[0] and 'end_date' not in calls[0]
    assert calls[1]['start_date'] == '2026-07-01' and 'end_date' not in calls[1]


def test_dated_fetch_drops_a_row_repeated_across_one_day_pages():
    """A day too busy to split can still serve the same row on two of its pages."""
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api.MAX_PER_PAGE = 2
    pages = {1: [{'id': 'a'}, {'id': 'b'}], 2: [{'id': 'b'}, {'id': 'c'}], 3: []}

    api._get = lambda _url, params: {'success': True, 'data': pages[params['page']]}
    items = api.get_financial_transactions('1', start_date='2026-07-01', end_date='2026-07-01')

    assert [item['id'] for item in items] == ['a', 'b', 'c']


def test_dated_fetch_keeps_payloads_that_carry_no_id():
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api._get = lambda _url, _params: {'success': True, 'data': [{'title': 'x'}, {'title': 'y'}]}

    items = api.get_comments('1', start_date='2026-07-01', end_date='2026-07-31')

    assert items == [{'title': 'x'}, {'title': 'y'}]


def test_dated_fetch_stops_when_requests_grow_without_new_rows():
    """If the endpoint ignores the date filter, splitting must fail instead of looping.

    Deliberately runs on the real constants: overriding them here would prove only that
    the check reads its own settings, leaving a raised ceiling free to ship green.
    """
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    calls = []
    # One full page of the same rows for every window, as an unfiltered endpoint answers:
    # requests climb, distinct rows do not, so the allowance stops growing after one page.
    page = [{'id': n} for n in range(api.MAX_PER_PAGE)]

    def fake_get(_url, params):
        calls.append(params)
        # Finite only so that a disabled guard fails this test instead of hanging it;
        # the real endpoint would keep answering for as long as it is asked.
        if len(calls) > 2000:
            return {'success': True, 'data': []}
        return {'success': True, 'data': page}

    api._get = fake_get

    with pytest.raises(RuntimeError, match='date filter'):
        api.get_financial_transactions('1', start_date='2000-01-01', end_date='2026-12-31')

    # Nothing is ever recorded as seen — every page is discarded by a split — so the base
    # allowance is the whole allowance.
    assert len(calls) == api.SPLIT_REQUESTS_BASE
    # An absolute ceiling too, so raising the constants cannot quietly disable the guard.
    assert len(calls) < 1000
    # The budget is scoped to one dated fetch, not left armed on the client.
    assert api._split_seen is None


def test_split_budget_grows_with_distinct_rows_not_with_days():
    """Only rows nobody has seen yet buy more requests."""
    api = YClientsAPI('token', 'login', 'password')

    api._split_seen = set()
    api._split_requests = api.SPLIT_REQUESTS_BASE
    with pytest.raises(RuntimeError, match='0 distinct rows'):
        api._spend_split_budget('endpoint')

    # Two pages worth of distinct rows raise the ceiling by two per-page allowances.
    api._split_seen = set(range(2 * api.MAX_PER_PAGE))
    api._split_requests = api.SPLIT_REQUESTS_BASE + 2 * api.SPLIT_REQUESTS_PER_PAGE - 1
    api._spend_split_budget('endpoint')
    with pytest.raises(RuntimeError, match='distinct rows'):
        api._spend_split_budget('endpoint')


def test_split_budget_is_inert_outside_a_dated_fetch():
    api = YClientsAPI('token', 'login', 'password')
    api._split_requests = 10 ** 6
    api._spend_split_budget('endpoint')


def test_fetch_reports_itself_incomplete_when_a_day_overflows_one_page():
    """Callers that purge before reloading need to know the answer may be short."""
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api.MAX_PER_PAGE = 2
    rows = [{'id': n} for n in range(3)]

    api._get = lambda _url, params: {'success': True, 'data': rows[
        (params['page'] - 1) * api.MAX_PER_PAGE: params['page'] * api.MAX_PER_PAGE
    ]}
    api.get_financial_transactions('1', start_date='2026-07-01', end_date='2026-07-01')

    assert api.last_dated_fetch_complete is False


def test_fetch_reports_itself_complete_when_every_piece_fits_one_page():
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api.last_dated_fetch_complete = False
    api._get = lambda _url, _params: {'success': True, 'data': [{'id': 1}]}

    api.get_financial_transactions('1', start_date='2026-07-01', end_date='2026-07-31')

    # The flag describes the latest window, not every window the client ever read.
    assert api.last_dated_fetch_complete is True


def test_a_day_that_exactly_fills_one_page_is_not_reported_incomplete():
    """Filling the probe is not the same as spilling past it — one page loses nothing."""
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    api.MAX_PER_PAGE = 2
    rows = [{'id': 1}, {'id': 2}]

    api._get = lambda _url, params: {'success': True, 'data': rows[
        (params['page'] - 1) * api.MAX_PER_PAGE: params['page'] * api.MAX_PER_PAGE
    ]}
    items = api.get_financial_transactions('1', start_date='2026-07-01', end_date='2026-07-01')

    assert [item['id'] for item in items] == [1, 2]
    assert api.last_dated_fetch_complete is True


@pytest.mark.parametrize(
    ('days', 'rows_per_day', 'shape'),
    [
        (90, 25, 'flat'),        # a 90-day refresh window at a real branch's density
        (3170, 25, 'flat'),      # a branch open since 2018, whole history
        (9490, 8, 'flat'),       # the default 2000-01-01 floor, mostly empty years
        (3170, 25, 'clustered'), # the same volume packed into one weekday in seven
    ],
)
def test_honest_workloads_stay_well_inside_the_split_budget(days, rows_per_day, shape):
    """The guard must bound a broken endpoint without ever firing on a real branch.

    The constants are argued for with measured numbers; without this the calibration can
    drift silently — a smaller base breaks every full sync, a larger one restores the
    runaway it exists to stop.
    """
    api = YClientsAPI('token', 'login', 'password')
    api.user_token = 'authenticated'
    start = date(2000, 1, 1)

    rows_by_day = {}
    for offset in range(days):
        day = start + timedelta(days=offset)
        if shape == 'clustered':
            count = rows_per_day * 7 if offset % 7 == 0 else 0
        else:
            count = rows_per_day
        if count:
            rows_by_day[day.isoformat()] = count

    requests = []
    high_water = []

    def fake_get(_url, params):
        requests.append(params)
        high_water.append(api._split_requests)
        window_start = date.fromisoformat(params['start_date'])
        window_end = date.fromisoformat(params['end_date'])
        rows = []
        for day_iso, count in rows_by_day.items():
            day = date.fromisoformat(day_iso)
            if window_start <= day <= window_end:
                rows.extend({'id': f'{day_iso}:{n}'} for n in range(count))
        page = params['page']
        return {'success': True, 'data': rows[
            (page - 1) * api.MAX_PER_PAGE: page * api.MAX_PER_PAGE
        ]}

    api._get = fake_get
    items = api.get_financial_transactions(
        '1', start_date=start.isoformat(),
        end_date=(start + timedelta(days=days - 1)).isoformat(),
    )

    assert len(items) == sum(rows_by_day.values())
    assert len(items) == len({item['id'] for item in items})
    assert api.last_dated_fetch_complete is True

    spent = len(requests)
    allowance = (
        api.SPLIT_REQUESTS_BASE
        + api.SPLIT_REQUESTS_PER_PAGE * (len(items) // api.MAX_PER_PAGE)
    )
    # Two-thirds of the allowance is the line: anything closer means the constants no
    # longer leave room for a branch busier than the ones they were measured on.
    assert spent < allowance * 2 / 3, (
        f'{shape} {days}d x{rows_per_day}: {spent} запросов при лимите {allowance}'
    )
