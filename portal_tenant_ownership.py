"""Tenant ownership helpers for portal account branch assignment.

A tenant without business users is "admin-only" and its branches may be taken over by an owner onboarding with
valid YClients credentials. The role filter below names platform admins explicitly, but in the real schema
(`ck_portal_users_platform_admin_no_tenant`) they have no tenant at all, so in practice the tenant is simply empty.
Taking over moves the branch (and its `Company` row with the history) to the owner's tenant and drops the stale
credential<->company links on the source side.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import PortalBranch, PortalUser, YClientsCredential, YClientsCredentialCompany


async def tenant_has_business_users(db: AsyncSession, portal_account_id: int) -> bool:
    """Return true when a tenant has real users, not only platform admins."""
    row = (
        await db.execute(
            select(PortalUser.id)
            .where(
                PortalUser.portal_account_id == portal_account_id,
                PortalUser.role != 'platform_admin',
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def can_reassign_branch_from_tenant(
    db: AsyncSession,
    source_portal_account_id: int,
    target_portal_account_id: int,
) -> bool:
    if source_portal_account_id == target_portal_account_id:
        return True
    return not await tenant_has_business_users(db, source_portal_account_id)


async def _detach_source_credential_company_links(
    db: AsyncSession,
    source_portal_account_id: int,
    company_id: int,
) -> None:
    """Drop stale credential<->company links from the source tenant."""
    source_credential_ids = (
        await db.execute(
            select(YClientsCredential.id).where(
                YClientsCredential.portal_account_id == source_portal_account_id
            )
        )
    ).scalars().all()
    if not source_credential_ids:
        return
    await db.execute(
        delete(YClientsCredentialCompany).where(
            YClientsCredentialCompany.company_id == company_id,
            YClientsCredentialCompany.credential_id.in_(source_credential_ids),
        )
    )


async def reassign_branch_from_admin_only_tenant(
    db: AsyncSession,
    branch: PortalBranch,
    target_portal_account_id: int,
) -> bool:
    if branch.portal_account_id == target_portal_account_id:
        return True
    if not await can_reassign_branch_from_tenant(db, branch.portal_account_id, target_portal_account_id):
        return False
    await _detach_source_credential_company_links(
        db, branch.portal_account_id, branch.company_id
    )
    branch.portal_account_id = target_portal_account_id
    return True
