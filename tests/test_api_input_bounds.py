"""Request values the database cannot take, or the calendar does not cover, are refused up front, not as a 500."""
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import api
import auth_deps
from api import app
from api_types import INT32_MAX, INT32_MIN, MAX_BODY_ITEMS
from clock import utcnow
from conftest import issue_access_token
from helpers import cached_password_hash
from models import Company, Group, PortalAccount, PortalBranch, PortalUser

TOO_BIG = INT32_MAX + 1


@pytest_asyncio.fixture
async def owner_headers(async_session, monkeypatch):
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add_all([Group(id=1, title='G'), PortalAccount(id=1, label='Tenant', created_at=utcnow())])
    await async_session.flush()
    async_session.add(Company(id=1, title='Branch', group_id=1))
    await async_session.flush()
    async_session.add_all([
        PortalBranch(portal_account_id=1, company_id=1),
        PortalUser(
            id=1, portal_account_id=1, email='owner@example.com', password_hash=cached_password_hash('Passw0rd!x'),
            full_name='Owner', role='owner', is_active=True, email_verified_at=utcnow(), created_at=utcnow(),
        ),
    ])
    await async_session.commit()
    return {'Authorization': f'Bearer {await issue_access_token(async_session, 1, "owner")}'}


async def _call(db, method, url, headers, **kwargs):
    async def override_db():
        yield db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.request(method, url, headers=headers, **kwargs)
    finally:
        app.dependency_overrides.clear()


DAYS = {'start_date': '2026-01-01', 'end_date': '2026-01-31'}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('method', 'url', 'kwargs', 'extra_headers'),
    [
        ('GET', '/dashboard/bundle', {'params': {**DAYS, 'staff_id': TOO_BIG}}, {}),
        ('GET', '/dashboard/bundle', {'params': {**DAYS, 'company_id': TOO_BIG}}, {}),
        ('GET', '/dashboard/reports/data',
         {'params': {**DAYS, 'report_id': 'financial_overview', 'compare_staff_id': TOO_BIG}}, {}),
        ('GET', '/dashboard/branches', {}, {'X-Portal-Account-Id': str(TOO_BIG)}),
        ('DELETE', f'/auth/admin/users/{TOO_BIG}', {}, {}),
        ('DELETE', f'/auth/sessions/{TOO_BIG}', {}, {}),
        ('POST', f'/auth/admin/staff/{TOO_BIG}/create-account', {'json': {}}, {}),
        ('POST', '/auth/admin/distribute-credentials', {'json': {'user_ids': [TOO_BIG]}}, {}),
        ('PATCH', f'/auth/admin/yclients-credentials/{TOO_BIG}', {'json': {}}, {}),
        ('PATCH', '/dashboard/services',
         {'json': {'row_changes': [{'company_id': 1, 'service_id': TOO_BIG, 'is_extra': True}]}}, {}),
        ('POST', '/sync/trigger', {'json': {'credential_id': TOO_BIG}}, {'X-Sync-Token': 'test-sync-token'}),
        ('POST', '/onboarding/branches', {'json': {'credential_id': TOO_BIG, 'company_ids': [1]}}, {}),
    ],
)
async def test_an_id_beyond_int32_is_a_422_not_a_database_error(
    async_session, owner_headers, method, url, kwargs, extra_headers, monkeypatch
):
    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    response = await _call(async_session, method, url, {**owner_headers, **extra_headers}, **kwargs)

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_an_id_inside_the_range_still_reaches_the_handler(async_session, owner_headers):
    response = await _call(async_session, 'DELETE', f'/auth/admin/users/{INT32_MAX}', owner_headers)

    assert response.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [
        {'start_date': '1999-12-31', 'end_date': '2026-01-31'},
        {'start_date': '2026-01-01', 'end_date': '2100-01-01'},
        {'start_date': '0001-01-01', 'end_date': '2026-01-31'},
        {'start_date': '2026-01-01', 'end_date': '9999-12-31'},
    ],
)
async def test_a_period_outside_the_supported_calendar_is_a_400(async_session, owner_headers, params):
    bundle = await _call(async_session, 'GET', '/dashboard/bundle', owner_headers, params=params)
    report = await _call(
        async_session, 'GET', '/dashboard/reports/data', owner_headers,
        params={**params, 'report_id': 'financial_overview'},
    )

    assert (bundle.status_code, report.status_code) == (400, 400)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'compare', [{'compare_start_date': '1999-12-31', 'compare_end_date': '2025-12-31'},
                {'compare_start_date': '2025-01-01', 'compare_end_date': '2100-01-01'}],
)
async def test_a_comparison_window_outside_the_supported_calendar_is_a_400(async_session, owner_headers, compare):
    response = await _call(
        async_session, 'GET', '/dashboard/reports/data', owner_headers,
        params={**DAYS, 'report_id': 'financial_overview', **compare},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_the_first_and_last_supported_days_are_accepted(async_session, owner_headers):
    response = await _call(
        async_session, 'GET', '/dashboard/staff', owner_headers,
        params={'start_date': '2000-01-01', 'end_date': '2099-12-31'},
    )

    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize('field', ['opz_fact', 'reviews_fact'])
async def test_a_manual_fact_day_before_the_supported_calendar_is_a_400(async_session, owner_headers, field):
    response = await _call(
        async_session, 'GET', f'/dashboard/plan/{field}', owner_headers, params={'day': '0001-01-01'}
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_a_body_list_beyond_the_cap_is_a_422(async_session, owner_headers):
    row = {'company_id': 1, 'staff_id': 1, 'value': 1.0}
    response = await _call(
        async_session, 'POST', '/dashboard/plan/reviews_fact', owner_headers,
        json={'day': '2026-01-05', 'items': [row] * (MAX_BODY_ITEMS + 1)},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_health_answers_head_like_get(async_session):
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        head = await client.head('/health')
        get = await client.get('/health')

    assert (head.status_code, get.status_code) == (200, 200)


@pytest_asyncio.fixture
async def demo_like_branch(async_session, owner_headers):
    """The demo tenant lives on negative ids; validation must not take them for garbage."""
    async_session.add(Group(id=-1, title='Demo group'))
    await async_session.flush()
    async_session.add(Company(id=-3, title='Demo branch', group_id=-1))
    await async_session.flush()
    async_session.add(PortalBranch(portal_account_id=1, company_id=-3))
    await async_session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'params',
    [{'company_id': -3}, {'company_ids': '-3'}, {'company_ids': '1,-3'}, {'company_id': -3, 'staff_ids': '-24'}],
)
async def test_negative_ids_of_the_demo_tenant_pass_validation(async_session, owner_headers, demo_like_branch, params):
    bundle = await _call(async_session, 'GET', '/dashboard/bundle', owner_headers, params={**DAYS, **params})
    plan = await _call(async_session, 'GET', '/dashboard/widget/plan_fact', owner_headers, params={**DAYS, **params})

    # A staff id that does not exist is the handler's 400 (`unknown staff_id`), not a validation refusal.
    expected = 400 if 'staff_ids' in params else 200
    assert (bundle.status_code, plan.status_code) == (expected, expected)


@pytest.mark.asyncio
async def test_ids_beyond_int32_in_either_direction_are_refused(async_session, owner_headers):
    for bad in (INT32_MAX + 1, INT32_MIN - 1):
        single = await _call(async_session, 'GET', '/dashboard/bundle', owner_headers,
                             params={**DAYS, 'company_id': bad})
        listed = await _call(async_session, 'GET', '/dashboard/bundle', owner_headers,
                             params={**DAYS, 'company_ids': str(bad)})
        assert (single.status_code, listed.status_code) == (422, 400)


@pytest.mark.asyncio
async def test_a_zero_id_in_a_list_is_a_400(async_session, owner_headers):
    response = await _call(async_session, 'GET', '/dashboard/bundle', owner_headers,
                           params={**DAYS, 'company_ids': '0'})

    assert response.status_code == 400
