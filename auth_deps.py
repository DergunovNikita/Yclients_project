"""FastAPI dependencies for portal authentication and authorization."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jwt
from auth_scope import AccessContext
from auth_service import decode_access_token, load_portal_account_branch_ids, load_user_access_branch_ids
from auth_sessions import ACCESS_COOKIE_NAME, enforce_csrf
from config import API_KEY, AUTH_REQUIRE_LOGIN
from database import get_async_db
from models import PortalUser

OPEN_PATH_PREFIXES = (
    '/health',
    '/openapi.json',
    '/docs',
    '/redoc',
    '/auth/register',
    '/auth/login',
    '/auth/refresh',
    '/auth/logout',
    '/auth/verify-email',
    '/auth/forgot-password',
    '/auth/reset-password',
    '/auth/resend-verification',
    '/dashboard/auth/register',
    '/dashboard/auth/login',
    '/dashboard/auth/refresh',
    '/dashboard/auth/logout',
    '/dashboard/auth/verify-email',
    '/dashboard/auth/forgot-password',
    '/dashboard/auth/reset-password',
    '/dashboard/auth/resend-verification',
)


def _is_open_path(path: str) -> bool:
    if path in {'/health', '/openapi.json', '/docs', '/redoc'}:
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
    return AccessContext.from_user(user.id, user.role, user.portal_account_id, branch_ids)


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
