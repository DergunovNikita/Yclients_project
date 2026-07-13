"""Refresh tokens, session metadata, cookie auth, and CSRF for the portal."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    AUTH_COOKIE_DOMAIN,
    AUTH_COOKIE_SAMESITE,
    AUTH_COOKIE_SECURE,
    AUTH_CSRF_COOKIE_NAME,
    AUTH_CSRF_HEADER_NAME,
    AUTH_JWT_EXPIRE_MINUTES,
    AUTH_JWT_SECRET,
    AUTH_REFRESH_TOKEN_EXPIRE_DAYS,
)
from models import PortalRefreshToken, PortalUser

ACCESS_COOKIE_NAME = 'portal_access'
REFRESH_COOKIE_NAME = 'portal_refresh'
REFRESH_COOKIE_PATH = '/auth'
ACCESS_COOKIE_PATH = '/'
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}


@dataclass(frozen=True)
class IssuedSession:
    access_token: str
    refresh_token: str
    csrf_token: str
    refresh_record: PortalRefreshToken


# ---------- token hashing helpers ----------

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


def _hash_ip(ip: str | None) -> str | None:
    if not ip:
        return None
    pepper = AUTH_JWT_SECRET.encode('utf-8')
    return hashlib.sha256(pepper + ip.encode('utf-8')).hexdigest()


# ---------- device fingerprinting ----------

_BROWSER_KEYWORDS = (
    ('Edg/', 'Edge'),
    ('OPR/', 'Opera'),
    ('Firefox/', 'Firefox'),
    ('Chrome/', 'Chrome'),
    ('Safari/', 'Safari'),
)

_OS_KEYWORDS = (
    ('iPhone', 'iOS'),
    ('iPad', 'iPadOS'),
    ('Android', 'Android'),
    ('Windows NT', 'Windows'),
    ('Mac OS X', 'macOS'),
    ('Macintosh', 'macOS'),
    ('Linux', 'Linux'),
)


def parse_device_label(user_agent: str | None) -> str:
    if not user_agent:
        return 'Unknown device'
    browser = next((label for token, label in _BROWSER_KEYWORDS if token in user_agent), None)
    os_name = next((label for token, label in _OS_KEYWORDS if token in user_agent), None)
    if browser and os_name:
        return f'{browser} · {os_name}'
    return browser or os_name or 'Unknown device'


def extract_client_ip(request: Request) -> str | None:
    # Public browser traffic reaches the VM through the frontend proxy, which strips
    # client-supplied forwarding headers and rewrites x-forwarded-for to its socket peer.
    forwarded = request.headers.get('x-forwarded-for')
    if forwarded:
        return forwarded.split(',', 1)[0].strip() or None
    return request.client.host if request.client else None


# ---------- refresh token lifecycle ----------

async def issue_session(
    db: AsyncSession,
    user: PortalUser,
    request: Request,
    *,
    access_token_fn,
) -> IssuedSession:
    """Create a fresh refresh record and bundle access/refresh/csrf tokens."""
    raw_refresh = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    now = datetime.utcnow()

    user_agent = request.headers.get('user-agent', '')
    refresh = PortalRefreshToken(
        user_id=user.id,
        token_hash=_hash_token(raw_refresh),
        expires_at=now + timedelta(days=AUTH_REFRESH_TOKEN_EXPIRE_DAYS),
        last_used_at=now,
        user_agent=user_agent[:500] if user_agent else None,
        device_label=parse_device_label(user_agent),
        ip_hash=_hash_ip(extract_client_ip(request)),
        created_at=now,
    )
    db.add(refresh)
    await db.flush()

    access_token = access_token_fn(user)
    return IssuedSession(
        access_token=access_token,
        refresh_token=raw_refresh,
        csrf_token=csrf_token,
        refresh_record=refresh,
    )


async def rotate_session(
    db: AsyncSession,
    raw_refresh: str,
    request: Request,
    *,
    access_token_fn,
) -> tuple[PortalUser, IssuedSession]:
    """Validate the refresh token, revoke it, and issue a new pair (rotation)."""
    refresh = await _load_active_refresh(db, raw_refresh)
    user = (
        await db.execute(select(PortalUser).where(PortalUser.id == refresh.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail='User inactive')

    refresh.revoked_at = datetime.utcnow()
    issued = await issue_session(db, user, request, access_token_fn=access_token_fn)
    return user, issued


async def revoke_refresh(db: AsyncSession, raw_refresh: str) -> None:
    """Mark a single refresh token as revoked. Silently no-op if unknown."""
    token_hash = _hash_token(raw_refresh)
    await db.execute(
        update(PortalRefreshToken)
        .where(PortalRefreshToken.token_hash == token_hash, PortalRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.utcnow())
    )


async def revoke_user_sessions(
    db: AsyncSession,
    user_id: int,
    *,
    except_refresh: str | None = None,
) -> None:
    """Revoke all refresh tokens for a user. Used by logout-all and password change."""
    stmt = (
        update(PortalRefreshToken)
        .where(PortalRefreshToken.user_id == user_id, PortalRefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.utcnow())
    )
    if except_refresh is not None:
        stmt = stmt.where(PortalRefreshToken.token_hash != _hash_token(except_refresh))
    await db.execute(stmt)


async def list_user_sessions(db: AsyncSession, user_id: int) -> list[PortalRefreshToken]:
    rows = await db.execute(
        select(PortalRefreshToken)
        .where(
            PortalRefreshToken.user_id == user_id,
            PortalRefreshToken.revoked_at.is_(None),
            PortalRefreshToken.expires_at > datetime.utcnow(),
        )
        .order_by(PortalRefreshToken.last_used_at.desc().nullslast(), PortalRefreshToken.id.desc())
    )
    return list(rows.scalars().all())


async def revoke_session_by_id(db: AsyncSession, user_id: int, session_id: int) -> bool:
    result = await db.execute(
        update(PortalRefreshToken)
        .where(
            PortalRefreshToken.id == session_id,
            PortalRefreshToken.user_id == user_id,
            PortalRefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=datetime.utcnow())
    )
    return bool(result.rowcount)


async def _load_active_refresh(db: AsyncSession, raw_refresh: str) -> PortalRefreshToken:
    token_hash = _hash_token(raw_refresh)
    refresh = (
        await db.execute(select(PortalRefreshToken).where(PortalRefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()
    if refresh is None:
        raise HTTPException(status_code=401, detail='Invalid refresh token')
    if refresh.revoked_at is not None or refresh.expires_at < datetime.utcnow():
        raise HTTPException(status_code=401, detail='Refresh token expired or revoked')
    return refresh


def bump_user_token_version(user: PortalUser) -> None:
    """Invalidate every live access token for the user without touching DB rows."""
    user.token_version = (user.token_version or 0) + 1


# ---------- cookie helpers ----------

def _cookie_kwargs(*, max_age: int, path: str, http_only: bool) -> dict:
    kwargs: dict = {
        'max_age': max_age,
        'path': path,
        'secure': AUTH_COOKIE_SECURE,
        'samesite': AUTH_COOKIE_SAMESITE,
        'httponly': http_only,
    }
    if AUTH_COOKIE_DOMAIN:
        kwargs['domain'] = AUTH_COOKIE_DOMAIN
    return kwargs


def set_auth_cookies(response: Response, session: IssuedSession) -> None:
    access_max_age = AUTH_JWT_EXPIRE_MINUTES * 60
    refresh_max_age = AUTH_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60

    response.set_cookie(
        ACCESS_COOKIE_NAME,
        session.access_token,
        **_cookie_kwargs(max_age=access_max_age, path=ACCESS_COOKIE_PATH, http_only=True),
    )
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        session.refresh_token,
        **_cookie_kwargs(max_age=refresh_max_age, path=REFRESH_COOKIE_PATH, http_only=True),
    )
    # CSRF cookie must be readable by JS so the SPA can echo it in a header.
    response.set_cookie(
        AUTH_CSRF_COOKIE_NAME,
        session.csrf_token,
        **_cookie_kwargs(max_age=refresh_max_age, path=ACCESS_COOKIE_PATH, http_only=False),
    )


def clear_auth_cookies(response: Response) -> None:
    domain = AUTH_COOKIE_DOMAIN or None
    response.delete_cookie(ACCESS_COOKIE_NAME, path=ACCESS_COOKIE_PATH, domain=domain)
    response.delete_cookie(REFRESH_COOKIE_NAME, path=REFRESH_COOKIE_PATH, domain=domain)
    response.delete_cookie(AUTH_CSRF_COOKIE_NAME, path=ACCESS_COOKIE_PATH, domain=domain)


# ---------- CSRF ----------

def enforce_csrf(request: Request, *, allow_bearer_skip: bool = True) -> None:
    """Double-submit-cookie CSRF for mutating cookie-authenticated requests.

    Skipped for safe methods, Bearer auth (no cookie ambient credential), and
    requests without any session cookie.
    """
    if request.method in SAFE_METHODS:
        return
    has_session_cookie = bool(request.cookies.get(ACCESS_COOKIE_NAME) or request.cookies.get(REFRESH_COOKIE_NAME))
    auth_header = request.headers.get('authorization', '')
    if allow_bearer_skip and not has_session_cookie and auth_header.lower().startswith('bearer '):
        return
    if not has_session_cookie:
        return

    cookie_token = request.cookies.get(AUTH_CSRF_COOKIE_NAME)
    header_token = request.headers.get(AUTH_CSRF_HEADER_NAME)
    if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='CSRF token missing or invalid',
        )
