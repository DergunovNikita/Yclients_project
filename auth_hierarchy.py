"""Role hierarchy rules for portal user administration."""

from __future__ import annotations

from fastapi import HTTPException

from models import PORTAL_ROLES

ROLE_LEVEL = {role: index for index, role in enumerate(PORTAL_ROLES)}

USER_ADMIN_ROLES = ('super_admin', 'branch_admin')
USER_MANAGER_ROLES = (*USER_ADMIN_ROLES, 'manager')


def role_level(role: str) -> int:
    if role not in ROLE_LEVEL:
        raise HTTPException(status_code=400, detail=f'Invalid role. Allowed: {", ".join(PORTAL_ROLES)}')
    return ROLE_LEVEL[role]


def assignable_roles(actor_role: str) -> list[str]:
    actor_rank = role_level(actor_role)
    return [role for role in PORTAL_ROLES if ROLE_LEVEL[role] > actor_rank]


def rank_at_or_below(actor_role: str, target_role: str) -> bool:
    return role_level(target_role) >= role_level(actor_role)


def same_rank(actor_role: str, target_role: str) -> bool:
    return role_level(target_role) == role_level(actor_role)


def branches_overlap(actor_branch_ids: list[int], target_branch_ids: list[int]) -> bool:
    return bool(set(actor_branch_ids) & set(target_branch_ids))


def _branch_scope_ok(actor_role: str, actor_branch_ids: list[int], target_branch_ids: list[int]) -> bool:
    if actor_role == 'super_admin':
        return True
    if not actor_branch_ids or not target_branch_ids:
        return False
    return set(target_branch_ids).issubset(set(actor_branch_ids))


def can_list_user(
    actor_role: str,
    actor_branch_ids: list[int],
    target_role: str,
    target_branch_ids: list[int],
) -> bool:
    """List users with the same rank or lower, scoped by branches when needed."""
    if not rank_at_or_below(actor_role, target_role):
        return False
    if actor_role == 'super_admin':
        return True
    if same_rank(actor_role, target_role):
        if not actor_branch_ids:
            return False
        if not target_branch_ids:
            return False
        return branches_overlap(actor_branch_ids, target_branch_ids)
    return _branch_scope_ok(actor_role, actor_branch_ids, target_branch_ids)


def can_manage_user(
    actor_role: str,
    actor_branch_ids: list[int],
    target_role: str,
    target_branch_ids: list[int],
) -> bool:
    if actor_role not in USER_ADMIN_ROLES:
        return False
    return can_list_user(actor_role, actor_branch_ids, target_role, target_branch_ids)


def assert_can_assign_role(actor_role: str, target_role: str) -> None:
    if target_role not in PORTAL_ROLES:
        raise HTTPException(status_code=400, detail=f'Invalid role. Allowed: {", ".join(PORTAL_ROLES)}')
    if target_role not in assignable_roles(actor_role):
        raise HTTPException(status_code=403, detail='Cannot assign this role')


def assert_can_manage_user(
    actor_role: str,
    actor_branch_ids: list[int],
    target_role: str,
    target_branch_ids: list[int],
) -> None:
    if not can_manage_user(actor_role, actor_branch_ids, target_role, target_branch_ids):
        raise HTTPException(status_code=403, detail='Cannot manage this user')


def can_manage_staff(
    actor_role: str,
    actor_branch_ids: list[int],
    staff_company_id: int,
) -> bool:
    if actor_role not in USER_ADMIN_ROLES:
        return False
    if actor_role == 'super_admin':
        return True
    return staff_company_id in actor_branch_ids


def assert_can_manage_staff(
    actor_role: str,
    actor_branch_ids: list[int],
    staff_company_id: int,
) -> None:
    if not can_manage_staff(actor_role, actor_branch_ids, staff_company_id):
        raise HTTPException(status_code=403, detail='Cannot manage this staff member')


def validate_company_ids_for_role(
    actor_role: str,
    actor_branch_ids: list[int],
    target_role: str,
    company_ids: list[int],
) -> None:
    if target_role == 'super_admin':
        if company_ids:
            raise HTTPException(status_code=400, detail='super_admin must not have branch assignments')
        return

    if not company_ids:
        raise HTTPException(status_code=400, detail='At least one branch is required for this role')

    if actor_role != 'super_admin':
        invalid = set(company_ids) - set(actor_branch_ids)
        if invalid:
            raise HTTPException(status_code=403, detail=f'Branches not allowed: {sorted(invalid)}')
