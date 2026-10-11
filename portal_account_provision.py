"""Create portal accounts for staff members without login credentials."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from clock import utcnow

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth_service import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    is_deliverable_portal_email,
    normalize_email,
    set_user_branches,
)
from models import PortalBranch, PortalUser, Staff
from plan_config import normalize_staff_category
from portal_staff_sync import list_unlinked_staff

logger = logging.getLogger('yclients.auth')


@dataclass
class ProvisionedAccount:
    staff_id: int
    user_id: int
    email: str
    full_name: str
    company_id: int
    role: str


@dataclass
class _Taken:
    """What the provisioning checks would ask the database, loaded once for a whole batch."""

    user_ids: set[int]
    emails: set[str]
    tenant_by_company: dict[int, int]

    @classmethod
    async def load(cls, db: AsyncSession, staff_rows: list[Staff], *, email: str | None = None):
        """Look up the ids, logins and tenants the given staff rows could collide with.

        `email` replaces the CRM address when a single row is provisioned with an explicit login.
        """
        staff_ids = {int(staff.id) for staff in staff_rows}
        company_ids = {int(staff.company_id) for staff in staff_rows if staff.company_id is not None}
        candidates = {normalize_email(email or staff.email or '') for staff in staff_rows} - {''}
        user_ids = set((await db.execute(select(PortalUser.id).where(PortalUser.id.in_(staff_ids)))).scalars())
        taken_emails = set(
            (await db.execute(select(PortalUser.email).where(PortalUser.email.in_(candidates)))).scalars()
        )
        tenants = (
            await db.execute(
                select(PortalBranch.company_id, PortalBranch.portal_account_id).where(
                    PortalBranch.company_id.in_(company_ids)
                )
            )
        ).all()
        return cls(user_ids, taken_emails, {int(company): int(tenant) for company, tenant in tenants})


def _checked_staff_email(staff: Staff, taken: _Taken, preferred: str | None = None) -> str:
    email = normalize_email(preferred or staff.email or '')
    if not email or not is_deliverable_portal_email(email):
        raise ValueError(f'Real email is required for staff {staff.id}')
    if email in taken.emails:
        raise ValueError(f'Email already registered: {email}')
    return email


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


async def _provision(
    db: AsyncSession,
    staff: Staff,
    taken: _Taken,
    *,
    email: str | None,
    role: str,
    password: str | None,
    company_ids: list[int] | None,
) -> ProvisionedAccount:
    if staff.portal_user_id is not None:
        raise ValueError(f'Staff {staff.id} already has a portal account')
    if staff.fired:
        raise ValueError(f'Staff {staff.id} is inactive')
    if staff.id in taken.user_ids:
        raise ValueError(f'Portal user id {staff.id} is already taken')

    portal_account_id = taken.tenant_by_company.get(staff.company_id)
    if portal_account_id is None:
        raise ValueError(f'Staff company {staff.company_id} is not assigned to a tenant')

    login_email = _checked_staff_email(staff, taken, email)
    # A generated password is never shown to anyone, so it needs no hash of its own: the shared
    # never-revealed hash costs login the same bcrypt round and spares ~250 ms per account.
    password_hash = await asyncio.to_thread(hash_password, password) if password else DUMMY_PASSWORD_HASH
    now = utcnow()

    user = PortalUser(
        id=staff.id,
        portal_account_id=portal_account_id,
        email=login_email,
        password_hash=password_hash,
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

    return ProvisionedAccount(
        staff_id=staff.id,
        user_id=user.id,
        email=login_email,
        full_name=staff.name,
        company_id=staff.company_id,
        role=role,
    )


async def provision_staff_account(
    db: AsyncSession,
    staff: Staff,
    *,
    email: str | None = None,
    role: str = 'barber',
    password: str | None = None,
    company_ids: list[int] | None = None,
) -> ProvisionedAccount:
    taken = await _Taken.load(db, [staff], email=email)
    account = await _provision(
        db, staff, taken, email=email, role=role, password=password, company_ids=company_ids
    )
    await _ensure_portal_user_id_sequence(db)
    return account


def role_for_staff(staff: Staff) -> str:
    """Label the account the way the CRM already labels the person.

    `admin` and `barber` grant the same access; the label is what lets the user list tell a
    counter from a chair, and it is the only thing per-role money visibility can key on.
    """
    return 'admin' if normalize_staff_category(staff.position) == 'administrator' else 'barber'


async def provision_all_unlinked_staff(
    db: AsyncSession,
    allowed_company_ids: list[int] | None,
) -> tuple[list[ProvisionedAccount], list[str]]:
    """Give every unlinked staff row an account; a row that cannot get one is reported, not fatal."""
    staff_rows = await list_unlinked_staff(db, allowed_company_ids)
    created: list[ProvisionedAccount] = []
    errors: list[str] = []
    if not staff_rows:
        return created, errors
    taken = await _Taken.load(db, staff_rows)
    # Everything a failed row needs for its message is read before its savepoint can expire the row.
    for staff in staff_rows:
        staff_id = staff.id
        try:
            # The savepoint confines a database error (a concurrent insert took the login) to its row.
            async with db.begin_nested():
                account = await _provision(
                    db, staff, taken, email=None, role=role_for_staff(staff), password=None, company_ids=None
                )
        except ValueError as exc:
            errors.append(str(exc))
        except (RuntimeError, IntegrityError) as exc:
            logger.warning('provisioning staff_id=%s failed: %s', staff_id, type(exc).__name__)
            errors.append(f'Could not create an account for staff {staff_id}')
        else:
            taken.user_ids.add(account.user_id)
            taken.emails.add(account.email)
            created.append(account)
    if created:
        await _ensure_portal_user_id_sequence(db)
    return created, errors
