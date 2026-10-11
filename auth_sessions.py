"""Refresh tokens, session metadata, cookie auth, and CSRF for the portal."""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from clock import utcnow
from config import (
    AUTH_COOKIE_DOMAIN,
    AUTH_COOKIE_SAMESITE,
    AUTH_COOKIE_SECURE,
    AUTH_CSRF_COOKIE_NAME,
    AUTH_CSRF_HEADER_NAME,
    AUTH_JWT_EXPIRE_MINUTES,
    AUTH_JWT_SECRET,
    AUTH_MAX_ACTIVE_SESSIONS,
    AUTH_REFRESH_TOKEN_EXPIRE_DAYS,
    AUTH_SESSION_MAX_AGE_DAYS,
    PROXY_SHARED_SECRET,
)
from models import PortalRefreshToken, PortalUser
from sync_security import secure_equals

ACCESS_COOKIE_NAME = 'portal_access'
REFRESH_COOKIE_NAME = 'portal_refresh'
REFRESH_COOKIE_PATH = '/auth'
ACCESS_COOKIE_PATH = '/'
SAFE_METHODS = {'GET', 'HEAD', 'OPTIONS'}
PROXY_SECRET_HEADER = 'x-proxy-secret'
# The previous refresh token still rotates for this long: a client whose response was lost
# retries with it. Presented later it can only be a replay, so the session is revoked.
REFRESH_ROTATION_GRACE = timedelta(seconds=30)
# The shared demo account is exempt from AUTH_MAX_ACTIVE_SESSIONS (many visitors at once) but
# not unbounded: every anonymous demo login adds a row.
DEMO_MAX_ACTIVE_SESSIONS = 200

logger = logging.getLogger('yclients.auth')


@dataclass(frozen=True)
class IssuedSession:
    access_token: str
    refresh_token: str
    csrf_token: str


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


def client_ip(request: Request) -> str | None:
    """The visitor's address: the socket peer, or the first X-Forwarded-For hop of the trusted proxy.

    The header is believed only when the request proves it came through our own proxy by
    carrying PROXY_SHARED_SECRET; anyone else can write any X-Forwarded-For they like.
    """
    if PROXY_SHARED_SECRET and secure_equals(request.headers.get(PROXY_SECRET_HEADER), PROXY_SHARED_SECRET):
        forwarded = request.headers.get('x-forwarded-for', '').split(',', 1)[0].strip()
        if forwarded:
            return forwarded
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
    now = utcnow()

    user_agent = request.headers.get('user-agent', '')
    refresh = PortalRefreshToken(
        user_id=user.id,
        token_hash=_hash_token(raw_refresh),
        expires_at=_session_expiry(now, now),
        last_used_at=now,
        user_agent=user_agent[:500] if user_agent else None,
        device_label=parse_device_label(user_agent),
        ip_hash=_hash_ip(client_ip(request)),
        created_at=now,
    )
    db.add(refresh)
    await db.flush()
    await _maintain_user_sessions(db, user.id, now, is_demo=user.is_demo)

    access_token = access_token_fn(user, refresh.id)
    return IssuedSession(
        access_token=access_token,
        refresh_token=raw_refresh,
        csrf_token=csrf_token,
    )


async def rotate_session(
    db: AsyncSession,
    raw_refresh: str,
    request: Request,
    *,
    access_token_fn,
) -> tuple[PortalUser, IssuedSession]:
    """Validate and rotate a refresh token without creating another DB row.

    The token this session held before its last rotation is accepted once more within
    REFRESH_ROTATION_GRACE (the response carrying the new one may have been lost); later it
    revokes the session as a replay.
    """
    presented_hash = _hash_token(raw_refresh)
    now = utcnow()
    refresh, within_grace = await _load_rotatable_refresh(db, presented_hash, now)
    user = (
        await db.execute(select(PortalUser).where(PortalUser.id == refresh.user_id))
    ).scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=401, detail='User inactive')

    raw_rotated_refresh = secrets.token_urlsafe(48)
    csrf_token = secrets.token_urlsafe(32)
    user_agent = request.headers.get('user-agent', '')

    if within_grace:
        # The client still holds the old token, so it stays the "previous" one: a second lost
        # response is retried the same way.
        matches = PortalRefreshToken.previous_token_hash == presented_hash
        rotation = {}
    else:
        matches = PortalRefreshToken.token_hash == presented_hash
        rotation = {'previous_token_hash': presented_hash}
    rotate_result = await db.execute(
        update(PortalRefreshToken)
        .where(
            PortalRefreshToken.id == refresh.id,
            matches,
            PortalRefreshToken.expires_at > now,
        )
        .values(
            token_hash=_hash_token(raw_rotated_refresh),
            rotated_at=now,
            expires_at=_session_expiry(refresh.created_at, now),
            last_used_at=now,
            user_agent=user_agent[:500] if user_agent else None,
            device_label=parse_device_label(user_agent),
            ip_hash=_hash_ip(client_ip(request)),
            **rotation,
        )
        .execution_options(synchronize_session=False)
    )
    if rotate_result.rowcount != 1:
        raise HTTPException(status_code=401, detail='Invalid refresh token')
    await db.refresh(refresh)
    await _maintain_user_sessions(db, user.id, now, is_demo=user.is_demo)

    issued = IssuedSession(
        access_token=access_token_fn(user, refresh.id),
        refresh_token=raw_rotated_refresh,
        csrf_token=csrf_token,
    )
    return user, issued


async def revoke_refresh(db: AsyncSession, raw_refresh: str) -> None:
    """Delete a single refresh token. Silently no-op if unknown."""
    token_hash = _hash_token(raw_refresh)
    await db.execute(
        delete(PortalRefreshToken).where(PortalRefreshToken.token_hash == token_hash)
    )


async def revoke_user_sessions(
    db: AsyncSession,
    user_id: int,
    *,
    except_refresh: str | None = None,
) -> None:
    """Delete all refresh tokens for a user. Used by logout-all and password change."""
    stmt = delete(PortalRefreshToken).where(PortalRefreshToken.user_id == user_id)
    if except_refresh is not None:
        stmt = stmt.where(PortalRefreshToken.token_hash != _hash_token(except_refresh))
    await db.execute(stmt)


async def list_user_sessions(
    db: AsyncSession,
    user_id: int,
    *,
    is_demo: bool = False,
) -> list[PortalRefreshToken]:
    now = utcnow()
    await _maintain_user_sessions(db, user_id, now, is_demo=is_demo)
    rows = await db.execute(
        select(PortalRefreshToken)
        .where(
            PortalRefreshToken.user_id == user_id,
            PortalRefreshToken.expires_at > now,
        )
        .order_by(PortalRefreshToken.last_used_at.desc().nullslast(), PortalRefreshToken.id.desc())
    )
    return list(rows.scalars().all())


async def revoke_session_by_id(db: AsyncSession, user_id: int, session_id: int) -> bool:
    result = await db.execute(
        delete(PortalRefreshToken)
        .where(
            PortalRefreshToken.id == session_id,
            PortalRefreshToken.user_id == user_id,
        )
    )
    return bool(result.rowcount)


async def _maintain_user_sessions(
    db: AsyncSession,
    user_id: int,
    now: datetime,
    *,
    is_demo: bool,
) -> None:
    """Remove inactive records and retire least-recently-used excess sessions."""
    await db.execute(
        delete(PortalRefreshToken).where(
            PortalRefreshToken.user_id == user_id,
            PortalRefreshToken.expires_at <= now,
        )
    )
    max_active = DEMO_MAX_ACTIVE_SESSIONS if is_demo else max(1, int(AUTH_MAX_ACTIVE_SESSIONS))
    excess_ids = list(
        (
            await db.scalars(
                select(PortalRefreshToken.id)
                .where(
                    PortalRefreshToken.user_id == user_id,
                            PortalRefreshToken.expires_at > now,
                )
                .order_by(
                    PortalRefreshToken.last_used_at.desc().nullslast(),
                    PortalRefreshToken.id.desc(),
                )
                .offset(max_active)
            )
        ).all()
    )
    if excess_ids:
        await db.execute(delete(PortalRefreshToken).where(PortalRefreshToken.id.in_(excess_ids)))


def _session_expiry(created_at: datetime, now: datetime) -> datetime:
    """Sliding refresh expiry, never past the absolute session age cap."""
    sliding = now + timedelta(days=AUTH_REFRESH_TOKEN_EXPIRE_DAYS)
    return min(sliding, created_at + timedelta(days=max(1, AUTH_SESSION_MAX_AGE_DAYS)))


async def _end_session(db: AsyncSession, refresh: PortalRefreshToken) -> None:
    """Delete the session and commit now: the 401 that follows would roll the deletion back."""
    await db.execute(delete(PortalRefreshToken).where(PortalRefreshToken.id == refresh.id))
    await db.commit()


async def _load_rotatable_refresh(
    db: AsyncSession,
    token_hash: str,
    now: datetime,
) -> tuple[PortalRefreshToken, bool]:
    """Find the session a refresh token belongs to; the flag says it matched only as the previous token."""
    refresh = (
        await db.execute(select(PortalRefreshToken).where(PortalRefreshToken.token_hash == token_hash))
    ).scalar_one_or_none()
    within_grace = False
    if refresh is None:
        refresh = (
            await db.execute(select(PortalRefreshToken).where(PortalRefreshToken.previous_token_hash == token_hash))
        ).scalar_one_or_none()
        if refresh is None:
            raise HTTPException(status_code=401, detail='Invalid refresh token')
        within_grace = refresh.rotated_at is not None and now - refresh.rotated_at <= REFRESH_ROTATION_GRACE
        if not within_grace:
            logger.warning(
                'refresh token replayed after rotation user_id=%s session_id=%s', refresh.user_id, refresh.id
            )
            await _end_session(db, refresh)
            raise HTTPException(status_code=401, detail='Invalid refresh token')
    if refresh.expires_at < now:
        raise HTTPException(status_code=401, detail='Refresh token expired or revoked')
    if refresh.created_at + timedelta(days=max(1, AUTH_SESSION_MAX_AGE_DAYS)) <= now:
        await _end_session(db, refresh)
        raise HTTPException(status_code=401, detail='Refresh token expired or revoked')
    return refresh, within_grace


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
    if not cookie_token or not header_token or not secure_equals(cookie_token, header_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail='CSRF token missing or invalid',
        )
