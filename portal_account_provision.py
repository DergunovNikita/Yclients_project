"""Create portal accounts for staff members without login credentials."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service import (
    generate_bootstrap_password,
    hash_password,
    is_deliverable_portal_email,
    normalize_email,
    set_user_branches,
)
from models import PortalBranch, PortalUser, Staff


@dataclass
class ProvisionedAccount:
    staff_id: int
    user_id: int
    email: str
    full_name: str
    company_id: int
    role: str


async def _email_is_taken(db: AsyncSession, email: str) -> bool:
    existing = (await db.execute(select(PortalUser.id).where(PortalUser.email == email))).scalar_one_or_none()
    return existing is not None


async def _unique_staff_email(db: AsyncSession, staff: Staff, preferred: str | None = None) -> str:
    email = normalize_email(preferred or staff.email or '')
    if not email:
        raise ValueError(f'Real email is required for staff {staff.id}')
    if not is_deliverable_portal_email(email):
        raise ValueError(f'Real email is required for staff {staff.id}')
    if not await _email_is_taken(db, email):
        return email
    raise ValueError(f'Email already registered: {email}')


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
    company_ids: list[int] | None = None,
) -> ProvisionedAccount:
    if staff.portal_user_id is not None:
        raise ValueError(f'Staff {staff.id} already has a portal account')
    if staff.fired:
        raise ValueError(f'Staff {staff.id} is inactive')
    if not await _portal_user_id_is_available(db, staff.id):
        raise ValueError(f'Portal user id {staff.id} is already taken')

    portal_account_id = (
        await db.execute(
            select(PortalBranch.portal_account_id).where(PortalBranch.company_id == staff.company_id)
        )
    ).scalar_one_or_none()
    if portal_account_id is None:
        raise ValueError(f'Staff company {staff.company_id} is not assigned to a tenant')

    login_email = await _unique_staff_email(db, staff, email)
    bootstrap_password = password or generate_bootstrap_password()
    now = datetime.utcnow()

    user = PortalUser(
        id=staff.id,
        portal_account_id=int(portal_account_id),
        email=login_email,
        password_hash=await asyncio.to_thread(hash_password, bootstrap_password),
        full_name=staff.name,
        role=role,
        is_active=True,
        email_verified_at=now,
        created_at=now,
    )
    db.add(user)
    await db.flush()
    if user.id != staff.id:
        raise RuntimeError(f'Portal user id mismatch: expected {staff.id}, got {user.id}')

    await set_user_branches(db, user.id, company_ids or [staff.company_id])
    staff.portal_user_id = user.id
    if staff.fired:
        staff.fired = 0
    await _ensure_portal_user_id_sequence(db)

    return ProvisionedAccount(
        staff_id=staff.id,
        user_id=user.id,
        email=login_email,
        full_name=staff.name,
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
