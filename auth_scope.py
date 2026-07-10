"""Access scope resolution for branch-scoped dashboard queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException

FINANCIAL_ACCESS_ROLES = {'owner', 'platform_admin'}


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
        )

    @classmethod
    def from_user(
        cls,
        user_id: int,
        role: str,
        portal_account_id: int | None,
        company_ids: list[int] | None,
        staff_id: int | None = None,
    ) -> AccessContext:
        is_platform_admin = role == 'platform_admin'
        return cls(
            user_id=user_id,
            role=role,
            portal_account_id=portal_account_id,
            staff_id=staff_id,
            is_platform_admin=is_platform_admin,
            full_access=False,
            company_ids=company_ids or [],
        )


@dataclass(frozen=True)
class CompanyScope:
    """Normalized company filter passed into dashboard_service."""

    company_id: int | None = None
    allowed_company_ids: list[int] | None = None


def build_company_scope(ctx: AccessContext, requested_company_id: int | None) -> CompanyScope:
    """Validate requested branch and return SQL scope for dashboard queries."""
    if ctx.full_access:
        return CompanyScope(company_id=requested_company_id)

    allowed = ctx.company_ids or []
    if not allowed:
        raise HTTPException(status_code=403, detail='No branch access assigned')

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


def query_scope(ctx: AccessContext, requested_company_id: int | None) -> dict[str, Any]:
    company_scope = build_company_scope(ctx, requested_company_id)
    branch_ids, force_allowed = user_branch_ids(ctx)
    return {
        'company_id': company_scope.company_id,
        'allowed_company_ids': company_scope.allowed_company_ids,
        'branch_ids': branch_ids,
        'force_allowed': force_allowed,
    }


def effective_staff_id(ctx: AccessContext, requested_staff_id: int | None) -> int | None:
    """Apply staff-level user scope when the current viewer is linked to a staff row."""
    if ctx.full_access or ctx.staff_id is None:
        return requested_staff_id
    if requested_staff_id is not None and int(requested_staff_id) != int(ctx.staff_id):
        raise HTTPException(status_code=403, detail='Staff member not allowed')
    return int(ctx.staff_id)


def can_view_financials(ctx: AccessContext) -> bool:
    """Return whether the current principal may see revenue and money metrics."""
    return ctx.full_access or ctx.role in FINANCIAL_ACCESS_ROLES


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
