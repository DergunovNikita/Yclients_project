"""FastAPI dependencies for portal authentication and authorization."""

from __future__ import annotations

from datetime import datetime

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jwt
from auth_scope import AccessContext, BRANCH_SCOPE_ROLES
from auth_service import (
    decode_access_token,
    load_portal_account_branch_ids,
    load_portal_user_staff_rows,
    load_user_access_branch_ids,
)
from auth_sessions import ACCESS_COOKIE_NAME, enforce_csrf
from config import API_KEY, AUTH_REQUIRE_LOGIN
from database import get_async_db
from open_paths import OPEN_PATHS
from models import PortalMetricVisibility, PortalRefreshToken, PortalUser
from plan_config import ALL_MONEY_CODES, CONFIGURABLE_MONEY_ROLES, default_money_codes_for_role
from sync_security import secure_equals


async def _user_from_token(
    token: str,
    db: AsyncSession,
    x_portal_account_id: int | None = None,
) -> tuple[PortalUser, AccessContext]:
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail='Invalid or expired token') from exc

    try:
        user_id = int(payload['sub'])
        session_id = int(payload['sid'])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail='Invalid or expired token') from exc

    user_stmt = (
        select(PortalUser)
        .join(PortalRefreshToken, PortalRefreshToken.user_id == PortalUser.id)
        .where(
            PortalUser.id == user_id,
            PortalRefreshToken.id == session_id,
            PortalRefreshToken.revoked_at.is_(None),
            PortalRefreshToken.expires_at > datetime.utcnow(),
        )
    )
    user = (await db.execute(user_stmt)).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail='User not found or inactive')

    claim_tv = int(payload.get('tv', 0) or 0)
    if claim_tv != int(user.token_version or 0):
        raise HTTPException(status_code=401, detail='Session invalidated')

    if user.role == 'platform_admin':
        active_account_id = x_portal_account_id
        if active_account_id is not None:
            branch_ids = await load_portal_account_branch_ids(db, active_account_id)
            return user, AccessContext.from_user(
                user.id, user.role, active_account_id, branch_ids, is_demo=bool(user.is_demo)
            )
        return user, AccessContext.from_user(user.id, user.role, None, None, is_demo=bool(user.is_demo))

    branch_ids = await load_user_access_branch_ids(db, user)
    staff_keys: tuple[tuple[int, int], ...] = ()
    staff_id = None
    if user.role not in BRANCH_SCOPE_ROLES:
        # One staff row per branch, so someone working in two points owns two of them.
        staff_rows = await load_portal_user_staff_rows(db, user.id)
        staff_keys = tuple(
            (company_id, row_staff_id)
            for company_id, row_staff_id in staff_rows
            if company_id is not None
        )
        # The `effective_staff_id` clamp must land inside the assigned branches. A provisioned
        # employee keeps the CRM row of the branch they left (portal_staff_sync keeps it alive
        # on purpose) and that row always has the lower id, so taking the first one blindly
        # made every dashboard query answer `unknown staff_id`. Falling back to any row still
        # clamps — an unclamped context would open the whole branch instead.
        in_scope = [
            row_staff_id
            for company_id, row_staff_id in staff_keys
            if company_id in (branch_ids or ())
        ]
        staff_id = in_scope[0] if in_scope else (staff_rows[0][1] if staff_rows else None)
    money_metrics = await _resolve_money_metrics(db, user.portal_account_id, user.role)
    return user, AccessContext.from_user(
        user.id,
        user.role,
        user.portal_account_id,
        branch_ids,
        staff_id=staff_id,
        money_metrics=money_metrics,
        staff_keys=staff_keys,
        is_demo=bool(user.is_demo),
    )


async def _resolve_money_metrics(
    db: AsyncSession,
    portal_account_id: int | None,
    role: str,
) -> frozenset[str]:
    """Resolve visible money metrics: tenant config override, else role default."""
    if role not in CONFIGURABLE_MONEY_ROLES or portal_account_id is None:
        return default_money_codes_for_role(role)
    stored = await db.scalar(
        select(PortalMetricVisibility.visible_codes).where(
            PortalMetricVisibility.portal_account_id == portal_account_id,
            PortalMetricVisibility.role == role,
        )
    )
    if stored is None:
        return default_money_codes_for_role(role)
    return frozenset(code for code in stored if code in ALL_MONEY_CODES)


def _extract_access_token(request: Request, authorization: str | None) -> str | None:
    """Bearer header wins so explicit auth always takes precedence over cookies."""
    if authorization and authorization.lower().startswith('bearer '):
        return authorization.split(' ', 1)[1].strip()
    return request.cookies.get(ACCESS_COOKIE_NAME)


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> AccessContext | None:
    """Global auth: JWT user (header or cookie), API key (full access), or open paths."""
    if request.url.path in OPEN_PATHS:
        return None

    token = _extract_access_token(request, authorization)
    if token:
        user, ctx = await _user_from_token(token, db, x_portal_account_id)
        enforce_csrf(request)
        request.state.access = ctx
        request.state.portal_user = user
        return ctx

    if API_KEY:
        if secure_equals(x_api_key, API_KEY):
            ctx = AccessContext.api_key()
            request.state.access = ctx
            return ctx
        raise HTTPException(status_code=401, detail='Invalid API key')

    if not AUTH_REQUIRE_LOGIN:
        return None

    raise HTTPException(status_code=401, detail='Authentication required')


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> PortalUser:
    # `require_auth` already loaded the user and checked CSRF for this very token.
    user = getattr(request.state, 'portal_user', None)
    if user is not None:
        return user
    token = _extract_access_token(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail='Authentication required')
    user, _ = await _user_from_token(token, db)
    enforce_csrf(request)
    return user


async def get_dashboard_access(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> AccessContext:
    # The app-wide require_api_key dependency already resolved (and cached) the context,
    # so reuse it instead of decoding the token and re-querying the user a second time.
    ctx = getattr(request.state, 'access', None) or await require_auth(
        request, authorization, x_api_key, x_portal_account_id, db
    )
    if ctx is not None:
        return ctx
    if not AUTH_REQUIRE_LOGIN:
        return AccessContext.api_key()
    raise HTTPException(status_code=401, detail='Authentication required')


def require_roles(*roles: str):
    async def _dep(user: PortalUser = Depends(get_current_user)) -> PortalUser:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail='Insufficient permissions')
        return user

    return _dep


async def _caller_is_demo(request: Request, authorization: str | None, db: AsyncSession) -> bool:
    """Whether the request carries a valid demo JWT; any other caller is simply not demo.

    Soft by design: API-key, X-Sync-Token and unauthenticated callers carry no JWT and keep full
    behaviour, so token-based automation keeps working.
    """
    ctx = getattr(request.state, 'access', None)
    if ctx is not None and ctx.user_id is not None:
        return ctx.is_demo
    # `require_auth` did not run (an open path): resolve the token the slow way.
    token = _extract_access_token(request, authorization)
    if not token:
        return False
    try:
        user_id = int(decode_access_token(token).get('sub'))
    except (jwt.PyJWTError, TypeError, ValueError):
        return False
    return bool(await db.scalar(select(PortalUser.is_demo).where(PortalUser.id == user_id)))


async def is_demo_request(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> bool:
    """Whether the caller is the shared read-only demo account."""
    return await _caller_is_demo(request, authorization, db)


async def forbid_demo(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> None:
    """Reject mutating requests made by the shared read-only demo account.

    Attach per write route via ``dependencies=[Depends(forbid_demo)]`` — never at router level,
    since the routers also serve demo-allowed reads.
    """
    if await _caller_is_demo(request, authorization, db):
        raise HTTPException(status_code=403, detail='Demo account is read-only')
