"""Dashboard route and report-module structure: tenant of an API key, 400 mapping, logger, import graph."""

import dataclasses
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

import api
import dashboard_reports
import dashboard_routes
import dashboard_service
from api import app
from models import PortalAccount, PortalMetricVisibility

NOW = datetime(2026, 10, 1)
ROOT = Path(__file__).resolve().parent.parent


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


def test_a_value_error_becomes_a_400_and_other_exceptions_pass_through():
    with pytest.raises(HTTPException) as raised, dashboard_routes._value_error_as_400():
        raise ValueError('month must be YYYY-MM')
    assert (raised.value.status_code, raised.value.detail) == (400, 'month must be YYYY-MM')

    with pytest.raises(HTTPException) as conflict, dashboard_routes._value_error_as_400():
        raise HTTPException(status_code=409, detail='stale')
    assert conflict.value.status_code == 409

    with pytest.raises(KeyError), dashboard_routes._value_error_as_400():
        raise KeyError('defect')


@pytest.mark.asyncio
async def test_a_plan_settings_conflict_is_logged_as_a_warning_on_the_app_logger(client, monkeypatch):
    async def conflict(*args, **kwargs):
        raise dashboard_service.PlanSettingsConflict(dashboard_service.PLAN_SETTINGS_CONFLICT_DETAIL)

    monkeypatch.setattr(dashboard_routes, 'save_plan_settings', conflict)
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    logger = logging.getLogger('yclients.dashboard')
    handler = Collect(level=logging.DEBUG)
    logger.addHandler(handler)
    try:
        response = await client.post(
            '/dashboard/plan/settings', json={'month': '2025-05', 'branches': [{'company_id': 1}], 'staff': []}
        )
    finally:
        logger.removeHandler(handler)

    assert response.status_code == 409
    assert [(record.levelno, record.name) for record in records] == [(logging.WARNING, 'yclients.dashboard')]
    assert dashboard_routes.logger.name == 'yclients.dashboard'


def test_both_manual_fact_editors_share_one_payload_model():
    assert not hasattr(dashboard_routes, 'ManualReviewFactsPayload')
    assert not hasattr(dashboard_routes, 'ManualOpzFactsPayload')
    for path in ('/dashboard/plan/reviews_fact', '/dashboard/plan/opz_fact'):
        route = next(r for r in app.routes if getattr(r, 'path', None) == path and 'POST' in r.methods)
        assert route.body_field.field_info.annotation is dashboard_routes.ManualFactsPayload


def test_report_builder_modules_do_not_import_the_registry():
    """Builders and their helpers must be importable without `dashboard_reports`, or the registry cannot
    import them eagerly (the cycle the old lazy `_builder_for` import dodged)."""
    code = (
        'import sys, report_common, reports_clients, reports_feedback, reports_finance, reports_goods, '
        'reports_operations, reports_services, reports_team, payment_methods\n'
        'assert "dashboard_reports" not in sys.modules, "a builder module imports dashboard_reports"\n'
    )
    done = subprocess.run([sys.executable, '-c', code], cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr


def test_the_registry_refuses_a_builder_that_is_not_a_coroutine_function():
    definition = dashboard_reports.REPORT_DEFINITIONS[0]
    broken = dataclasses.replace(definition, builder=lambda req: {})

    with pytest.raises(RuntimeError, match='builder must be an async function'):
        dashboard_reports._build_registry((broken,))


@pytest.mark.asyncio
async def test_the_usage_report_is_refused_by_the_dispatcher():
    request = object()
    with pytest.raises(RuntimeError, match='explicit tenant'):
        await dashboard_reports.REPORT_REGISTRY[dashboard_reports.REPORT_USAGE_ID].builder(request)
