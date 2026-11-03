"""Sync portal users with staff records used in dashboard filters."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from models import SOURCE_PORTAL, PortalUser, PortalUserBranch, Staff, is_service_staff_name

# Attempts to claim a synthetic staff id before giving up; a retry only follows a lost race.
STAFF_ID_ATTEMPTS = 3


def portal_user_syncs_to_staff(user: PortalUser) -> bool:
    """Portal users with branch-scoped roles appear in dashboard «Работник»."""
    return user.role not in {'platform_admin', 'owner'} and bool(user.is_active)


def staff_display_name(user: PortalUser) -> str:
    if user.full_name and user.full_name.strip():
        return user.full_name.strip()
    return user.email.split('@', 1)[0]


async def _next_staff_id(db: AsyncSession) -> int:
    current_max = (await db.execute(select(func.max(Staff.id)))).scalar_one_or_none()
    return int(current_max or 0) + 1


async def _add_portal_staff(db: AsyncSession, *, name: str, role: str, company_id: int, user_id: int) -> None:
    """Insert a portal-owned staff row under max(id)+1.

    Two concurrent admin loads (or the ETL, which takes Staff ids from YClients) can pick the same
    id; the loser's INSERT violates the primary key, so it rolls back its savepoint and picks again.
    """
    for attempt in range(1, STAFF_ID_ATTEMPTS + 1):
        row = Staff(
            id=await _next_staff_id(db),
            name=name,
            position=role,
            company_id=company_id,
            portal_user_id=user_id,
            source_type=SOURCE_PORTAL,
            fired=0,
            bookable=True,
        )
        try:
            async with db.begin_nested():
                db.add(row)
            return
        except IntegrityError:
            if attempt == STAFF_ID_ATTEMPTS:
                raise


def _assign(row: Staff, **values) -> bool:
    """Set attributes that differ; report whether anything did, so callers commit only real repairs."""
    changed = False
    for field, value in values.items():
        if getattr(row, field) != value:
            setattr(row, field, value)
            changed = True
    return changed


def is_mirror_row(row: Staff) -> bool:
    """The portal's own synthetic row. Twin of `mirror_staff_clause`: change both together."""
    return row.source_type == SOURCE_PORTAL or (row.portal_user_id is not None and row.external_id is None)


def _is_crm_row(row: Staff, portal_user_id: int) -> bool:
    return row.portal_user_id == portal_user_id and not is_mirror_row(row)


def mirror_staff_clause():
    """Rows the portal made: marked `source_type = 'portal'`, or linked to a user without a YClients `external_id`.

    The second half is for the deploy window between migration `0062` and the restart of the API: code that predates
    the marker still inserts its mirrors as `'yclients'` and never gives them an `external_id`, while every row of
    YClients (the sync, adoption by `0033`, the demo seed) has one.
    """
    return or_(Staff.source_type == SOURCE_PORTAL, and_(Staff.portal_user_id.is_not(None), Staff.external_id.is_(None)))


def yclients_staff_clause():
    """Rows that come from YClients — everything but the portal's mirrors. YClients decides their `fired`."""
    return ~mirror_staff_clause()


def crm_staff_clause(portal_user_id: int):
    """A row of YClients linked to the user: provisioned from it, or linked by an admin (`portal_staff_link`)."""
    return and_(Staff.portal_user_id == portal_user_id, yclients_staff_clause())


def _portal_owned_staff_clause(portal_user_id: int):
    """Rows this module created — the only ones whose `fired` the portal may change.

    A portal account provisioned from a real employee points at that employee's row
    (portal_account_provision.provision_staff_account), and an admin can link an existing
    YClients row to an account the same way, so YClients owns that row's `fired` and name.
    Firing it because the account is scoped to other branches made a working employee
    disappear from the «Работник» filter.

    The CRM row is told by `source_type`, not by id: a mirror row takes max(id)+1 from the same number space as
    portal user ids and can land on its own user's id, and a linked row has any id at all.
    """
    return and_(Staff.portal_user_id == portal_user_id, mirror_staff_clause())


async def sync_portal_user_staff(
    db: AsyncSession,
    user: PortalUser,
    company_ids: list[int],
    *,
    linked: list[Staff] | None = None,
) -> bool:
    """Create or update staff rows for portal users who appear in worker filters.

    `linked` is the user's staff rows when the caller already loaded them in bulk.
    Returns whether anything was written.
    """
    if linked is None:
        linked = (
            await db.execute(select(Staff).where(Staff.portal_user_id == user.id))
        ).scalars().all()

    if not portal_user_syncs_to_staff(user) or not company_ids:
        if any(row.fired != 1 and not _is_crm_row(row, user.id) for row in linked):
            await db.execute(
                update(Staff)
                .where(_portal_owned_staff_clause(user.id))
                .values(fired=1)
            )
            return True
        return False

    name = staff_display_name(user)
    by_company: dict[int | None, Staff] = {}
    for row in sorted(linked, key=lambda item: (_is_crm_row(item, user.id), item.fired != 1, item.id)):
        # One row stands for the branch: a linked YClients row over a mirror, a live one over a fired one.
        by_company[row.company_id] = row
    target_companies = set(company_ids)

    changed = False
    for company_id in sorted(target_companies):
        row = by_company.get(company_id)
        if row is None:
            await _add_portal_staff(db, name=name, role=user.role, company_id=company_id, user_id=user.id)
            changed = True
        elif _is_crm_row(row, user.id):
            # Provisioned YClients staff: name, position and `fired` stay owned by the CRM.
            changed |= _assign(row, bookable=True)
        else:
            changed |= _assign(row, name=name, position=user.role, fired=0, bookable=True)

    for row in linked:
        if _is_crm_row(row, user.id):
            continue
        if row.company_id not in target_companies or by_company[row.company_id] is not row:
            changed |= _assign(row, fired=1)
    return changed


async def detach_portal_user_staff(db: AsyncSession, portal_user_id: int) -> None:
    """Before a portal user is deleted: fire the rows the portal made and release the CRM row.

    The CRM row (linked, not a mirror) belongs to YClients, so it stays, but its link
    must go: left pointing at a dead id it would be hidden from the unlinked list and refuse a new account.
    """
    await db.execute(
        update(Staff)
        .where(_portal_owned_staff_clause(portal_user_id))
        .values(fired=1)
    )
    await db.execute(
        update(Staff)
        .where(crm_staff_clause(portal_user_id), Staff.portal_user_id == portal_user_id)
        .values(portal_user_id=None)
    )


async def sync_all_portal_users_staff(db: AsyncSession, portal_account_id: int | None = None) -> bool:
    """Ensure staff rows exist for all portal users that should appear in worker filters.

    Pass `portal_account_id` to scope the walk to one tenant — every caller with a resolved
    tenant should, so a request against tenant A never writes and commits Staff rows for
    tenant B. Leave it unscoped only for a deliberate cross-tenant repair (e.g. a backfill).
    Branches and staff rows are read in two queries for the whole walk, not per user.
    Returns whether anything was written, so a read-only caller can skip the commit.
    """
    stmt = select(PortalUser).order_by(PortalUser.id.asc())
    if portal_account_id is not None:
        stmt = stmt.where(PortalUser.portal_account_id == portal_account_id)
    users = [user for user in (await db.execute(stmt)).scalars().all() if portal_user_syncs_to_staff(user)]
    if not users:
        return False
    user_ids = [user.id for user in users]

    branch_ids: dict[int, list[int]] = {user_id: [] for user_id in user_ids}
    branch_rows = await db.execute(
        select(PortalUserBranch.user_id, PortalUserBranch.company_id)
        .where(PortalUserBranch.user_id.in_(user_ids))
        .order_by(PortalUserBranch.company_id.asc())
    )
    for user_id, company_id in branch_rows.all():
        branch_ids[user_id].append(company_id)

    linked: dict[int, list[Staff]] = {user_id: [] for user_id in user_ids}
    staff_rows = await db.execute(select(Staff).where(Staff.portal_user_id.in_(user_ids)))
    for row in staff_rows.scalars().all():
        linked[row.portal_user_id].append(row)

    changed = False
    for user in users:
        changed |= await sync_portal_user_staff(db, user, branch_ids[user.id], linked=linked[user.id])
    return changed


async def list_unlinked_staff(
    db: AsyncSession,
    allowed_company_ids: list[int] | None,
) -> list[Staff]:
    stmt = (
        select(Staff)
        .where(
            Staff.fired == 0,
            Staff.portal_user_id.is_(None),
            # A retired mirror a concurrent sync woke up again is nobody's chair either.
            yclients_staff_clause(),
        )
        .order_by(Staff.company_id.asc(), Staff.name.asc(), Staff.id.asc())
    )
    if allowed_company_ids is not None:
        stmt = stmt.where(Staff.company_id.in_(allowed_company_ids))
    # The waiting list and «Администратор…» placeholders are service rows, not people to give a login.
    return [row for row in (await db.execute(stmt)).scalars() if not is_service_staff_name(row.name)]
