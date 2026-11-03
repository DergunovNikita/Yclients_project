"""Tie a portal user to the real YClients `staff` row of the same person, and release it again.

An account made with «Создать пользователя» only has a mirror row (`source_type = 'portal'`, see
`portal_staff_sync`), which carries no visits. Linking points `staff.portal_user_id` at the person's own row of the
branch, retires the mirror and moves its manual facts across, so every dashboard endpoint clamps the account to the
row that has the numbers.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import PortalUser, Staff, is_service_staff_name
from portal_audit import log_portal_audit
from portal_staff_sync import mirror_staff_clause, sync_portal_user_staff, yclients_staff_clause
from scripts.merge_mirror_manual_facts import MergeRefused, apply_moves

LINKABLE_ROLES = ('admin', 'barber')


class StaffLinkNotFound(LookupError):
    """No such row for this actor: missing, fired, a mirror, or outside the actor's branches."""


class StaffLinkConflict(ValueError):
    """The row or the branch is already taken, or the manual facts cannot be merged safely."""


class StaffLinkRefused(ValueError):
    """The request is well-formed but the account or branch cannot take a link."""


@dataclass(frozen=True)
class StaffLinkResult:
    staff_id: int
    company_id: int
    retired_staff_ids: list[int]
    moved_fact_values: int


def _row_payload(row: Staff) -> dict:
    return {
        'staff_id': int(row.id),
        'company_id': int(row.company_id),
        'name': row.name,
        'position': row.position,
        'fired': bool(row.fired),
    }


async def _lock_user(db: AsyncSession, user_id: int) -> None:
    """Serialise link and unlink of one account, so «one linked row per branch» cannot be raced past."""
    await db.execute(select(PortalUser.id).where(PortalUser.id == user_id).with_for_update())


async def _linked_rows(db: AsyncSession, user_id: int) -> list[Staff]:
    return (
        await db.execute(
            select(Staff)
            .where(Staff.portal_user_id == user_id, yclients_staff_clause())
            .order_by(Staff.company_id.asc(), Staff.id.asc())
            .execution_options(populate_existing=True)
        )
    ).scalars().all()


def _linkable_clause():
    """A row YClients gave out: it has an `external_id`, which is also what keeps it from reading as a mirror."""
    return and_(yclients_staff_clause(), Staff.external_id.is_not(None))


def _assert_linkable_role(user: PortalUser) -> None:
    if user.role not in LINKABLE_ROLES:
        raise StaffLinkRefused('Only admin and barber accounts are linked to YClients staff')


async def list_staff_links(
    db: AsyncSession, user: PortalUser, user_branch_ids: list[int], scope_branch_ids: list[int]
) -> list[dict]:
    """Per branch of the user (inside the actor's scope): the linked row and the rows that could be linked.

    A row linked in a branch the user has since left is listed too, so it can be released.
    """
    _assert_linkable_role(user)
    scope = set(scope_branch_ids)
    linked = [row for row in await _linked_rows(db, user.id) if row.company_id in scope]
    linked_by_company: dict[int, list[Staff]] = {}
    for row in linked:
        linked_by_company.setdefault(int(row.company_id), []).append(row)
    open_companies = sorted(set(user_branch_ids) & scope)
    candidates: dict[int, list[Staff]] = {company_id: [] for company_id in open_companies}
    if open_companies:
        rows = await db.execute(
            select(Staff)
            .where(
                Staff.company_id.in_(open_companies),
                Staff.portal_user_id.is_(None),
                Staff.fired == 0,
                _linkable_clause(),
            )
        )
        # Sorted here, not by the database: its collation decides how Cyrillic and case compare.
        for row in sorted(rows.scalars(), key=lambda item: ((item.name or '').strip().casefold(), item.id)):
            if not is_service_staff_name(row.name):
                candidates[int(row.company_id)].append(row)
    return [
        {
            'company_id': company_id,
            'linked': [_row_payload(row) for row in linked_by_company.get(company_id, [])],
            # A branch with a linked row takes no second one.
            'candidates': (
                [] if company_id in linked_by_company else [_row_payload(row) for row in candidates.get(company_id, [])]
            ),
        }
        for company_id in sorted(set(open_companies) | set(linked_by_company))
    ]


async def link_staff(
    db: AsyncSession,
    *,
    actor_user_id: int,
    user: PortalUser,
    staff_id: int,
    user_branch_ids: list[int],
    scope_branch_ids: list[int],
) -> StaffLinkResult:
    """Link a YClients row to the user in the caller's transaction; the caller commits."""
    _assert_linkable_role(user)
    user_id = int(user.id)
    portal_account_id = user.portal_account_id
    await _lock_user(db, user_id)

    staff = (
        await db.execute(
            select(Staff).where(Staff.id == staff_id, Staff.fired == 0, _linkable_clause())
        )
    ).scalar_one_or_none()
    # "Exists in a branch you cannot reach" must look like "does not exist".
    if staff is None or staff.company_id not in set(scope_branch_ids) or is_service_staff_name(staff.name):
        raise StaffLinkNotFound('Staff member not found')
    company_id = int(staff.company_id)
    if company_id not in set(user_branch_ids):
        raise StaffLinkRefused("Staff member's branch is not one of the user's branches")
    if staff.portal_user_id is not None:
        raise StaffLinkConflict('Staff member is already linked to a portal account')
    if any(int(row.company_id) == company_id for row in await _linked_rows(db, user_id)):
        raise StaffLinkConflict('The user already has a linked staff member in this branch; unlink it first')

    claimed = await db.execute(
        update(Staff)
        .where(Staff.id == staff_id, Staff.portal_user_id.is_(None), Staff.fired == 0, _linkable_clause())
        .values(portal_user_id=user_id)
    )
    if claimed.rowcount != 1:  # another actor linked it between the read and the write
        raise StaffLinkConflict('Staff member is already linked to a portal account')

    mirrors = (
        await db.execute(
            select(Staff)
            .where(Staff.portal_user_id == user_id, Staff.company_id == company_id, mirror_staff_clause())
            .order_by(Staff.id.asc())
            .with_for_update()
        )
    ).scalars().all()
    retired_ids = [int(mirror.id) for mirror in mirrors]
    moved = 0
    try:
        for mirror_id in retired_ids:
            moves = await apply_moves(
                db,
                {mirror_id: staff_id},
                actor_user_id=actor_user_id,
                portal_account_id=portal_account_id,
                delete_sources=True,
            )
            moved += len(moves)
    except MergeRefused as exc:
        raise StaffLinkConflict(f'Manual facts of the mirror row cannot be moved: {exc}') from exc
    for mirror in mirrors:
        # Detached as well as fired: a mirror still pointing at the user would be woken by `sync_portal_user_staff`.
        mirror.fired = 1
        mirror.portal_user_id = None

    await log_portal_audit(
        db,
        actor_user_id=actor_user_id,
        portal_account_id=portal_account_id,
        action='portal_user.staff_linked',
        target_type='portal_user',
        target_id=user_id,
        metadata={
            'staff_id': staff_id,
            'company_id': company_id,
            'retired_mirror_ids': retired_ids,
            'moved_fact_values': moved,
        },
    )
    await db.flush()
    return StaffLinkResult(staff_id, company_id, retired_ids, moved)


async def unlink_staff(
    db: AsyncSession,
    *,
    actor_user_id: int,
    user: PortalUser,
    staff_id: int,
    user_branch_ids: list[int],
    scope_branch_ids: list[int],
) -> int:
    """Release a linked row (it is YClients' and is never fired) and give the branch its mirror back.

    Returns the branch id. The caller commits.
    """
    user_id = int(user.id)
    portal_account_id = user.portal_account_id
    await _lock_user(db, user_id)
    staff = (
        await db.execute(
            select(Staff).where(Staff.id == staff_id, Staff.portal_user_id == user_id, yclients_staff_clause())
        )
    ).scalar_one_or_none()
    if staff is None or staff.company_id not in set(scope_branch_ids):
        raise StaffLinkNotFound('Linked staff member not found')
    company_id = int(staff.company_id)

    released = await db.execute(
        update(Staff)
        .where(Staff.id == staff_id, Staff.portal_user_id == user_id)
        .values(portal_user_id=None)
    )
    if released.rowcount != 1:
        raise StaffLinkNotFound('Linked staff member not found')

    await sync_portal_user_staff(db, user, user_branch_ids)
    await log_portal_audit(
        db,
        actor_user_id=actor_user_id,
        portal_account_id=portal_account_id,
        action='portal_user.staff_unlinked',
        target_type='portal_user',
        target_id=user_id,
        metadata={'staff_id': staff_id, 'company_id': company_id},
    )
    await db.flush()
    return company_id
