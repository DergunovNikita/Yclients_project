"""FastAPI dependencies for portal authentication and authorization."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jwt
from auth_scope import AccessContext
from auth_service import decode_access_token, load_portal_account_branch_ids, load_user_access_branch_ids
from auth_sessions import ACCESS_COOKIE_NAME, enforce_csrf
from config import API_KEY, AUTH_REQUIRE_LOGIN, IS_PRODUCTION
from database import get_async_db
from models import PortalMetricVisibility, PortalUser, Staff
from plan_config import ALL_MONEY_CODES, CONFIGURABLE_MONEY_ROLES, default_money_codes_for_role

OPEN_PATH_PREFIXES = (
    '/health',
    '/auth/register',
    '/auth/login',
    '/auth/demo-login',
    '/auth/refresh',
    '/auth/logout',
    '/auth/verify-email',
    '/auth/forgot-password',
    '/auth/reset-password',
    '/auth/resend-verification',
    '/dashboard/auth/register',
    '/dashboard/auth/login',
    '/dashboard/auth/demo-login',
    '/dashboard/auth/refresh',
    '/dashboard/auth/logout',
    '/dashboard/auth/verify-email',
    '/dashboard/auth/forgot-password',
    '/dashboard/auth/reset-password',
    '/dashboard/auth/resend-verification',
)

DOCS_OPEN_PATHS = set() if IS_PRODUCTION else {'/openapi.json', '/docs', '/redoc'}


def _is_open_path(path: str) -> bool:
    if path == '/health' or path in DOCS_OPEN_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in OPEN_PATH_PREFIXES)


async def _user_from_token(
    token: str,
    db: AsyncSession,
    x_portal_account_id: int | None = None,
) -> AccessContext:
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail='Invalid or expired token') from exc

    user_id = int(payload['sub'])
    user = (await db.execute(select(PortalUser).where(PortalUser.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail='User not found or inactive')

    claim_tv = int(payload.get('tv', 0) or 0)
    if claim_tv != int(user.token_version or 0):
        raise HTTPException(status_code=401, detail='Session invalidated')

    if user.role == 'platform_admin':
        active_account_id = x_portal_account_id
        if active_account_id is not None:
            branch_ids = await load_portal_account_branch_ids(db, active_account_id)
            return AccessContext.from_user(user.id, user.role, active_account_id, branch_ids)
        return AccessContext.from_user(user.id, user.role, None, None)

    branch_ids = await load_user_access_branch_ids(db, user)
    staff_id = None
    if user.role == 'viewer':
        staff_id = await db.scalar(
            select(Staff.id)
            .where(Staff.portal_user_id == user.id, Staff.fired == 0)
            .order_by(Staff.id.asc())
            .limit(1)
        )
    money_metrics = await _resolve_money_metrics(db, user.portal_account_id, user.role)
    return AccessContext.from_user(
        user.id,
        user.role,
        user.portal_account_id,
        branch_ids,
        staff_id=staff_id,
        money_metrics=money_metrics,
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
    if _is_open_path(request.url.path):
        return None

    token = _extract_access_token(request, authorization)
    if token:
        ctx = await _user_from_token(token, db, x_portal_account_id)
        enforce_csrf(request)
        request.state.access = ctx
        return ctx

    if API_KEY:
        if x_api_key == API_KEY:
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
    token = _extract_access_token(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail='Authentication required')
    ctx = await _user_from_token(token, db)
    enforce_csrf(request)
    if ctx is None or ctx.user_id is None:
        raise HTTPException(status_code=401, detail='Authentication required')
    user = (await db.execute(select(PortalUser).where(PortalUser.id == ctx.user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail='User not found')
    return user


async def get_dashboard_access(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> AccessContext:
    ctx = await require_auth(request, authorization, x_api_key, x_portal_account_id, db)
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


async def forbid_demo(
    request: Request,
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> None:
    """Reject mutating requests made by the shared read-only demo account.

    Soft by design: only a valid demo JWT is blocked with 403. API-key,
    X-Sync-Token and unauthenticated callers carry no JWT and pass through
    unchanged, so token-based automation keeps working. Attach per write route
    via ``dependencies=[Depends(forbid_demo)]`` — never at router level, since
    the routers also serve demo-allowed reads.
    """
    token = _extract_access_token(request, authorization)
    if not token:
        return
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        return
    try:
        user_id = int(payload.get('sub'))
    except (TypeError, ValueError):
        return
    if await db.scalar(select(PortalUser.is_demo).where(PortalUser.id == user_id)):
        raise HTTPException(status_code=403, detail='Demo account is read-only')
