"""HTTP routes for product dashboard JSON (Chart.js / SPA)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from typing import Annotated, Any, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, BeforeValidator, Field, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth_deps import forbid_demo, get_dashboard_access, is_demo_request
from auth_hierarchy import USER_ADMIN_ROLES
from auth_scope import (
    AccessContext,
    BRANCH_SCOPE_ROLES,
    can_view_branch_payments,
    can_view_financials,
    can_view_report_usage,
    effective_staff_id,
    hidden_money_codes,
    manual_fact_staff_keys,
    query_scope,
    require_financial_access,
    require_tenant_context,
    user_branch_ids,
)
import dashboard_service
from clock import utcnow
from dashboard_service import (
    MANUAL_FACT_CONFLICT_DETAIL,
    ManualFactConflict,
    ManualFactRowNotOpen,
    OverviewPeriodPreset,
    PLAN_SETTINGS_CONFLICT_DETAIL,
    PlanSettingsConflict,
    fetch_branches,
    fetch_tenant_branch_order,
    fetch_manual_opz_facts,
    fetch_manual_review_facts,
    fetch_plan_fact,
    fetch_plan_settings,
    fetch_revenue_daily,
    fetch_dashboard_services,
    save_branch_order,
    save_service_management,
    save_plan_settings,
    save_manual_facts,
    fetch_staff,
    fetch_summary,
    fetch_top_services,
    level_group_staff_count,
    staff_ids_for_levels,
)
from dashboard_reports import (
    BRANCH_PAYMENT_REPORTS,
    DEMO_UNAVAILABLE_REPORTS,
    REPORT_GRANULARITIES,
    REPORT_USAGE_ID,
    fetch_report_data,
    fetch_report_registry,
    report_requires_financials,
    resolve_report_id,
)
from database import get_async_db
from models import (
    Company, PortalAccount, PortalMetricVisibility, Staff, SyncState, YClientsCredential, YClientsCredentialCompany,
)
from plan_config import (
    ALL_MONEY_CODES,
    AVERAGE_CHECK_REVENUE_FIELDS,
    CONFIGURABLE_MONEY_ROLES,
    MONEY_METRICS,
    OPZ_QTY_CODE,
    REVIEWS_QTY_CODE,
    default_money_codes_for_role,
    money_payload_keys,
)
from payment_methods import (
    CONFLICT_DETAIL,
    ManualPaymentConflict,
    PaymentRowNotOpen,
    fetch_yandex_pay_editor,
    save_yandex_pay,
)
from portal_audit import log_portal_audit
from report_payload import ReportCalculationError, ReportInputError
from report_usage import fetch_report_usage, track_report_usage
from sync_orchestrator import get_sync_status
from sync_parsing import serialize_dt
from sync_pipeline import transactional_state_key

# Under `yclients`, whose handler and level `api._configure_logging` sets: a module-named logger would drop INFO.
logger = logging.getLogger('yclients.dashboard')

router = APIRouter()

METRIC_VISIBILITY_CONFLICT_DETAIL = 'Visibility was changed by someone else; reload and try again'

# Which Overview preset produced the window. It picks the baseline the deltas measure
# against, so an unknown value is rejected rather than quietly falling back to another
# comparison. Absent means the dates were typed by hand. The set of presets lives with
# the baseline rule that consumes it, so the two cannot drift apart.
# Query() has to live inside the Annotated: as a default value it replaces the field and
# the validator below never runs, so `?period_preset=` would 422 the whole view.
PeriodPreset = Annotated[
    OverviewPeriodPreset | None,
    # An empty value means absent. A typo still fails loudly, because the preset decides
    # which window every delta on the page is measured against.
    BeforeValidator(lambda value: value or None),
    Query(description='Preset the window came from'),
]


class MetricVisibilityPayload(BaseModel):
    role: str
    visible_codes: list[str]


class BranchOrderPayload(BaseModel):
    # A tenant has a handful of branches; the cap only keeps an absurd body from reaching the comparison.
    company_ids: list[int] = Field(max_length=1000)


class ManualFactItem(BaseModel):
    company_id: int
    staff_id: int
    # Strict: lax float parsing turns `true` into one review.
    value: float | None = Field(None, strict=True)
    # What the last GET showed for this row: the save is an edit only where `value` differs.
    previous_value: float | None = Field(None, strict=True)


class ManualFactsPayload(BaseModel):
    month: str
    company_id: int | None = None
    staff_id: int | None = None
    items: list[ManualFactItem]


ISO_DAY = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _iso_day(value: Any) -> Any:
    """Only a 'YYYY-MM-DD' string is a day: lax date parsing would also take a number as a timestamp."""
    if value is None:
        return None
    if isinstance(value, str) and ISO_DAY.match(value):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError('must be an ISO date (YYYY-MM-DD)')


IsoDay = Annotated[date | None, BeforeValidator(_iso_day)]


class YandexPayItem(BaseModel):
    company_id: int
    # Strict: lax float parsing turns `true` into one rouble.
    value: float | None = Field(None, strict=True)
    previous_value: float | None = Field(None, strict=True)
    # The last day `value` covers; the optimistic lock is the pair, so the date has its previous twin.
    data_through: IsoDay = None
    previous_data_through: IsoDay = None


class YandexPayPayload(BaseModel):
    month: str
    company_id: int | None = None
    items: list[YandexPayItem]


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
    extra_services_qty: Any = None
    extra_services_pct: Any = None


class PlanSettingsPayload(BaseModel):
    month: str
    copy_from: str | None = Field(None, pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
    branches: list[PlanSettingsBranchPayload]
    staff: list[PlanSettingsStaffPayload]


class ServiceManagementRowChange(BaseModel):
    company_id: int
    service_id: int
    is_extra: bool


class ServiceManagementPayload(BaseModel):
    row_changes: list[ServiceManagementRowChange] = Field(default_factory=list)

    @model_validator(mode='after')
    def validate_unique_targets(self):
        row_keys = [(item.company_id, item.service_id) for item in self.row_changes]
        if len(row_keys) != len(set(row_keys)):
            raise ValueError('duplicate service row change')
        return self


def _parse_range(start: date, end: date) -> tuple[date, date]:
    if start > end:
        raise HTTPException(status_code=400, detail='start_date must be <= end_date')
    # Every window is half-open (`end + 1 day`) and has a baseline before `start`; at the ends of the
    # calendar that arithmetic overflows into a 500.
    if start <= date.min or end >= date.max:
        raise HTTPException(status_code=400, detail='start_date and end_date are out of the supported range')
    return start, end


def _require_settings_admin(ctx: AccessContext) -> None:
    if not (ctx.full_access or ctx.role in USER_ADMIN_ROLES):
        raise HTTPException(status_code=403, detail='Settings are not allowed for this role')


def _require_manual_fact_access(ctx: AccessContext) -> frozenset[tuple[int, int]] | None:
    """Rows the caller may see and edit in the reviews / additional-OPZ editors.

    ``None`` is the branch-wide scope the settings admins and branch managers get. A set is
    the self-service scope: a staff member enters their own value and nobody else's; 403 is
    for a portal user owning no staff row at all.

    Which of those rows the editors actually show is decided per requested month downstream,
    by the same administrator rule the write path applies — so a barber reaches this far and
    gets an empty table, while the portal keeps the tabs off their screen entirely
    (`manual_fact_entry_keys`).
    """
    staff_keys = manual_fact_staff_keys(ctx)
    if staff_keys is not None and not staff_keys:
        raise HTTPException(status_code=403, detail='Manual facts are not available')
    return staff_keys


def _require_branch_payments_access(ctx: AccessContext) -> None:
    """The one gate for payment totals: report catalog, report data, editor and `/auth/me`."""
    if not can_view_branch_payments(ctx):
        raise HTTPException(status_code=403, detail='Payment totals are not allowed for this role')


def _assert_manual_fact_staff_allowed(
    staff_keys: frozenset[tuple[int, int]] | None,
    staff_id: int | None,
) -> None:
    """The «Работник» filter, which carries no company of its own — hence the id-only match.

    Its sibling `_assert_manual_fact_rows_allowed` matches the whole pair, because payload rows
    do carry one. `Staff.id` is a global primary key, so the id alone still identifies the row.
    """
    if staff_keys is None or staff_id is None:
        return
    if not any(staff_id == owned_staff_id for _, owned_staff_id in staff_keys):
        raise HTTPException(status_code=403, detail='Staff member not allowed')


def _assert_manual_fact_rows_allowed(
    staff_keys: frozenset[tuple[int, int]] | None,
    items: list[Any],
) -> None:
    """A row the caller does not own is an access denial, not a malformed payload.

    The service repeats the check as a safety net for any other caller, but it can only
    answer with a `ValueError` → 400 carrying an internal English sentence, which the SPA
    shows verbatim. Denying here keeps the status honest for the access log too.
    """
    if staff_keys is None:
        return
    for item in items:
        if (item.company_id, item.staff_id) not in staff_keys:
            raise HTTPException(status_code=403, detail='Staff member not allowed')


MAX_STAFF_POSITION_IDS = 200


def _parse_staff_position_ids(raw: str | None) -> frozenset[int] | None:
    """`staff_position_ids` query value: comma-separated positive ints, `None` when absent or empty."""
    if raw is None or not raw.strip():
        return None
    ids: set[int] = set()
    for part in raw.split(','):
        part = part.strip()
        # The length check comes first: `int()` of a thousands-digit string raises instead of answering.
        if not (part.isascii() and part.isdigit() and len(part) <= 10 and 0 < int(part) <= 2**31 - 1):
            raise HTTPException(status_code=400, detail='staff_position_ids must be comma-separated positive integers')
        ids.add(int(part))
    if len(ids) > MAX_STAFF_POSITION_IDS:
        raise HTTPException(status_code=400, detail=f'staff_position_ids takes at most {MAX_STAFF_POSITION_IDS} ids')
    return frozenset(ids)


def _hide_summary_financials(summary: dict[str, Any], hidden_codes: frozenset[str]) -> dict[str, Any]:
    payload = deepcopy(summary)
    for key in money_payload_keys(hidden_codes, 'summary'):
        payload.pop(key, None)
    if 'revenue' in hidden_codes:
        # The whole `average_check` block survives on `avg_check`, but it carries the income it
        # was divided from. Hiding revenue at the top level and shipping it one key deeper is
        # not hiding it.
        average_check = payload.get('average_check')
        if isinstance(average_check, dict):
            for field in AVERAGE_CHECK_REVENUE_FIELDS:
                average_check.pop(field, None)
    personal = payload.get('administrator_personal')
    if isinstance(personal, dict):
        # An administrator's own Plan/fact facts carry the money metrics under their plan keys.
        for code in money_payload_keys(hidden_codes, 'plan'):
            personal['metrics'].pop(code, None)
    payload['financials_hidden'] = True
    return payload


def _strip_plan_fact_financials(value: Any, hidden_plan_codes: set[str], drop_all_money: bool) -> Any:
    if isinstance(value, list):
        items = []
        for item in value:
            stripped = _strip_plan_fact_financials(item, hidden_plan_codes, drop_all_money)
            if stripped is not None:
                items.append(stripped)
        return items
    if isinstance(value, dict):
        code = value.get('code')
        if code in hidden_plan_codes or (drop_all_money and value.get('format') == 'money'):
            return None
        result: dict[str, Any] = {}
        for key, item in value.items():
            stripped = _strip_plan_fact_financials(item, hidden_plan_codes, drop_all_money)
            if stripped is not None:
                result[key] = stripped
        return result
    return value


def _hide_plan_fact_financials(plan_fact: dict[str, Any], hidden_codes: frozenset[str]) -> dict[str, Any]:
    # The widget never carries `staff_leaderboards` (only the ratings report asks `fetch_plan_fact` for them,
    # and `_hide_staff_leaderboard_financials` covers that report), so only plan cells are money here.
    hidden_plan_codes = money_payload_keys(hidden_codes, 'plan')
    drop_all_money = ALL_MONEY_CODES <= hidden_codes
    payload = _strip_plan_fact_financials(plan_fact, hidden_plan_codes, drop_all_money) or {}
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
            _remove_table_field(extra_table, 'avg', metric='avg')

    payload['tables'] = [table for table in tables if table.get('id') not in hidden_table_ids]
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


async def _validated_widget_scope(
    db: AsyncSession,
    ctx: AccessContext,
    company_id: int | None,
    staff_id: int | None,
) -> tuple[dict[str, Any], int | None]:
    scope = query_scope(ctx, company_id)
    scoped_staff_id = effective_staff_id(ctx, staff_id)
    await _validate_dashboard_scope(
        db,
        scope['company_id'],
        scoped_staff_id,
        allowed_company_ids=scope['branch_ids'],
    )
    return scope, scoped_staff_id


async def _require_visibility_portal_account_id(
    db: AsyncSession, ctx: AccessContext, x_portal_account_id: int | None
) -> int:
    """Tenant of a metric-visibility route. An API key has none of its own, so it has to name one."""
    portal_account_id = ctx.portal_account_id
    if portal_account_id is None and ctx.full_access and x_portal_account_id is not None:
        if await db.get(PortalAccount, x_portal_account_id) is None:
            raise HTTPException(status_code=400, detail='unknown X-Portal-Account-Id')
        portal_account_id = x_portal_account_id
    if portal_account_id is None:
        raise HTTPException(status_code=400, detail='X-Portal-Account-Id is required')
    return portal_account_id


@contextmanager
def _value_error_as_400() -> Iterator[None]:
    """Answer a `ValueError` the service raised for a rejected input as 400 with its message.

    Wrap the whole try/except of a route, not a part of it: handlers for narrower exceptions
    (conflicts, closed rows) must see them first, even when they subclass `ValueError`.
    """
    try:
        yield
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    # The dates are optional so that callers which only administer staff keep the whole
    # scope; the dashboard filters pass them and get the branches that period can report on.
    start, end = (None, None) if start_date is None or end_date is None else _parse_range(start_date, end_date)
    scope = query_scope(ctx, company_id)
    return {
        'success': True,
        'data': await fetch_staff(
            db,
            scope['company_id'],
            allowed_company_ids=scope['branch_ids'],
            force_allowed=scope['force_allowed'],
            start=start,
            end=end,
        ),
    }


@router.get('/services')
async def dashboard_services(
    company_id: int | None = Query(None),
    q: str | None = Query(None),
    category: str | None = Query(None),
    is_extra: bool | None = Query(None),
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
            allowed_company_ids=scope['allowed_company_ids'],
        ),
    }


@router.patch('/services', dependencies=[Depends(forbid_demo)])
async def dashboard_services_save(
    payload: ServiceManagementPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    """Atomically persist dashboard-managed service labels."""
    _require_settings_admin(ctx)
    allowed_company_ids, _ = user_branch_ids(ctx)
    with _value_error_as_400():
        data = await save_service_management(
            db,
            row_changes=[item.model_dump() for item in payload.row_changes],
            allowed_company_ids=allowed_company_ids,
        )
    return {'success': True, 'data': data}


@router.get('/reports')
async def dashboard_reports(
    ctx: AccessContext = Depends(get_dashboard_access),
    is_demo: bool = Depends(is_demo_request),
):
    """Report catalog for the product reports SPA, narrowed to what the role may open."""
    return {
        'success': True,
        'data': fetch_report_registry(
            is_demo,
            hide_financials=not can_view_financials(ctx),
            hide_branch_payments=not can_view_branch_payments(ctx),
            hide_report_usage=not can_view_report_usage(ctx),
        ),
    }


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
    compare_previous: bool = Query(
        False, description='Compare with the Overview baseline of the period; an explicit compare window wins'
    ),
    period_preset: PeriodPreset = None,
    staff_position_ids: str | None = Query(None, description='Comma-separated barber level position ids'),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
    is_demo: bool = Depends(is_demo_request),
):
    start, end = _parse_range(start_date, end_date)
    position_ids = _parse_staff_position_ids(staff_position_ids)
    # Every gate speaks about the canonical report: a retired id must not dodge the check
    # of the report it now resolves to, nor inherit the one it used to have.
    canonical_id = resolve_report_id(report_id)
    if canonical_id is None:
        raise HTTPException(status_code=400, detail='unknown report_id')
    # The catalog already hides these for demo; block the direct URL too so a
    # bookmark cannot surface a report the demo data can never populate.
    if is_demo and canonical_id in DEMO_UNAVAILABLE_REPORTS:
        raise HTTPException(status_code=404, detail='Report is not available in the demo tenant')
    if report_requires_financials(canonical_id):
        require_financial_access(ctx)
    if canonical_id in BRANCH_PAYMENT_REPORTS:
        _require_branch_payments_access(ctx)
    if granularity not in REPORT_GRANULARITIES:
        raise HTTPException(status_code=400, detail='granularity must be one of day, week, month')
    if canonical_id == REPORT_USAGE_ID:
        # Tenant-wide analytics: no branch scope to resolve, and the registry builder cannot run it.
        if not can_view_report_usage(ctx):
            raise HTTPException(status_code=403, detail='Report usage is not available for this role')
        return {
            'success': True,
            'data': await fetch_report_usage(db, require_tenant_context(ctx), start, end, granularity),
        }
    # What the client asked for, noted before the clamps below rewrite it.
    requested_staff_filter = staff_id is not None
    compare_requested = bool(
        compare_start_date or compare_end_date or compare_staff_id is not None or compare_previous
    )
    scope = query_scope(ctx, company_id)
    staff_id = effective_staff_id(ctx, staff_id)
    if compare_staff_id is not None:
        # Clamping an absent value would turn "no staff comparison" into a comparison with oneself.
        compare_staff_id = effective_staff_id(ctx, compare_staff_id)
    if (compare_start_date is None) ^ (compare_end_date is None):
        raise HTTPException(status_code=400, detail='compare_start_date and compare_end_date must be passed together')
    if compare_end_date is not None and compare_end_date >= date.max:
        # The comparison window is half-open too; its start has no baseline, so only the end can overflow.
        raise HTTPException(status_code=400, detail='compare_end_date is out of the supported range')
    await _validate_dashboard_scope(
        db,
        scope['company_id'],
        staff_id,
        compare_staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
    )
    with track_report_usage(
        ctx,
        is_demo=is_demo,
        report_id=canonical_id,
        requested_report_id=report_id,
        start=start,
        end=end,
        granularity=granularity,
        period_preset=period_preset,
        compare_used=compare_requested,
        staff_filter=requested_staff_filter,
        company_filter=company_id is not None,
    ) as usage:
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
                period_preset=period_preset,
                compare_previous=compare_previous,
                staff_position_ids=position_ids,
            )
        except ReportInputError as exc:
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
        usage.source_status = data.get('source_status')
    if canonical_id == 'staff_leaderboard':
        data = _hide_staff_leaderboard_financials(data, hidden_money_codes(ctx))
    return {'success': True, 'data': data}


async def _tenant_sync_health(
    db: AsyncSession, portal_account_id: int, company_ids: list[int] | None,
) -> dict[str, Any] | None:
    """The tenant's own sync freshness, or None when it has no credentials to sync with.

    The global run status says nothing about one tenant: a run that skipped a tenant whose
    credentials broke still ends `success`. Freshness is therefore the oldest per-branch
    checkpoint (written only when that branch's records, payments, goods and comments all
    loaded), and `failed` is the credential's own `needs_reauth` flag, which auth and decrypt
    failures set and the next successful auth clears.
    """
    credentials = (await db.execute(
        select(YClientsCredential.id, YClientsCredential.needs_reauth).where(
            YClientsCredential.portal_account_id == portal_account_id,
            YClientsCredential.is_active.is_(True),
        )
    )).all()
    if not credentials:
        return None
    linked = set((await db.execute(
        select(YClientsCredentialCompany.company_id).where(
            YClientsCredentialCompany.credential_id.in_([row.id for row in credentials])
        )
    )).scalars().all())
    if company_ids is not None:
        linked &= set(company_ids)
    keys = [transactional_state_key(company_id) for company_id in sorted(linked)]
    checkpoints = {}
    if keys:
        checkpoints = dict((await db.execute(
            select(SyncState.key, SyncState.updated_at).where(SyncState.key.in_(keys))
        )).all())
    # A branch that never completed a pass has no checkpoint, and the tenant is only as fresh as it.
    stamps = [checkpoints.get(key) for key in keys]
    fresh_at = min(stamps) if stamps and all(stamps) else None
    return {
        'last_successful_sync_at': serialize_dt(fresh_at),
        'failed': any(row.needs_reauth for row in credentials),
    }


@router.get('/widget/sync_status')
async def dashboard_widget_sync_status(
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    portal_account_id = require_tenant_context(ctx, allow_full_access=True)
    sync_payload = await asyncio.to_thread(get_sync_status)
    # The run is global, so tenants get only what the header line needs: no log path, run
    # message (raw exception text), initiator or job queue. /sync/status keeps the full payload.
    last_run = sync_payload.get('last_run')
    sync = {
        'running': sync_payload['running'],
        'last_successful_sync_at': sync_payload.get('last_successful_sync_at'),
        'last_run': {'status': last_run['status'], 'finished_at': last_run['finished_at']} if last_run else None,
        'tenant_sync_failed': False,
    }
    health = await _tenant_sync_health(db, portal_account_id, ctx.company_ids) if portal_account_id else None
    if health is not None:
        # The global timestamp and run would read as this tenant's even when it was skipped; a
        # null last_run also keeps the page from falling back to the global run's finish time.
        sync.update(
            last_successful_sync_at=health['last_successful_sync_at'],
            last_run=None,
            tenant_sync_failed=health['failed'],
        )
    return {'success': True, 'data': {'sync': sync}}


@router.get('/widget/plan_fact')
async def dashboard_widget_plan_fact(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    staff_position_ids: str | None = Query(None, description='Comma-separated barber level position ids'),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    start, end = _parse_range(start_date, end_date)
    position_ids = _parse_staff_position_ids(staff_position_ids)
    scope, staff_id = await _validated_widget_scope(db, ctx, company_id, staff_id)
    branch_ids, force_allowed = user_branch_ids(ctx)
    plan_fact = await fetch_plan_fact(
        db,
        start,
        end,
        scope['company_id'],
        staff_id,
        allowed_company_ids=branch_ids,
        force_allowed=force_allowed,
        # `effective_staff_id` clamped a personal role to its own row: whole-branch totals are not its to read.
        include_branch_row=ctx.full_access or ctx.role in BRANCH_SCOPE_ROLES,
        staff_position_ids=position_ids,
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
    with _value_error_as_400():
        data = await fetch_plan_settings(
            db,
            month,
            copy_from,
            allowed_company_ids=branch_ids,
            force_allowed=force_allowed,
        )
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
    with _value_error_as_400():
        try:
            data = await save_plan_settings(
                db,
                payload.month,
                [item.model_dump() for item in payload.branches],
                [item.model_dump() for item in payload.staff],
                allowed_company_ids=branch_ids,
                force_allowed=force_allowed,
                replace_existing_plan=payload.copy_from is not None,
                actor_user_id=ctx.user_id,
                portal_account_id=ctx.portal_account_id,
            )
        except PlanSettingsConflict as exc:
            logger.warning('plan settings save refused, concurrent save: month=%s user=%s', payload.month, ctx.user_id)
            raise HTTPException(status_code=409, detail=PLAN_SETTINGS_CONFLICT_DETAIL) from exc
    return {'success': True, 'data': data}


async def _manual_fact_get(
    db: AsyncSession,
    ctx: AccessContext,
    fetch_fn: Callable[..., Any],
    month: str,
    company_id: int | None,
    staff_id: int | None,
) -> dict[str, Any]:
    """Shared read path for the reviews / additional-OPZ editors — only `fetch_fn` differs.

    Scope is checked against the database (`_validate_dashboard_scope`) because the editor
    must be able to say "unknown branch" before anyone types a value, unlike the save path
    below, which validates rows against the staff/company keys it already owns.
    """
    staff_keys = _require_manual_fact_access(ctx)
    scope = query_scope(ctx, company_id)
    _assert_manual_fact_staff_allowed(staff_keys, staff_id)
    await _validate_dashboard_scope(db, scope['company_id'], staff_id, allowed_company_ids=scope['branch_ids'])
    branch_ids, force_allowed = user_branch_ids(ctx)
    with _value_error_as_400():
        data = await fetch_fn(
            db,
            month,
            scope['company_id'],
            staff_id,
            allowed_company_ids=branch_ids,
            force_allowed=force_allowed,
            allowed_staff_keys=staff_keys,
            portal_account_id=ctx.portal_account_id,
        )
    return {'success': True, 'data': data}


async def _manual_fact_post(
    db: AsyncSession,
    ctx: AccessContext,
    metric_code: str,
    month: str,
    company_id: int | None,
    staff_id: int | None,
    items: list[Any],
) -> dict[str, Any]:
    """Shared write path for the reviews / additional-OPZ editors — only `metric_code` differs.

    Deliberately does not call `_validate_dashboard_scope`, unlike `_manual_fact_get` above:
    `save_manual_facts` rejects any row whose (company_id, staff_id) is not a currently open one via
    `ManualFactRowNotOpen`, so an unknown company or staff id is already refused downstream.
    """
    staff_keys = _require_manual_fact_access(ctx)
    scope = query_scope(ctx, company_id)
    _assert_manual_fact_staff_allowed(staff_keys, staff_id)
    _assert_manual_fact_rows_allowed(staff_keys, items)
    branch_ids, force_allowed = user_branch_ids(ctx)
    with _value_error_as_400():
        try:
            data = await save_manual_facts(
                db,
                month,
                metric_code,
                scope['company_id'],
                staff_id,
                [item.model_dump(exclude_unset=True) for item in items],
                allowed_company_ids=branch_ids,
                force_allowed=force_allowed,
                allowed_staff_keys=staff_keys,
                actor_user_id=ctx.user_id,
                portal_account_id=ctx.portal_account_id,
            )
        except ManualFactConflict as exc:
            logger.info('manual fact save refused, stale value: month=%s user=%s', month, ctx.user_id)
            raise HTTPException(status_code=409, detail=MANUAL_FACT_CONFLICT_DETAIL) from exc
        except ManualFactRowNotOpen as exc:
            # The reason names staff and branch ids, and the SPA prints `detail` verbatim —
            # the editors are open to rank-and-file staff now, so it belongs in the log.
            logger.info('manual fact row not open for entry: %s', exc)
            raise HTTPException(status_code=400, detail='Manual fact row is not open for entry') from exc
    return {'success': True, 'data': data}


@router.get('/plan/reviews_fact')
async def dashboard_plan_reviews_fact(
    month: str = Query(..., description='Reviews fact month in YYYY-MM format'),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    return await _manual_fact_get(db, ctx, fetch_manual_review_facts, month, company_id, staff_id)


@router.post('/plan/reviews_fact', dependencies=[Depends(forbid_demo)])
async def dashboard_plan_reviews_fact_save(
    payload: ManualFactsPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    return await _manual_fact_post(
        db, ctx, REVIEWS_QTY_CODE, payload.month, payload.company_id, payload.staff_id, payload.items
    )


@router.get('/plan/opz_fact')
async def dashboard_plan_opz_fact(
    month: str = Query(..., description='Additional OPZ month in YYYY-MM format'),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    return await _manual_fact_get(db, ctx, fetch_manual_opz_facts, month, company_id, staff_id)


@router.post('/plan/opz_fact', dependencies=[Depends(forbid_demo)])
async def dashboard_plan_opz_fact_save(
    payload: ManualFactsPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    return await _manual_fact_post(
        db, ctx, OPZ_QTY_CODE, payload.month, payload.company_id, payload.staff_id, payload.items
    )


async def _yandex_pay_editor(
    db: AsyncSession,
    ctx: AccessContext,
    month: str,
    company_id: int | None,
) -> dict[str, Any]:
    branch_ids, force_allowed = user_branch_ids(ctx)
    with _value_error_as_400():
        data = await fetch_yandex_pay_editor(
            db,
            month,
            company_id,
            allowed_company_ids=branch_ids,
            force_allowed=force_allowed,
            portal_account_id=ctx.portal_account_id,
        )
    return {'success': True, 'data': data}


@router.get('/payments/yandex_pay')
async def dashboard_yandex_pay(
    month: str = Query(..., description='Month in YYYY-MM format'),
    company_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_branch_payments_access(ctx)
    scope = query_scope(ctx, company_id)
    await _validate_dashboard_scope(db, scope['company_id'], None, allowed_company_ids=scope['branch_ids'])
    return await _yandex_pay_editor(db, ctx, month, scope['company_id'])


@router.post('/payments/yandex_pay', dependencies=[Depends(forbid_demo)])
async def dashboard_yandex_pay_save(
    payload: YandexPayPayload,
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_branch_payments_access(ctx)
    scope = query_scope(ctx, payload.company_id)
    if not ctx.full_access:
        # A row of someone else's branch is a denial, not a malformed payload — and it must not
        # reach the service, whose only answer would be a 400 with an internal sentence.
        allowed = set(ctx.company_ids or [])
        if any(item.company_id not in allowed for item in payload.items):
            raise HTTPException(status_code=403, detail='Branch not allowed')
    branch_ids, force_allowed = user_branch_ids(ctx)
    with _value_error_as_400():
        try:
            await save_yandex_pay(
                db,
                payload.month,
                scope['company_id'],
                [item.model_dump() for item in payload.items],
                allowed_company_ids=branch_ids,
                force_allowed=force_allowed,
                actor_user_id=ctx.user_id,
                portal_account_id=ctx.portal_account_id,
            )
        except ManualPaymentConflict as exc:
            logger.info('yandex pay save refused, stale value: month=%s user=%s', payload.month, ctx.user_id)
            raise HTTPException(status_code=409, detail=CONFLICT_DETAIL) from exc
        except PaymentRowNotOpen as exc:
            logger.info('yandex pay row not open for entry: %s', exc)
            raise HTTPException(status_code=400, detail='Payment row is not open for entry') from exc
    return await _yandex_pay_editor(db, ctx, payload.month, scope['company_id'])


def _require_visibility_admin(ctx: AccessContext, action: str = 'configure metric visibility') -> None:
    if not (ctx.full_access or ctx.role in ('owner', 'platform_admin')):
        raise HTTPException(status_code=403, detail=f'Not allowed to {action}')


@router.get('/metric-visibility')
async def dashboard_metric_visibility(
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_visibility_admin(ctx)
    portal_account_id = await _require_visibility_portal_account_id(db, ctx, x_portal_account_id)
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
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_visibility_admin(ctx)
    portal_account_id = await _require_visibility_portal_account_id(db, ctx, x_portal_account_id)
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
            updated_at=utcnow(),
        )
        db.add(row)
    else:
        row.visible_codes = visible_codes
        row.updated_at = utcnow()
    await log_portal_audit(
        db,
        actor_user_id=ctx.user_id,
        portal_account_id=portal_account_id,
        action='metric_visibility.updated',
        target_type='role',
        target_id=payload.role,
        metadata={'visible_codes': visible_codes},
    )
    try:
        await db.commit()
    except IntegrityError:
        # Two first saves of the same (tenant, role) both saw no row; the second one hits the unique index.
        await db.rollback()
        raise HTTPException(status_code=409, detail=METRIC_VISIBILITY_CONFLICT_DETAIL) from None
    return {'success': True, 'data': {'role': payload.role, 'visible_codes': visible_codes}}


@router.get('/branch-order')
async def dashboard_branch_order(
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_visibility_admin(ctx, 'configure the branch order')
    portal_account_id = await _require_visibility_portal_account_id(db, ctx, x_portal_account_id)
    return {'success': True, 'data': await fetch_tenant_branch_order(db, portal_account_id)}


@router.put('/branch-order', dependencies=[Depends(forbid_demo)])
async def dashboard_branch_order_save(
    payload: BranchOrderPayload,
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    _require_visibility_admin(ctx, 'configure the branch order')
    portal_account_id = await _require_visibility_portal_account_id(db, ctx, x_portal_account_id)
    with _value_error_as_400():
        company_ids = await save_branch_order(db, portal_account_id, payload.company_ids)
        await log_portal_audit(
            db,
            actor_user_id=ctx.user_id,
            portal_account_id=portal_account_id,
            action='branch_order.updated',
            target_type='portal_account',
            target_id=str(portal_account_id),
            metadata={'company_ids': company_ids},
        )
        await db.commit()
    return {'success': True, 'data': await fetch_tenant_branch_order(db, portal_account_id)}


@router.get('/bundle')
async def dashboard_bundle(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    period_preset: PeriodPreset = None,
    staff_position_ids: str | None = Query(None, description='Comma-separated barber level position ids'),
    db: AsyncSession = Depends(get_async_db),
    ctx: AccessContext = Depends(get_dashboard_access),
):
    start, end = _parse_range(start_date, end_date)
    position_ids = _parse_staff_position_ids(staff_position_ids)
    scope, staff_id = await _validated_widget_scope(db, ctx, company_id, staff_id)
    factual_at = dashboard_service.factual_now()
    level_filter = None
    if position_ids is not None and staff_id is None:
        # The selected barbers stand in for the staff filter as a group, the same people in every window. A selected
        # (or clamped) person wins and the level is ignored, as in Plan/fact. An empty group stays a group: it
        # matches nobody instead of falling back to the whole branch.
        group, titles = await staff_ids_for_levels(
            db, end, scope['company_id'], scope['allowed_company_ids'], position_ids
        )
        # `staff_count` counts the people the numbers are about: members still working or with a completed visit.
        staff_count = await level_group_staff_count(
            db, group, start, end, scope['company_id'], scope['allowed_company_ids'], factual_at
        )
        level_filter = {'position_ids': sorted(position_ids), 'titles': titles, 'staff_count': staff_count}
        staff_id = group
    summary = await fetch_summary(
        db,
        start,
        end,
        scope['company_id'],
        staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
        factual_at=factual_at,
        period_preset=period_preset,
    )
    if level_filter is not None:
        summary['level_filter'] = level_filter
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
        administrator_attribution=True,
    )
    if not can_see_financials:
        return {
            'success': True,
            'data': {
                'source_status': summary.get('source_status', 'ready'),
                'missing_sources': summary.get('missing_sources', []),
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
        use_administrator_schedule=True,
    )
    extra_services = await fetch_top_services(
        db,
        start,
        end,
        scope['company_id'],
        None,
        staff_id,
        allowed_company_ids=scope['allowed_company_ids'],
        factual_at=factual_at,
        use_administrator_schedule=True,
        only_extra=True,
    )
    return {
        'success': True,
        'data': {
            'source_status': summary.get('source_status', 'ready'),
            'missing_sources': summary.get('missing_sources', []),
            'summary': summary,
            'revenue_daily': daily,
            'top_services': services,
            'extra_services': extra_services,
        },
    }
