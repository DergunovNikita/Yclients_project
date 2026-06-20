"""Sync portal users with staff records used in dashboard filters."""

from __future__ import annotations

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import PortalUser, Staff

# Legacy alias; use portal_user_syncs_to_staff() for rules.
STAFF_SYNC_ROLES = ('branch_admin', 'manager', 'viewer')


def portal_user_syncs_to_staff(user: PortalUser) -> bool:
    """Portal users with branches appear in dashboard «Работник» (not super_admin)."""
    return user.role != 'super_admin' and bool(user.is_active)


def staff_display_name(user: PortalUser) -> str:
    if user.full_name and user.full_name.strip():
        return user.full_name.strip()
    return user.email.split('@', 1)[0]


async def _next_staff_id(db: AsyncSession) -> int:
    current_max = (await db.execute(select(func.max(Staff.id)))).scalar_one_or_none()
    return int(current_max or 0) + 1


async def sync_portal_user_staff(
    db: AsyncSession,
    user: PortalUser,
    company_ids: list[int],
) -> None:
    """Create or update staff rows for portal users who appear in worker filters."""
    linked = (
        await db.execute(select(Staff).where(Staff.portal_user_id == user.id))
    ).scalars().all()

    if not portal_user_syncs_to_staff(user) or not company_ids:
        if linked:
            await db.execute(
                update(Staff)
                .where(Staff.portal_user_id == user.id)
                .values(fired=1)
            )
        return

    name = staff_display_name(user)
    by_company = {row.company_id: row for row in linked}
    target_companies = set(company_ids)

    for company_id in sorted(target_companies):
        row = by_company.get(company_id)
        if row is None:
            new_id = await _next_staff_id(db)
            db.add(
                Staff(
                    id=new_id,
                    name=name,
                    position=user.role,
                    company_id=company_id,
                    portal_user_id=user.id,
                    fired=0,
                    bookable=True,
                )
            )
            await db.flush()
            continue
        if int(row.id) == int(user.id):
            # Provisioned YClients staff: keep synced name/position from CRM.
            row.fired = 0
            row.bookable = True
            continue
        row.name = name
        row.position = user.role
        row.fired = 0
        row.bookable = True

    for company_id, row in by_company.items():
        if company_id not in target_companies:
            row.fired = 1


async def deactivate_portal_user_staff(db: AsyncSession, portal_user_id: int) -> None:
    await db.execute(
        update(Staff)
        .where(Staff.portal_user_id == portal_user_id)
        .values(fired=1)
    )


async def sync_all_portal_users_staff(db: AsyncSession) -> None:
    """Ensure staff rows exist for all portal users that should appear in worker filters."""
    from auth_service import load_user_branch_ids

    users = (await db.execute(select(PortalUser).order_by(PortalUser.id.asc()))).scalars().all()
    for user in users:
        if not portal_user_syncs_to_staff(user):
            continue
        branch_ids = await load_user_branch_ids(db, user.id)
        await sync_portal_user_staff(db, user, branch_ids)


async def list_unlinked_staff(
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
