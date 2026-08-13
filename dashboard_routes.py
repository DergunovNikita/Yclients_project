"""HTTP routes for product dashboard JSON (Chart.js / SPA)."""

from __future__ import annotations

import asyncio
import hmac
import csv
import io
from copy import deepcopy
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth_deps import forbid_demo, get_dashboard_access, is_demo_request
from auth_hierarchy import USER_ADMIN_ROLES
from auth_scope import (
    AccessContext,
    can_view_financials,
    effective_staff_id,
    hidden_money_codes,
    query_scope,
    require_financial_access,
    require_tenant_context,
    user_branch_ids,
)
from config import SYNC_API_TOKEN
from dashboard_service import (
    fetch_branches,
    fetch_extra_services,
    fetch_manual_review_facts,
    fetch_plan_fact,
    fetch_plan_settings,
    fetch_revenue_daily,
    fetch_dashboard_services,
    fetch_service_kpi_groups,
    save_service_label,
    save_service_management,
    create_service_kpi_group,
    update_service_kpi_group,
    archive_service_kpi_group,
    save_service_kpi_assignment,
    save_plan_settings,
    save_manual_review_facts,
    fetch_staff,
    fetch_staff_directory,
    fetch_summary,
    fetch_top_services,
)
from dashboard_reports import (
    DEMO_UNAVAILABLE_REPORTS,
    REPORT_GRANULARITIES,
    ReportCalculationError,
    fetch_report_data,
    fetch_report_registry,
    report_requires_financials,
)
from database import get_async_db
from models import Company, PortalAccount, PortalMetricVisibility, Staff
from plan_config import (
    ALL_MONEY_CODES,
    CONFIGURABLE_MONEY_ROLES,
    MONEY_METRICS,
    default_money_codes_for_role,
    money_payload_keys,
)
from portal_audit import log_portal_audit
from plan_import import import_plan_sheet_from_config
from sync_jobs import SyncJobService
from sync_orchestrator import get_sync_status

router = APIRouter()


class MetricVisibilityPayload(BaseModel):
    role: str
    visible_codes: list[str]


class ManualReviewFactItem(BaseModel):
    company_id: int
    staff_id: int
    value: float | None = None


class ManualReviewFactsPayload(BaseModel):
    month: str
    company_id: int | None = None
    staff_id: int | None = None
    items: list[ManualReviewFactItem]


class PlanSettingsBranchPayload(BaseModel):
    company_id: int
    wax_pct: Any = None
    head_care_pct: Any = None
    face_care_pct: Any = None
    camouflage_pct: Any = None
    cosmo_pct: Any = None
    opz_pct: Any = None
    cosmo_price: Any = None


class PlanSettingsStaffPayload(BaseModel):
    company_id: int
    staff_id: int
    staff_category: str
    clients: Any = None
    avg_check_total: Any = None
    reviews_qty: Any = None
    cosmo_qty: Any = None


class PlanSettingsPayload(BaseModel):
    month: str
    branches: list[PlanSettingsBranchPayload]
    staff: list[PlanSettingsStaffPayload]


class ServiceLabelPayload(BaseModel):
    is_extra: bool


class ServiceKpiGroupPayload(BaseModel):
    title: str | None = None
    code: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None


class ServiceKpiAssignmentPayload(BaseModel):
    group_id: int | None = None


class ServiceManagementRowChange(BaseModel):
    company_id: int
    service_id: int
    is_extra: bool | None = None
    kpi_group_id: int | None = None

    @model_validator(mode='after')
    def validate_change(self):
        changed_fields = self.model_fields_set & {'is_extra', 'kpi_group_id'}
        if not changed_fields:
            raise ValueError('service row change must include is_extra or kpi_group_id')
        if 'is_extra' in changed_fields and self.is_extra is None:
            raise ValueError('is_extra cannot be null')
        return self


class ServiceManagementGroupChange(BaseModel):
    id: int
    title: str | None = None
    code: str | None = None
    description: str | None = None
    sort_order: int | None = None
    is_active: bool | None = None

    @model_validator(mode='after')
    def validate_change(self):
        mutable_fields = {'title', 'code', 'description', 'sort_order', 'is_active'}
        if not self.model_fields_set & mutable_fields:
            raise ValueError('service group change must include at least one mutable field')
        for field in ('title', 'code', 'sort_order', 'is_active'):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f'{field} cannot be null')
        return self


class ServiceManagementPayload(BaseModel):
    row_changes: list[ServiceManagementRowChange] = Field(default_factory=list)
    group_changes: list[ServiceManagementGroupChange] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_unique_targets(self):
        row_keys = [(item.company_id, item.service_id) for item in self.row_changes]
        if len(row_keys) != len(set(row_keys)):
            raise ValueError('duplicate service row change')
        group_ids = [item.id for item in self.group_changes]
        if len(group_ids) != len(set(group_ids)):
            raise ValueError('duplicate service group change')
        return self


def _parse_range(start: date, end: date) -> tuple[date, date]:
    if start > end:
        raise HTTPException(status_code=400, detail='start_date must be <= end_date')
    return start, end


def _require_sync_token(x_sync_token: str | None) -> None:
    configured_token = (SYNC_API_TOKEN or '').strip()
    if not configured_token or not hmac.compare_digest(x_sync_token or '', configured_token):
        raise HTTPException(status_code=401, detail='Invalid sync token')


def _require_sync_access(ctx: AccessContext) -> None:
    if ctx.user_id is not None and ctx.role != 'platform_admin':
        raise HTTPException(status_code=403, detail='Sync operations require platform_admin role')


def _require_settings_admin(ctx: AccessContext) -> None:
    if not (ctx.full_access or ctx.role in USER_ADMIN_ROLES):
        raise HTTPException(status_code=403, detail='Settings require admin role')


def _hide_summary_financials(summary: dict[str, Any], hidden_codes: frozenset[str]) -> dict[str, Any]:
    payload = deepcopy(summary)
    for key in money_payload_keys(hidden_codes, 'summary'):
        payload.pop(key, None)
    payload['financials_hidden'] = True
    return payload


def _strip_plan_fact_financials(
    value: Any,
    hidden_plan_codes: set[str],
    hidden_leaderboard_keys: set[str],
    drop_all_money: bool,
) -> Any:
    if isinstance(value, list):
        items = []
        for item in value:
            stripped = _strip_plan_fact_financials(item, hidden_plan_codes, hidden_leaderboard_keys, drop_all_money)
            if stripped is not None:
                items.append(stripped)
        return items
    if isinstance(value, dict):
        code = value.get('code')
        if code in hidden_plan_codes or (drop_all_money and value.get('format') == 'money'):
            return None
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in hidden_leaderboard_keys:
                continue
            if key == 'staff_leaderboards' and isinstance(item, dict):
                filtered = {
                    board_key: _strip_plan_fact_financials(
                        board_value, hidden_plan_codes, hidden_leaderboard_keys, drop_all_money
                    )
                    for board_key, board_value in item.items()
                    if board_key not in hidden_leaderboard_keys
                }
                result[key] = {k: v for k, v in filtered.items() if v is not None}
                continue
            stripped = _strip_plan_fact_financials(item, hidden_plan_codes, hidden_leaderboard_keys, drop_all_money)
            if stripped is not None:
                result[key] = stripped
        return result
    return value


def _hide_plan_fact_financials(plan_fact: dict[str, Any], hidden_codes: frozenset[str]) -> dict[str, Any]:
    hidden_plan_codes = money_payload_keys(hidden_codes, 'plan')
    hidden_leaderboard_keys = money_payload_keys(hidden_codes, 'leaderboard')
    drop_all_money = ALL_MONEY_CODES <= hidden_codes
    payload = _strip_plan_fact_financials(
        plan_fact, hidden_plan_codes, hidden_leaderboard_keys, drop_all_money
    ) or {}
    payload['financials_hidden'] = True
    return payload


def _remove_table_field(table: dict[str, Any], field: str, metric: str | None = None) -> None:
    table['columns'] = [column for column in table.get('columns', []) if column.get('key') != field]
    for row in table.get('rows', []):
        row.pop(field, None)
    ranking = table.get('ranking')
    if not isinstance(ranking, dict):
        return
    if metric is not None:
        ranking['options'] = [option for option in ranking.get('options', []) if option.get('key') != metric]
        ranking.get('rows_by_metric', {}).pop(metric, None)
    for rows in ranking.get('rows_by_metric', {}).values():
        for row in rows:
            row.pop(field, None)


def _hide_staff_leaderboard_financials(
    report: dict[str, Any],
    hidden_codes: frozenset[str],
) -> dict[str, Any]:
    """Apply per-metric visibility to the mixed ratings report without leaking raw values."""
    payload = deepcopy(report)
    tables = list(payload.get('tables') or [])

    hidden_table_ids = money_payload_keys(hidden_codes, 'leaderboard')
    if 'revenue' in hidden_codes:
        # The only card in this mixed report is the top barber revenue, derived
        # from the positive-revenue leaderboard; drop it with the revenue tables.
        payload['cards'] = []
        payload['charts'] = []
        extra_table = next((table for table in tables if table.get('id') == 'extra_services'), None)
        if extra_table is not None:
            _remove_table_field(extra_table, 'sum', metric='sum')

    payload['tables'] = [table for table in tables if table.get('id') not in hidden_table_ids]
    payload['raw'] = {}
    if hidden_codes:
        payload['financials_hidden'] = True
    return payload


async def _validate_dashboard_scope(
    db: AsyncSession,
    company_id: int | None,
    staff_id: int | None,
    compare_staff_id: int | None = None,
    allowed_company_ids: list[int] | None = None,
) -> None:
    if company_id is not None:
        if allowed_company_ids is not None and company_id not in allowed_company_ids:
            raise HTTPException(status_code=403, detail='Branch not allowed')
        company_exists = await db.scalar(select(Company.id).where(Company.id == company_id).limit(1))
        if company_exists is None:
            raise HTTPException(status_code=400, detail='unknown company_id')

    for field_name, candidate_staff_id in (('staff_id', staff_id), ('compare_staff_id', compare_staff_id)):
        if candidate_staff_id is None:
            continue
        conditions = [Staff.id == candidate_staff_id]
        if company_id is not None:
            conditions.append(Staff.company_id == company_id)
        if allowed_company_ids is not None:
            conditions.append(Staff.company_id.in_(allowed_company_ids))
        staff_exists = await db.scalar(select(Staff.id).where(*conditions).limit(1))
        if staff_exists is None:
            raise HTTPException(status_code=400, detail=f'unknown {field_name}')


async def _default_portal_account_id(db: AsyncSession) -> int:
    account_id = await db.scalar(select(PortalAccount.id).order_by(PortalAccount.id.asc()).limit(1))
    if account_id is not None:
        return int(account_id)
    account = PortalAccount(label='default', created_at=datetime.utcnow())
    db.add(account)
    await db.flush()
    return int(account.id)


async def _kpi_portal_account_id(db: AsyncSession, ctx: AccessContext) -> int | None:
    if ctx.portal_account_id is not None:
        return ctx.portal_account_id
    if ctx.full_access:
        return await _default_portal_account_id(db)
    return None


async def _require_kpi_portal_account_id(db: AsyncSession, ctx: AccessContext) -> int:
    portal_account_id = await _kpi_portal_account_id(db, ctx)
    if portal_account_id is None:
        raise HTTPException(status_code=400, detail='X-Portal-Account-Id is required')
    return portal_account_id


@router.get('/branches')
async def dashboard_branches(
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    branch_ids, force_allowed = user_branch_ids(ctx)
    return {
        'success': True,
        'data': await fetch_branches(db, branch_ids, force_allowed=force_allowed),
    }


@router.get('/staff')
async def dashboard_staff(
    company_id: int | None = Query(None, description='Optional YClients company (salon) id'),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    scope = query_scope(ctx, company_id)
    return {
        'success': True,
        'data': await fetch_staff(
            db,
            scope['company_id'],
            allowed_company_ids=scope['branch_ids'],
            force_allowed=scope['force_allowed'],
        ),
    }


@router.get('/staff_directory.csv')
async def dashboard_staff_directory_csv(
    include_fired: bool = Query(False, description='Include fired/stale staff when true'),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    if ctx.user_id is not None and ctx.role not in USER_ADMIN_ROLES:
        raise HTTPException(status_code=403, detail='Staff directory export requires admin role')

    branch_ids, force_allowed = user_branch_ids(ctx)
    rows = await fetch_staff_directory(
        db,
        include_fired,
        allowed_company_ids=branch_ids,
        force_allowed=force_allowed,
    )
    columns = [
        'company_id',
        'company_title',
        'staff_id',
        'staff_name',
        'position',
        'user_id',
        'fired',
        'working',
        'bookable',
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'inline; filename=staff_directory.csv'},
    )


@router.get('/services')
async def dashboard_services(
    company_id: int | None = Query(None),
    q: str | None = Query(None),
    category: str | None = Query(None),
    is_extra: bool | None = Query(None),
    kpi_group_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    """Current branch service catalog with dashboard-maintained labels."""
    _require_settings_admin(ctx)
    scope = query_scope(ctx, company_id)
    return {
        'success': True,
        'data': await fetch_dashboard_services(
            db,
            company_id=scope['company_id'],
            q=q,
            category=category,
            is_extra=is_extra,
            kpi_group_id=kpi_group_id,
            allowed_company_ids=scope['allowed_company_ids'],
            portal_account_id=await _require_kpi_portal_account_id(db, ctx),
        ),
    }


@router.patch('/services', dependencies=[Depends(forbid_demo)])
async def dashboard_services_save(
    payload: ServiceManagementPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    """Atomically persist dashboard-managed service and KPI group changes."""
    _require_settings_admin(ctx)
    allowed_company_ids, _ = user_branch_ids(ctx)
    try:
        data = await save_service_management(
            db,
            row_changes=[item.model_dump(exclude_unset=True) for item in payload.row_changes],
            group_changes=[item.model_dump(exclude_unset=True) for item in payload.group_changes],
            allowed_company_ids=allowed_company_ids,
            portal_account_id=await _require_kpi_portal_account_id(db, ctx),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.get('/services/kpi_groups')
async def dashboard_service_kpi_groups(
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    return {
        'success': True,
        'data': await fetch_service_kpi_groups(
            db,
            portal_account_id=await _require_kpi_portal_account_id(db, ctx),
            include_inactive=True,
        ),
    }


@router.post('/services/kpi_groups', dependencies=[Depends(forbid_demo)])
async def dashboard_service_kpi_group_create(
    payload: ServiceKpiGroupPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    try:
        data = await create_service_kpi_group(
            db,
            portal_account_id=await _require_kpi_portal_account_id(db, ctx),
            title=payload.title or '',
            code=payload.code,
            description=payload.description,
            sort_order=payload.sort_order,
            is_active=True if payload.is_active is None else payload.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.patch('/services/kpi_groups/{group_id}', dependencies=[Depends(forbid_demo)])
async def dashboard_service_kpi_group_update(
    group_id: int,
    payload: ServiceKpiGroupPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    try:
        data = await update_service_kpi_group(
            db,
            group_id,
            portal_account_id=await _require_kpi_portal_account_id(db, ctx),
            title=payload.title,
            code=payload.code,
            description=payload.description,
            sort_order=payload.sort_order,
            is_active=payload.is_active,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.delete('/services/kpi_groups/{group_id}', dependencies=[Depends(forbid_demo)])
async def dashboard_service_kpi_group_delete(
    group_id: int,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    try:
        data = await archive_service_kpi_group(
            db,
            group_id,
            portal_account_id=await _require_kpi_portal_account_id(db, ctx),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.patch('/services/{company_id}/{service_id}/labels', dependencies=[Depends(forbid_demo)])
async def dashboard_service_label_save(
    company_id: int,
    service_id: int,
    payload: ServiceLabelPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    scope = query_scope(ctx, company_id)
    try:
        data = await save_service_label(
            db,
            company_id,
            service_id,
            is_extra=payload.is_extra,
            allowed_company_ids=scope['branch_ids'],
            portal_account_id=await _kpi_portal_account_id(db, ctx),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.patch('/services/{company_id}/{service_id}/kpi_group', dependencies=[Depends(forbid_demo)])
async def dashboard_service_kpi_assignment_save(
    company_id: int,
    service_id: int,
    payload: ServiceKpiAssignmentPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    scope = query_scope(ctx, company_id)
    try:
        data = await save_service_kpi_assignment(
            db,
            company_id,
            service_id,
            group_id=payload.group_id,
            allowed_company_ids=scope['branch_ids'],
            portal_account_id=await _kpi_portal_account_id(db, ctx),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.get('/reports')
async def dashboard_reports(is_demo: bool = Depends(is_demo_request)):
    """Full report catalog for the product reports SPA."""
    return {'success': True, 'data': fetch_report_registry(is_demo)}


@router.get('/reports/data')
async def dashboard_report_data(
    report_id: str = Query(...),
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    granularity: str = Query('day', description='day, week or month'),
    compare_start_date: date | None = Query(None),
    compare_end_date: date | None = Query(None),
    compare_staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
    is_demo: bool = Depends(is_demo_request),
):
    start, end = _parse_range(start_date, end_date)
    # The catalog already hides these for demo; block the direct URL too so a
    # bookmark cannot surface a report the demo data can never populate.
    if is_demo and report_id in DEMO_UNAVAILABLE_REPORTS:
        raise HTTPException(status_code=404, detail='Report is not available in the demo tenant')
    if report_requires_financials(report_id):
        require_financial_access(ctx)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    compare_staff_id = effective_staff_id(ctx, compare_staff_id)
    if granularity not in REPORT_GRANULARITIES:
        raise HTTPException(status_code=400, detail='granularity must be one of day, week, month')
    if (compare_start_date is None) ^ (compare_end_date is None):
        raise HTTPException(status_code=400, detail='compare_start_date and compare_end_date must be passed together')
    await _validate_dashboard_scope(
        db,
        scope['company_id'],
        staff_id,
        compare_staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
    )
    try:
        data = await fetch_report_data(
            db,
            report_id,
            start,
            end,
            scope['company_id'],
            staff_id,
            granularity,
            compare_start_date,
            compare_end_date,
            compare_staff_id,
            allowed_company_ids=scope['allowed_company_ids'],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ReportCalculationError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                'code': 'report_calculation_failed',
                'message': 'Не удалось рассчитать рейтинги за выбранный период.',
                'retryable': True,
            },
        ) from exc
    if report_id == 'staff_leaderboard':
        data = _hide_staff_leaderboard_financials(data, hidden_money_codes(ctx))
    return {'success': True, 'data': data}


@router.get('/widget/sync_status')
async def dashboard_widget_sync_status(
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    portal_account_id = require_tenant_context(ctx, allow_full_access=True)
    sync_payload = await asyncio.to_thread(get_sync_status)
    queue = await SyncJobService().async_get_status_payload(db, portal_account_id=portal_account_id)
    return {'success': True, 'data': {'sync': sync_payload, 'queue': queue}}


@router.get('/widget/summary')
async def dashboard_widget_summary(
    start_date: date = Query(..., description='Inclusive period start'),
    end_date: date = Query(..., description='Inclusive period end'),
    company_id: int | None = Query(None, description='Optional YClients company (salon) id'),
    staff_id: int | None = Query(None, description='Optional active staff id'),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    start, end = _parse_range(start_date, end_date)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    factual_at = datetime.now()
    summary = await fetch_summary(
        db,
        start,
        end,
        scope['company_id'],
        staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
        factual_at=factual_at,
    )
    hidden = hidden_money_codes(ctx)
    if hidden:
        summary = _hide_summary_financials(summary, hidden)
    return {
        'success': True,
        'data': summary,
    }


@router.get('/widget/revenue_daily')
async def dashboard_widget_revenue_daily(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    require_financial_access(ctx)
    start, end = _parse_range(start_date, end_date)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    return {
        'success': True,
        'data': await fetch_revenue_daily(
            db,
            start,
            end,
            scope['company_id'],
            staff_id,
            allowed_company_ids=scope['allowed_company_ids'],
        ),
    }


@router.get('/widget/top_services')
async def dashboard_widget_top_services(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    require_financial_access(ctx)
    start, end = _parse_range(start_date, end_date)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    return {
        'success': True,
        'data': await fetch_top_services(
            db,
            start,
            end,
            scope['company_id'],
            limit,
            staff_id,
            allowed_company_ids=scope['allowed_company_ids'],
        ),
    }


@router.get('/widget/extra_services')
async def dashboard_widget_extra_services(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    limit: int | None = Query(None, ge=1, le=1000),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    require_financial_access(ctx)
    start, end = _parse_range(start_date, end_date)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    return {
        'success': True,
        'data': await fetch_extra_services(
            db,
            start,
            end,
            scope['company_id'],
            limit,
            staff_id,
            allowed_company_ids=scope['allowed_company_ids'],
        ),
    }


@router.get('/widget/plan_fact')
async def dashboard_widget_plan_fact(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    start, end = _parse_range(start_date, end_date)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    branch_ids, force_allowed = user_branch_ids(ctx)
    plan_fact = await fetch_plan_fact(
        db,
        start,
        end,
        scope['company_id'],
        staff_id,
        allowed_company_ids=branch_ids,
        force_allowed=force_allowed,
    )
    hidden = hidden_money_codes(ctx)
    if hidden:
        plan_fact = _hide_plan_fact_financials(plan_fact, hidden)
    return {
        'success': True,
        'data': plan_fact,
    }


@router.get('/plan/settings')
async def dashboard_plan_settings(
    month: str = Query(..., description='Plan settings month in YYYY-MM format'),
    copy_from: str | None = Query(None, description='Optional source month in YYYY-MM format'),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    require_financial_access(ctx)
    branch_ids, force_allowed = user_branch_ids(ctx)
    try:
        data = await fetch_plan_settings(
            db,
            month,
            copy_from,
            allowed_company_ids=branch_ids,
            force_allowed=force_allowed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.post('/plan/settings', dependencies=[Depends(forbid_demo)])
async def dashboard_plan_settings_save(
    payload: PlanSettingsPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    require_financial_access(ctx)
    branch_ids, force_allowed = user_branch_ids(ctx)
    try:
        data = await save_plan_settings(
            db,
            payload.month,
            [item.model_dump() for item in payload.branches],
            [item.model_dump() for item in payload.staff],
            allowed_company_ids=branch_ids,
            force_allowed=force_allowed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.get('/plan/reviews_fact')
async def dashboard_plan_reviews_fact(
    month: str = Query(..., description='Reviews fact month in YYYY-MM format'),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    branch_ids, force_allowed = user_branch_ids(ctx)
    try:
        data = await fetch_manual_review_facts(
            db,
            month,
            scope['company_id'],
            staff_id,
            allowed_company_ids=branch_ids,
            force_allowed=force_allowed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.post('/plan/reviews_fact', dependencies=[Depends(forbid_demo)])
async def dashboard_plan_reviews_fact_save(
    payload: ManualReviewFactsPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_settings_admin(ctx)
    scope = query_scope(ctx, payload.company_id)
    payload_staff_id = effective_staff_id(ctx, payload.staff_id)
    branch_ids, force_allowed = user_branch_ids(ctx)
    try:
        data = await save_manual_review_facts(
            db,
            payload.month,
            scope['company_id'],
            payload_staff_id,
            [item.model_dump() for item in payload.items],
            allowed_company_ids=branch_ids,
            force_allowed=force_allowed,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


def _require_visibility_admin(ctx: AccessContext) -> None:
    if not (ctx.full_access or ctx.role in ('owner', 'platform_admin')):
        raise HTTPException(status_code=403, detail='Not allowed to configure metric visibility')


@router.get('/metric-visibility')
async def dashboard_metric_visibility(
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_visibility_admin(ctx)
    portal_account_id = await _require_kpi_portal_account_id(db, ctx)
    stored = {
        row.role: [code for code in (row.visible_codes or []) if code in ALL_MONEY_CODES]
        for row in (
            await db.execute(
                select(PortalMetricVisibility).where(
                    PortalMetricVisibility.portal_account_id == portal_account_id
                )
            )
        ).scalars()
    }
    roles = {
        role: stored.get(role, sorted(default_money_codes_for_role(role)))
        for role in CONFIGURABLE_MONEY_ROLES
    }
    return {
        'success': True,
        'data': {
            'money_metrics': [{'code': m['code'], 'label': m['label']} for m in MONEY_METRICS],
            'roles': roles,
            'defaults': {role: sorted(default_money_codes_for_role(role)) for role in CONFIGURABLE_MONEY_ROLES},
        },
    }


@router.put('/metric-visibility', dependencies=[Depends(forbid_demo)])
async def dashboard_metric_visibility_save(
    payload: MetricVisibilityPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_visibility_admin(ctx)
    portal_account_id = await _require_kpi_portal_account_id(db, ctx)
    if payload.role not in CONFIGURABLE_MONEY_ROLES:
        raise HTTPException(status_code=400, detail='Role visibility is not configurable')
    unknown = sorted(set(payload.visible_codes) - ALL_MONEY_CODES)
    if unknown:
        raise HTTPException(status_code=400, detail=f'Unknown money metric codes: {unknown}')
    visible_codes = sorted({code for code in payload.visible_codes if code in ALL_MONEY_CODES})

    row = (
        await db.execute(
            select(PortalMetricVisibility).where(
                PortalMetricVisibility.portal_account_id == portal_account_id,
                PortalMetricVisibility.role == payload.role,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = PortalMetricVisibility(
            portal_account_id=portal_account_id,
            role=payload.role,
            visible_codes=visible_codes,
            updated_at=datetime.utcnow(),
        )
        db.add(row)
    else:
        row.visible_codes = visible_codes
        row.updated_at = datetime.utcnow()
    await log_portal_audit(
        db,
        actor_user_id=ctx.user_id,
        portal_account_id=portal_account_id,
        action='metric_visibility.updated',
        target_type='role',
        target_id=payload.role,
        metadata={'visible_codes': visible_codes},
    )
    await db.commit()
    return {'success': True, 'data': {'role': payload.role, 'visible_codes': visible_codes}}


@router.post('/plan/sync', dependencies=[Depends(forbid_demo)])
async def dashboard_plan_sync(
    x_sync_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_sync_token(x_sync_token)
    _require_sync_access(ctx)
    return {'success': True, 'data': await import_plan_sheet_from_config(db)}


@router.get('/bundle')
async def dashboard_bundle(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    start, end = _parse_range(start_date, end_date)
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    factual_at = datetime.now()
    summary = await fetch_summary(
        db,
        start,
        end,
        scope['company_id'],
        staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
        factual_at=factual_at,
    )
    hidden = hidden_money_codes(ctx)
    can_see_financials = can_view_financials(ctx)
    daily = await fetch_revenue_daily(
        db,
        start,
        end,
        scope['company_id'],
        staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
        include_financials=can_see_financials,
        factual_at=factual_at,
    )
    if not can_see_financials:
        return {
            'success': True,
            'data': {
                'summary': _hide_summary_financials(summary, hidden),
                'revenue_daily': daily,
                'top_services': [],
                'extra_services': [],
                'financials_hidden': True,
            },
        }
    if hidden:
        summary = _hide_summary_financials(summary, hidden)
    services = await fetch_top_services(
        db,
        start,
        end,
        scope['company_id'],
        10,
        staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
        factual_at=factual_at,
    )
    extra_services = await fetch_extra_services(
        db,
        start,
        end,
        scope['company_id'],
        None,
        staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
        factual_at=factual_at,
    )
    return {
        'success': True,
        'data': {
            'summary': summary,
            'revenue_daily': daily,
            'top_services': services,
            'extra_services': extra_services,
        },
    }
