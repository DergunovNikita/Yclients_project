"""Dashboard routes: the tenant an API key must name, and the registry refusing a non-async builder."""

import dataclasses
from datetime import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

import api
import dashboard_reports
from api import app
from models import PortalAccount, PortalMetricVisibility

NOW = datetime(2026, 10, 1)


@pytest_asyncio.fixture
async def client(async_session):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as http:
        yield http
    app.dependency_overrides.clear()


async def _accounts(session) -> int:
    return await session.scalar(select(func.count()).select_from(PortalAccount))


@pytest.mark.asyncio
async def test_an_api_key_must_name_the_tenant_of_a_settings_read(client, async_session):
    response = await client.get('/dashboard/metric-visibility')

    assert response.status_code == 400
    assert response.json()['detail'] == 'X-Portal-Account-Id is required'
    # A GET used to create a 'default' tenant when there was none.
    assert await _accounts(async_session) == 0


@pytest.mark.asyncio
async def test_an_api_key_cannot_name_a_tenant_that_does_not_exist(client, async_session):
    response = await client.get('/dashboard/metric-visibility', headers={'X-Portal-Account-Id': '9'})

    assert response.status_code == 400
    assert response.json()['detail'] == 'unknown X-Portal-Account-Id'
    assert await _accounts(async_session) == 0


@pytest.mark.asyncio
async def test_an_api_key_write_lands_on_the_tenant_it_names_not_on_the_lowest_id(client, async_session):
    async_session.add_all(
        [PortalAccount(id=1, label='first', created_at=NOW), PortalAccount(id=2, label='second', created_at=NOW)]
    )
    await async_session.commit()
    body = {'role': 'manager', 'visible_codes': ['revenue']}

    refused = await client.put('/dashboard/metric-visibility', json=body)
    saved = await client.put('/dashboard/metric-visibility', json=body, headers={'X-Portal-Account-Id': '2'})

    assert refused.status_code == 400
    assert saved.status_code == 200
    rows = (await async_session.execute(select(PortalMetricVisibility))).scalars().all()
    assert [(row.portal_account_id, row.role) for row in rows] == [(2, 'manager')]
    listed = await client.get('/dashboard/metric-visibility', headers={'X-Portal-Account-Id': '2'})
    assert listed.status_code == 200
    assert listed.json()['data']['roles']['manager'] == ['revenue']


@pytest.mark.asyncio
async def test_the_service_catalog_is_not_tenant_scoped_so_an_api_key_needs_no_tenant(client):
    assert (await client.get('/dashboard/services')).status_code == 200
    assert (await client.patch('/dashboard/services', json={'row_changes': []})).status_code == 200


def test_the_registry_refuses_a_builder_that_is_not_a_coroutine_function():
    definition = dashboard_reports.REPORT_DEFINITIONS[0]
    broken = dataclasses.replace(definition, builder=lambda req: {})

    with pytest.raises(RuntimeError, match='builder must be an async function'):
        dashboard_reports._build_registry((broken,))
