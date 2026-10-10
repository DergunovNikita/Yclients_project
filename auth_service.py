"""Password hashing, JWT, email tokens, and outbound mail."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

import bcrypt
import jwt
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    APP_PUBLIC_URL,
    AUTH_CONSOLE_EMAIL,
    AUTH_EMAIL_RESEND_COOLDOWN_SECONDS,
    AUTH_JWT_EXPIRE_MINUTES,
    AUTH_JWT_SECRET,
    IS_PRODUCTION,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_SSL,
    SMTP_USE_TLS,
    SMTP_USER,
    smtp_is_configured,
)
from models import PortalBranch, PortalEmailToken, PortalUser, PortalUserBranch, Staff

TOKEN_PURPOSE_VERIFY = 'verify'
TOKEN_PURPOSE_RESET = 'reset'
TOKEN_TTL_HOURS = {'verify': 48, 'reset': 2}
PORTAL_PASSWORD_MIN_LENGTH = 8
# bcrypt 5 raises on longer input and older versions truncate silently; neither is acceptable.
PASSWORD_MAX_BYTES = 72


def check_password_bytes(password: str) -> str:
    """Pydantic validator: reject passwords bcrypt cannot hash in full (Cyrillic is 2 bytes a character)."""
    if len(password.encode('utf-8')) > PASSWORD_MAX_BYTES:
        raise ValueError(f'Password must not exceed {PASSWORD_MAX_BYTES} bytes in UTF-8 (about 36 Cyrillic characters)')
    return password


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


# Checked when the e-mail is unknown so a miss costs the same bcrypt round as a hit.
DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(16))


def generate_bootstrap_password(length: int = 24) -> str:
    """Generate a random password used only to seed a password hash."""
    alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def verify_password(password: str, password_hash: str) -> bool:
    raw = password.encode('utf-8')
    if len(raw) > PASSWORD_MAX_BYTES:
        return False
    try:
        return bcrypt.checkpw(raw, password_hash.encode('utf-8'))
    except ValueError:  # malformed stored hash
        return False


def create_access_token(
    user_id: int,
    role: str,
    token_version: int,
    session_id: int,
) -> str:
    payload = {
        'sub': str(user_id),
        'role': role,
        'tv': int(token_version or 0),
        'exp': datetime.utcnow() + timedelta(minutes=AUTH_JWT_EXPIRE_MINUTES),
        'iat': datetime.utcnow(),
        'sid': int(session_id),
    }
    return jwt.encode(payload, AUTH_JWT_SECRET, algorithm='HS256')


def email_cooldown_active(sent_at: datetime | None) -> bool:
    if sent_at is None or AUTH_EMAIL_RESEND_COOLDOWN_SECONDS <= 0:
        return False
    return datetime.utcnow() - sent_at < timedelta(seconds=AUTH_EMAIL_RESEND_COOLDOWN_SECONDS)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, AUTH_JWT_SECRET, algorithms=['HS256'])


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


async def invalidate_email_tokens(db: AsyncSession, user_id: int, purpose: str, *, now: datetime | None = None) -> None:
    """Mark every unused token of one purpose as used, so only the newest mail link stays valid."""
    await db.execute(
        update(PortalEmailToken)
        .where(
            PortalEmailToken.user_id == user_id,
            PortalEmailToken.purpose == purpose,
            PortalEmailToken.used_at.is_(None),
        )
        .values(used_at=now or datetime.utcnow())
        .execution_options(synchronize_session=False)
    )


async def create_email_token(db: AsyncSession, user_id: int, purpose: str, *, commit: bool = True) -> str:
    raw = secrets.token_urlsafe(32)
    ttl = TOKEN_TTL_HOURS[purpose]
    now = datetime.utcnow()
    await invalidate_email_tokens(db, user_id, purpose, now=now)
    db.add(
        PortalEmailToken(
            user_id=user_id,
            token_hash=_token_hash(raw),
            purpose=purpose,
            expires_at=now + timedelta(hours=ttl),
            created_at=now,
        )
    )
    if commit:
        await db.commit()
    return raw


async def consume_email_token(db: AsyncSession, raw_token: str, purpose: str) -> PortalUser | None:
    """Spend a token exactly once; the conditional UPDATE is what makes concurrent replays lose."""
    now = datetime.utcnow()
    user_id = (
        await db.execute(
            update(PortalEmailToken)
            .where(
                PortalEmailToken.token_hash == _token_hash(raw_token),
                PortalEmailToken.purpose == purpose,
                PortalEmailToken.used_at.is_(None),
                PortalEmailToken.expires_at > now,
                PortalEmailToken.user_id.in_(select(PortalUser.id).where(PortalUser.is_active.is_(True))),
            )
            .values(used_at=now)
            .returning(PortalEmailToken.user_id)
            .execution_options(synchronize_session=False)
        )
    ).scalar_one_or_none()
    if user_id is None:
        return None

    user = (await db.execute(select(PortalUser).where(PortalUser.id == user_id))).scalar_one()
    await db.commit()
    return user


async def load_user_branch_ids(db: AsyncSession, user_id: int) -> list[int]:
    rows = await db.execute(
        select(PortalUserBranch.company_id)
        .where(PortalUserBranch.user_id == user_id)
        .order_by(PortalUserBranch.company_id.asc())
    )
    return [row[0] for row in rows.all()]


async def load_portal_account_branch_ids(db: AsyncSession, portal_account_id: int | None) -> list[int]:
    if portal_account_id is None:
        return []
    rows = await db.execute(
        select(PortalBranch.company_id)
        .where(PortalBranch.portal_account_id == portal_account_id)
        .order_by(PortalBranch.company_id.asc())
    )
    return [row[0] for row in rows.all()]


async def load_user_access_branch_ids(db: AsyncSession, user: PortalUser) -> list[int]:
    if user.role == 'platform_admin':
        return []
    if user.role == 'owner':
        return await load_portal_account_branch_ids(db, user.portal_account_id)
    return await load_user_branch_ids(db, user.id)


async def load_portal_user_staff_rows(db: AsyncSession, user_id: int) -> list[tuple[int | None, int]]:
    """(company_id, staff_id) rows a portal user owns — one per branch they work in.

    Rows without a branch are kept: they cannot belong to a branch-scoped pair, but they
    still count as "this user is a staff member" for the self-scope clamp.
    """
    rows = (
        await db.execute(
            select(Staff.company_id, Staff.id)
            .where(Staff.portal_user_id == user_id, Staff.fired == 0)
            .order_by(Staff.id.asc())
        )
    ).all()
    return [
        (int(row.company_id) if row.company_id is not None else None, int(row.id))
        for row in rows
    ]


async def set_user_branches(db: AsyncSession, user_id: int, company_ids: list[int]) -> None:
    await db.execute(delete(PortalUserBranch).where(PortalUserBranch.user_id == user_id))
    for company_id in sorted(set(company_ids)):
        db.add(PortalUserBranch(user_id=user_id, company_id=company_id))


def user_can_login(user: PortalUser) -> bool:
    if not user.is_active:
        return False
    return True


def _email_link(path: str, token: str) -> str:
    base = APP_PUBLIC_URL.rstrip('/')
    return f'{base}{path}?token={token}'


def _email_delivery_mode() -> str:
    if AUTH_CONSOLE_EMAIL or not smtp_is_configured():
        return 'console'
    return 'smtp'


def send_auth_email(to_email: str, subject: str, body: str) -> None:
    if _email_delivery_mode() == 'console':
        # The body carries a bearer token (reset/invite/verify link); anyone who reads the container
        # logs could take the account over, so only local development gets to see it.
        if IS_PRODUCTION:
            print(f'[auth-email] To: {to_email}\nSubject: {subject}\n(body withheld in production)\n')
        else:
            print(f'[auth-email] To: {to_email}\nSubject: {subject}\n{body}\n')
        return

    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = SMTP_FROM or SMTP_USER
    message['To'] = to_email
    message.set_content(body, charset='utf-8')

    if SMTP_USE_SSL or SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
            smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(message)
        return

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        if SMTP_USE_TLS:
            smtp.starttls()
        smtp.login(SMTP_USER, SMTP_PASSWORD)
        smtp.send_message(message)


async def send_verification_email(db: AsyncSession, user: PortalUser) -> None:
    token = await create_email_token(db, user.id, TOKEN_PURPOSE_VERIFY, commit=False)
    user.email_verification_sent_at = datetime.utcnow()
    link = _email_link('/verify-email.html', token)
    name = (user.full_name or user.email).strip()
    try:
        await asyncio.to_thread(
            send_auth_email,
            user.email,
            'Подтверждение регистрации — YClients Portal',
            (
                f'Здравствуйте, {name}!\n\n'
                'Для завершения регистрации перейдите по ссылке (действует 48 часов):\n'
                f'{link}\n\n'
                'Если вы не регистрировались на портале, просто проигнорируйте это письмо.\n'
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def send_password_reset_email(db: AsyncSession, user: PortalUser) -> None:
    token = await create_email_token(db, user.id, TOKEN_PURPOSE_RESET, commit=False)
    user.password_reset_sent_at = datetime.utcnow()
    link = _email_link('/reset-password.html', token)
    name = (user.full_name or user.email).strip()
    try:
        await asyncio.to_thread(
            send_auth_email,
            user.email,
            'Сброс пароля — YClients Portal',
            (
                f'Здравствуйте, {name}!\n\n'
                'Чтобы задать новый пароль, перейдите по ссылке (действует 2 часа):\n'
                f'{link}\n\n'
                'Если вы не запрашивали сброс, проигнорируйте это письмо.\n'
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


async def send_account_invite_email(db: AsyncSession, user: PortalUser) -> None:
    token = await create_email_token(db, user.id, TOKEN_PURPOSE_RESET, commit=False)
    user.password_reset_sent_at = datetime.utcnow()
    link = _email_link('/reset-password.html', token)
    name = (user.full_name or user.email).strip()
    try:
        await asyncio.to_thread(
            send_auth_email,
            user.email,
            'Доступ к YClients Portal',
            (
                f'Здравствуйте, {name}!\n\n'
                'Для вас создан аккаунт на портале.\n\n'
                f'Логин: {user.email}\n'
                'Чтобы задать пароль, перейдите по ссылке (действует 2 часа):\n'
                f'{link}\n\n'
                'Если вы не ожидали это письмо, просто проигнорируйте его.\n'
            ),
        )
        await db.commit()
    except Exception:
        await db.rollback()
        raise


def is_deliverable_portal_email(email: str) -> bool:
    """Synthetic staff logins are not real inboxes."""
    local, _, domain = email.partition('@')
    if not local or not domain:
        return False
    return domain.casefold() != 'portal.local'
