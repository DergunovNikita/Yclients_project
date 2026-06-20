"""Access scope resolution for branch-scoped dashboard queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException


@dataclass(frozen=True)
class AccessContext:
    """Resolved access for the current request."""

    user_id: int | None
    role: str | None
    full_access: bool
    company_ids: list[int] | None  # None = all branches; [] = none

    @classmethod
    def api_key(cls) -> AccessContext:
        return cls(user_id=None, role=None, full_access=True, company_ids=None)

    @classmethod
    def from_user(cls, user_id: int, role: str, company_ids: list[int] | None) -> AccessContext:
        full = role == 'super_admin'
        return cls(
            user_id=user_id,
            role=role,
            full_access=full,
            company_ids=None if full else (company_ids or []),
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
