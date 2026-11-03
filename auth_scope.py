"""Access scope resolution for branch-scoped dashboard queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

from plan_config import ALL_MONEY_CODES, default_money_codes_for_role

# Roles scoped to a whole branch rather than to one staff row. The complement is what
# `AccessContext.staff_id` / `staff_keys` are resolved for, so this decides the manual-fact
# editors (who may enter which rows), the branch payments and the branch totals of Plan/fact.
BRANCH_SCOPE_ROLES = ('platform_admin', 'owner', 'branch_admin', 'manager')

# Roles that are not clamped to their own staff row when reading the dashboard: the branch roles
# and the administrator, who reads every colleague of their branches (money stays per metric).
# `barber` is the only personal role left. This is read-only visibility; writing manual facts
# is still decided by `BRANCH_SCOPE_ROLES` / `manual_fact_staff_keys`.
STAFF_VIEW_ROLES = (*BRANCH_SCOPE_ROLES, 'admin')


@dataclass(frozen=True)
class AccessContext:
    """Resolved access for the current request."""

    user_id: int | None
    role: str | None
    portal_account_id: int | None
    staff_id: int | None
    is_platform_admin: bool
    full_access: bool
    company_ids: list[int] | None  # None = all branches; [] = none
    money_metrics: frozenset[str] = ALL_MONEY_CODES  # visible money metric codes
    staff_keys: tuple[tuple[int, int], ...] = ()  # (company_id, staff_id) rows the user owns
    is_demo: bool = False  # the shared read-only demo account

    @classmethod
    def api_key(cls) -> AccessContext:
        return cls(
            user_id=None,
            role=None,
            portal_account_id=None,
            staff_id=None,
            is_platform_admin=False,
            full_access=True,
            company_ids=None,
            money_metrics=ALL_MONEY_CODES,
        )

    @classmethod
    def from_user(
        cls,
        user_id: int,
        role: str,
        portal_account_id: int | None,
        company_ids: list[int] | None,
        staff_id: int | None = None,
        money_metrics: frozenset[str] | None = None,
        staff_keys: tuple[tuple[int, int], ...] = (),
        is_demo: bool = False,
    ) -> AccessContext:
        is_platform_admin = role == 'platform_admin'
        if money_metrics is None:
            money_metrics = default_money_codes_for_role(role)
        return cls(
            user_id=user_id,
            role=role,
            portal_account_id=portal_account_id,
            staff_id=staff_id,
            is_platform_admin=is_platform_admin,
            full_access=False,
            company_ids=company_ids or [],
            money_metrics=frozenset(money_metrics),
            staff_keys=tuple(staff_keys),
            is_demo=is_demo,
        )


@dataclass(frozen=True)
class CompanyScope:
    """Normalized company filter passed into dashboard_service."""

    company_id: int | None = None
    allowed_company_ids: list[int] | None = None


def build_company_scope(
    ctx: AccessContext,
    requested_company_id: int | None,
    requested_company_ids: frozenset[int] | None = None,
) -> CompanyScope:
    """Validate requested branch(es) and return SQL scope for dashboard queries.

    `requested_company_ids` is a chosen group of two or more branches: the network path restricted to it. A
    single branch always comes as `requested_company_id`, so one chosen branch behaves exactly like the scalar.
    """
    if ctx.full_access:
        if requested_company_ids:
            return CompanyScope(allowed_company_ids=sorted(requested_company_ids))
        return CompanyScope(company_id=requested_company_id)

    allowed = ctx.company_ids or []
    if not allowed:
        raise HTTPException(status_code=403, detail='No branch access assigned')

    if requested_company_ids:
        if not requested_company_ids <= set(allowed):
            raise HTTPException(status_code=403, detail='Branch not allowed')
        return CompanyScope(allowed_company_ids=sorted(requested_company_ids))

    if requested_company_id is not None:
        if requested_company_id not in allowed:
            raise HTTPException(status_code=403, detail='Branch not allowed')
        return CompanyScope(company_id=requested_company_id)

    if len(allowed) == 1:
        return CompanyScope(company_id=allowed[0])

    return CompanyScope(allowed_company_ids=allowed)


def user_branch_ids(ctx: AccessContext) -> tuple[list[int] | None, bool]:
    """Return branch ids and whether user-scoped filtering is enforced."""
    if ctx.full_access:
        return None, False
    return ctx.company_ids or [], True


def query_scope(
    ctx: AccessContext,
    requested_company_id: int | None,
    requested_company_ids: frozenset[int] | None = None,
) -> dict[str, Any]:
    company_scope = build_company_scope(ctx, requested_company_id, requested_company_ids)
    branch_ids, force_allowed = user_branch_ids(ctx)
    return {
        'company_id': company_scope.company_id,
        'allowed_company_ids': company_scope.allowed_company_ids,
        'branch_ids': branch_ids,
        'force_allowed': force_allowed,
    }


def effective_staff_selection(
    ctx: AccessContext,
    requested: int | frozenset[int] | None,
    company_id: int | None = None,
    allowed_company_ids: list[int] | None = None,
) -> int | frozenset[int] | None:
    """Clamp a personal role to its own staff rows inside the resolved branch scope.

    A role outside `STAFF_VIEW_ROLES` is personal by definition, so an unresolved staff row
    is a refusal, not a pass. YClients can fire an employee while their portal login stays
    active — `load_portal_user_staff_rows` then returns nothing — and falling through would
    hand that stale login the whole branch instead of one row.

    A person owns one row per branch they work in, so the clamp is a set, narrowed to the request's branch
    scope (`company_id` or `allowed_company_ids`; the user's own branches when neither is given). Nothing
    requested gives all those rows: one is returned as an `int`, several as a `frozenset`. A requested person
    or group must lie inside them, otherwise 403. Roles that view the whole branch (`STAFF_VIEW_ROLES`) and
    the API key keep what they asked for; the caller still validates it against the branch scope.
    """
    if ctx.full_access or ctx.role in STAFF_VIEW_ROLES:
        return requested
    if ctx.staff_id is None and not ctx.staff_keys:
        raise HTTPException(status_code=403, detail='No staff row assigned')
    if ctx.staff_keys:
        scope_ids = {company_id} if company_id is not None else set(
            allowed_company_ids if allowed_company_ids is not None else ctx.company_ids or []
        )
        own = {int(staff_id) for row_company_id, staff_id in ctx.staff_keys if row_company_id in scope_ids}
    else:
        own = {int(ctx.staff_id)}
    if not own:
        raise HTTPException(status_code=403, detail='No staff row assigned')
    if requested is None:
        return next(iter(own)) if len(own) == 1 else frozenset(own)
    if not ({int(requested)} if isinstance(requested, int) else set(requested)) <= own:
        raise HTTPException(status_code=403, detail='Staff member not allowed')
    return requested


def manual_fact_staff_keys(ctx: AccessContext) -> frozenset[tuple[int, int]] | None:
    """Rows the principal may enter manual facts for.

    ``None`` means the whole branch scope, the way plan settings already work. A set means
    only these ``(company_id, staff_id)`` pairs — a staff member reporting for themselves owns
    one row per branch they work in, so the answer is a set and not a single staff id.
    """
    if ctx.full_access or ctx.role in BRANCH_SCOPE_ROLES:
        return None
    allowed_companies = ctx.company_ids
    return frozenset(
        (company_id, staff_id)
        for company_id, staff_id in ctx.staff_keys
        if allowed_companies is None or company_id in allowed_companies
    )


def can_view_money_metric(ctx: AccessContext, code: str) -> bool:
    """Return whether the current principal may see a specific money metric."""
    return ctx.full_access or code in ctx.money_metrics


def hidden_money_codes(ctx: AccessContext) -> frozenset[str]:
    """Money metric codes the current principal must not see."""
    if ctx.full_access:
        return frozenset()
    return ALL_MONEY_CODES - ctx.money_metrics


def can_view_financials(ctx: AccessContext) -> bool:
    """Coarse revenue gate: whether the principal may see revenue-level money data."""
    return can_view_money_metric(ctx, 'revenue')


def can_view_branch_payments(ctx: AccessContext) -> bool:
    """Whether the principal may see branch-level payment totals and enter Yandex Pay.

    Revenue visibility alone is not enough: `admin`/`barber` are not branch-level roles. Branch
    cash/cashless totals are owner-side reporting that an administrator, who reads colleagues'
    counts but not revenue, must not get just because the owner opened `revenue`. One predicate
    guards the report catalog, the report data, the editor and the `/auth/me` flag, so the four
    never disagree.
    """
    return ctx.full_access or (ctx.role in BRANCH_SCOPE_ROLES and can_view_financials(ctx))


def can_view_report_usage(ctx: AccessContext) -> bool:
    """Whether the principal may read the report-usage analytics of a tenant.

    Platform operators only: tenant owners do not see it. The global API key is not a person
    and has no tenant, so it never qualifies.
    """
    return not ctx.full_access and ctx.role == 'platform_admin'


def require_financial_access(ctx: AccessContext) -> None:
    if not can_view_financials(ctx):
        raise HTTPException(status_code=403, detail='Financial metrics are not allowed for this role')


def require_tenant_context(ctx: AccessContext, *, allow_full_access: bool = False) -> int | None:
    """Return active tenant id for tenant-scoped endpoints."""
    if ctx.full_access:
        if allow_full_access:
            return None
        raise HTTPException(status_code=400, detail='Tenant scope is required')
    if ctx.is_platform_admin and ctx.portal_account_id is None:
        raise HTTPException(status_code=400, detail='X-Portal-Account-Id is required')
    if ctx.portal_account_id is None:
        raise HTTPException(status_code=403, detail='Tenant account is required')
    return ctx.portal_account_id


def require_sync_company_ids(ctx: AccessContext, requested_company_ids: list[int] | None = None) -> list[int] | None:
    """Validate optional sync branch scope against the current auth context."""
    requested = [int(item) for item in dict.fromkeys(requested_company_ids or [])]
    if ctx.full_access:
        return requested or None
    allowed = ctx.company_ids or []
    if not allowed:
        raise HTTPException(status_code=403, detail='No branch access assigned')
    if requested:
        forbidden = sorted(set(requested) - set(allowed))
        if forbidden:
            raise HTTPException(status_code=403, detail=f'Branches outside scope: {forbidden}')
        return requested
    return allowed
