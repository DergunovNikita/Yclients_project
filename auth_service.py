"""Password hashing, JWT, email tokens, and outbound mail."""

from __future__ import annotations

import hashlib
import secrets
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage

import bcrypt
import jwt
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    APP_PUBLIC_URL,
    AUTH_CONSOLE_EMAIL,
    AUTH_EMAIL_VERIFY_REQUIRED,
    AUTH_JWT_EXPIRE_MINUTES,
    AUTH_JWT_SECRET,
    SMTP_FROM,
    SMTP_HOST,
    SMTP_PASSWORD,
    SMTP_PORT,
    SMTP_USE_SSL,
    SMTP_USE_TLS,
    SMTP_USER,
    smtp_is_configured,
)
from models import PortalEmailToken, PortalUser, PortalUserBranch

TOKEN_PURPOSE_VERIFY = 'verify'
TOKEN_PURPOSE_RESET = 'reset'
TOKEN_TTL_HOURS = {'verify': 48, 'reset': 2}


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')


def generate_initial_password(length: int = 12) -> str:
    """Generate a readable initial password (letters + digits, no ambiguous chars)."""
    alphabet = 'abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789'
    return ''.join(secrets.choice(alphabet) for _ in range(length))


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), password_hash.encode('utf-8'))


def create_access_token(user_id: int, role: str) -> str:
    payload = {
        'sub': str(user_id),
        'role': role,
        'exp': datetime.utcnow() + timedelta(minutes=AUTH_JWT_EXPIRE_MINUTES),
        'iat': datetime.utcnow(),
    }
    return jwt.encode(payload, AUTH_JWT_SECRET, algorithm='HS256')


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, AUTH_JWT_SECRET, algorithms=['HS256'])


def normalize_email(email: str) -> str:
    return email.strip().casefold()


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode('utf-8')).hexdigest()


async def create_email_token(db: AsyncSession, user_id: int, purpose: str) -> str:
    raw = secrets.token_urlsafe(32)
    ttl = TOKEN_TTL_HOURS[purpose]
    db.add(
        PortalEmailToken(
            user_id=user_id,
            token_hash=_token_hash(raw),
            purpose=purpose,
            expires_at=datetime.utcnow() + timedelta(hours=ttl),
            created_at=datetime.utcnow(),
        )
    )
    await db.commit()
    return raw


async def consume_email_token(db: AsyncSession, raw_token: str, purpose: str) -> PortalUser | None:
    token_hash = _token_hash(raw_token)
    row = (
        await db.execute(
            select(PortalEmailToken).where(
                PortalEmailToken.token_hash == token_hash,
                PortalEmailToken.purpose == purpose,
            )
        )
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at < datetime.utcnow():
        return None

    user = (await db.execute(select(PortalUser).where(PortalUser.id == row.user_id))).scalar_one_or_none()
    if user is None:
        return None

    row.used_at = datetime.utcnow()
    await db.commit()
    return user


async def load_user_branch_ids(db: AsyncSession, user_id: int) -> list[int]:
    rows = await db.execute(
        select(PortalUserBranch.company_id)
        .where(PortalUserBranch.user_id == user_id)
        .order_by(PortalUserBranch.company_id.asc())
    )
    return [row[0] for row in rows.all()]


async def set_user_branches(db: AsyncSession, user_id: int, company_ids: list[int]) -> None:
    await db.execute(delete(PortalUserBranch).where(PortalUserBranch.user_id == user_id))
    for company_id in sorted(set(company_ids)):
        db.add(PortalUserBranch(user_id=user_id, company_id=company_id))
    await db.commit()


def user_can_login(user: PortalUser) -> bool:
    if not user.is_active:
        return False
    if AUTH_EMAIL_VERIFY_REQUIRED and user.email_verified_at is None:
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
    token = await create_email_token(db, user.id, TOKEN_PURPOSE_VERIFY)
    link = _email_link('/verify-email.html', token)
    name = (user.full_name or user.email).strip()
    send_auth_email(
        user.email,
        'Подтверждение регистрации — YClients Portal',
        (
            f'Здравствуйте, {name}!\n\n'
            'Для завершения регистрации перейдите по ссылке (действует 48 часов):\n'
            f'{link}\n\n'
            'Если вы не регистрировались на портале, просто проигнорируйте это письмо.\n'
        ),
    )


async def send_password_reset_email(db: AsyncSession, user: PortalUser) -> None:
    token = await create_email_token(db, user.id, TOKEN_PURPOSE_RESET)
    link = _email_link('/reset-password.html', token)
    name = (user.full_name or user.email).strip()
    send_auth_email(
        user.email,
        'Сброс пароля — YClients Portal',
        (
            f'Здравствуйте, {name}!\n\n'
            'Чтобы задать новый пароль, перейдите по ссылке (действует 2 часа):\n'
            f'{link}\n\n'
            'Если вы не запрашивали сброс, проигнорируйте это письмо.\n'
        ),
    )


def send_account_credentials_email(user: PortalUser, password: str) -> None:
    login_url = APP_PUBLIC_URL.rstrip('/') + '/login.html'
    name = (user.full_name or user.email).strip()
    send_auth_email(
        user.email,
        'Доступ к YClients Portal',
        (
            f'Здравствуйте, {name}!\n\n'
            'Для вас создан аккаунт на портале.\n\n'
            f'Логин: {user.email}\n'
            f'Пароль: {password}\n\n'
            f'Войти: {login_url}\n\n'
            'Рекомендуем сменить пароль после первого входа в личном кабинете.\n'
        ),
    )


def is_deliverable_portal_email(email: str) -> bool:
    """Synthetic staff logins are not real inboxes."""
    local, _, domain = email.partition('@')
    if not local or not domain:
        return False
    return domain.casefold() != 'portal.local'
