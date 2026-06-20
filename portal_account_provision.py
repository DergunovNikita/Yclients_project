"""Create portal accounts for staff members without login credentials."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service import generate_initial_password, hash_password, normalize_email, set_user_branches
from models import PortalUser, Staff

_CYRILLIC_TO_LATIN = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh', 'з': 'z',
    'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r',
    'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch',
    'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
}


def _transliterate(value: str) -> str:
    chars = []
    for char in value.lower():
        if char in _CYRILLIC_TO_LATIN:
            chars.append(_CYRILLIC_TO_LATIN[char])
            continue
        normalized = unicodedata.normalize('NFKD', char)
        ascii_char = normalized.encode('ascii', 'ignore').decode('ascii')
        chars.append(ascii_char.lower())
    return ''.join(chars)


def staff_login_email(staff: Staff) -> str:
    """Build a unique login email for a staff member without a portal account."""
    slug = _transliterate(staff.name or '')
    slug = re.sub(r'[^a-z0-9]+', '.', slug).strip('.')
    if not slug:
        slug = 'worker'
    return f'{slug}.{staff.id}@portal.local'


@dataclass
class ProvisionedAccount:
    staff_id: int
    user_id: int
    email: str
    full_name: str
    initial_password: str
    company_id: int
    role: str


async def _email_is_taken(db: AsyncSession, email: str) -> bool:
    existing = (await db.execute(select(PortalUser.id).where(PortalUser.email == email))).scalar_one_or_none()
    return existing is not None


async def _unique_staff_email(db: AsyncSession, staff: Staff, preferred: str | None = None) -> str:
    if preferred:
        email = normalize_email(preferred)
        if not await _email_is_taken(db, email):
            return email
        raise ValueError(f'Email already registered: {email}')

    base = staff_login_email(staff)
    if not await _email_is_taken(db, base):
        return base

    local, domain = base.split('@', 1)
    suffix = 2
    while True:
        candidate = normalize_email(f'{local}.{suffix}@{domain}')
        if not await _email_is_taken(db, candidate):
            return candidate
        suffix += 1


async def _ensure_portal_user_id_sequence(db: AsyncSession) -> None:
    bind = db.get_bind()
    if bind.dialect.name != 'postgresql':
        return
    await db.execute(
        text(
            "SELECT setval("
            "pg_get_serial_sequence('system.portal_users', 'id'), "
            "COALESCE((SELECT MAX(id) FROM system.portal_users), 1), "
            "true)"
        )
    )


async def _portal_user_id_is_available(db: AsyncSession, user_id: int) -> bool:
    existing = (await db.execute(select(PortalUser.id).where(PortalUser.id == user_id))).scalar_one_or_none()
    return existing is None


async def provision_staff_account(
    db: AsyncSession,
    staff: Staff,
    *,
    email: str | None = None,
    role: str = 'viewer',
    password: str | None = None,
) -> ProvisionedAccount:
    if staff.portal_user_id is not None:
        raise ValueError(f'Staff {staff.id} already has a portal account')
    if staff.fired:
        raise ValueError(f'Staff {staff.id} is inactive')
    if not await _portal_user_id_is_available(db, staff.id):
        raise ValueError(f'Portal user id {staff.id} is already taken')

    login_email = await _unique_staff_email(db, staff, email)
    initial_password = password or generate_initial_password()
    now = datetime.utcnow()

    user = PortalUser(
        id=staff.id,
        email=login_email,
        password_hash=hash_password(initial_password),
        full_name=staff.name,
        role=role,
        is_active=True,
        email_verified_at=now,
        initial_password=initial_password,
        created_at=now,
    )
    db.add(user)
    await db.flush()
    if user.id != staff.id:
        raise RuntimeError(f'Portal user id mismatch: expected {staff.id}, got {user.id}')

    await set_user_branches(db, user.id, [staff.company_id])
    staff.portal_user_id = user.id
    if staff.fired:
        staff.fired = 0
    await _ensure_portal_user_id_sequence(db)

    return ProvisionedAccount(
        staff_id=staff.id,
        user_id=user.id,
        email=login_email,
        full_name=staff.name,
        initial_password=initial_password,
        company_id=staff.company_id,
        role=role,
    )


async def list_unlinked_staff_for_provision(
    db: AsyncSession,
    allowed_company_ids: list[int] | None,
) -> list[Staff]:
    stmt = (
        select(Staff)
        .where(
            Staff.fired == 0,
            Staff.portal_user_id.is_(None),
        )
        .order_by(Staff.company_id.asc(), Staff.name.asc(), Staff.id.asc())
    )
    if allowed_company_ids is not None:
        stmt = stmt.where(Staff.company_id.in_(allowed_company_ids))
    return (await db.execute(stmt)).scalars().all()


async def provision_all_unlinked_staff(
    db: AsyncSession,
    allowed_company_ids: list[int] | None,
) -> tuple[list[ProvisionedAccount], list[str]]:
    staff_rows = await list_unlinked_staff_for_provision(db, allowed_company_ids)
    created: list[ProvisionedAccount] = []
    errors: list[str] = []
    for staff in staff_rows:
        try:
            created.append(await provision_staff_account(db, staff))
        except ValueError as exc:
            errors.append(str(exc))
    return created, errors
