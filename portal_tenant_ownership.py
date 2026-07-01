"""Tenant ownership helpers for portal account branch assignment."""

from __future__ import annotations

from sqlalchemy import select
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
    company_id: int | None = None,
) -> bool:
    if source_portal_account_id == target_portal_account_id:
        return True
    if await tenant_has_business_users(db, source_portal_account_id):
        return False
    if company_id is None:
        return True
    row = (
        await db.execute(
            select(YClientsCredentialCompany.id)
            .join(YClientsCredential, YClientsCredential.id == YClientsCredentialCompany.credential_id)
            .where(
                YClientsCredential.portal_account_id == source_portal_account_id,
                YClientsCredential.is_active.is_(True),
                YClientsCredentialCompany.company_id == company_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is None


async def reassign_branch_from_admin_only_tenant(
    db: AsyncSession,
    branch: PortalBranch,
    target_portal_account_id: int,
) -> bool:
    if branch.portal_account_id == target_portal_account_id:
        return True
    if not await can_reassign_branch_from_tenant(
        db,
        branch.portal_account_id,
        target_portal_account_id,
        branch.company_id,
    ):
        return False
    branch.portal_account_id = target_portal_account_id
    return True
