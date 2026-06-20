"""FastAPI dependencies for portal authentication and authorization."""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import jwt
from auth_scope import AccessContext
from auth_service import decode_access_token, load_user_branch_ids
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
    '/auth/verify-email',
    '/auth/forgot-password',
    '/auth/reset-password',
)


def _is_open_path(path: str) -> bool:
    if path in {'/health', '/openapi.json', '/docs', '/redoc'}:
        return True
    return any(path.startswith(prefix) for prefix in OPEN_PATH_PREFIXES)


async def _user_from_bearer(
    authorization: str | None,
    db: AsyncSession,
) -> AccessContext | None:
    if not authorization or not authorization.lower().startswith('bearer '):
        return None
    token = authorization.split(' ', 1)[1].strip()
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail='Invalid or expired token') from exc

    user_id = int(payload['sub'])
    user = (await db.execute(select(PortalUser).where(PortalUser.id == user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail='User not found or inactive')

    branch_ids = await load_user_branch_ids(db, user.id)
    return AccessContext.from_user(user.id, user.role, branch_ids)


async def require_auth(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> AccessContext | None:
    """Global auth: JWT user, API key (full access), or open paths."""
    if _is_open_path(request.url.path):
        return None

    if authorization and authorization.lower().startswith('bearer '):
        ctx = await _user_from_bearer(authorization, db)
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
    authorization: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
) -> PortalUser:
    ctx = await _user_from_bearer(authorization, db)
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
    db: AsyncSession = Depends(get_async_db),
) -> AccessContext:
    ctx = await require_auth(request, authorization, x_api_key, db)
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
