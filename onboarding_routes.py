"""Onboarding wizard for freshly-registered owners.

Two logical steps:
  1. add YClients credentials (POST /onboarding/credentials -> returns company preview)
  2. pick branches            (POST /onboarding/branches -> marks onboarding_completed_at)
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth_deps import get_current_user
from database import get_async_db
from data_sources import SOURCE_YCLIENTS, adapter_from_credential, adapter_from_payload, normalize_source_type
from models import PortalBranch, PortalUser, YClientsCredential, YClientsCredentialCompany
from portal_audit import log_portal_audit
from yclients_credentials import CredentialsConfigError, mark_credential_failure_async, mark_credential_success_async, new_credential
from sync_jobs import SyncJobService

router = APIRouter()


class OnboardingCredentialsRequest(BaseModel):
    source_type: str = Field(default=SOURCE_YCLIENTS, min_length=1, max_length=32)
    partner_token: str = Field(min_length=1, max_length=4096)
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)
    title: str = Field(default='YClients integration', min_length=1, max_length=255)


class OnboardingBranchesRequest(BaseModel):
    source_type: str = Field(default=SOURCE_YCLIENTS, min_length=1, max_length=32)
    credential_id: int
    company_ids: list[int] = Field(min_length=1, max_length=200)


def _require_owner(user: PortalUser) -> None:
    if user.role != 'owner':
        raise HTTPException(status_code=403, detail='Onboarding is only available to owner accounts')
    if user.portal_account_id is None:
        raise HTTPException(status_code=400, detail='Owner is not attached to a tenant')


async def _account_credentials(db: AsyncSession, portal_account_id: int) -> list[YClientsCredential]:
    rows = await db.execute(
        select(YClientsCredential)
        .where(YClientsCredential.portal_account_id == portal_account_id)
        .order_by(YClientsCredential.id.asc())
    )
    return list(rows.scalars().all())


async def _account_branches(db: AsyncSession, portal_account_id: int) -> list[int]:
    rows = await db.execute(
        select(PortalBranch.company_id).where(PortalBranch.portal_account_id == portal_account_id)
    )
    return sorted(int(row[0]) for row in rows.all())


def _step_from_state(user: PortalUser, has_credentials: bool, has_branches: bool) -> str:
    if not has_credentials:
        return 'pending_credentials'
    if not has_branches:
        return 'pending_branches'
    return 'done'


@router.get('/state')
async def onboarding_state(
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _require_owner(user)
    credentials = await _account_credentials(db, user.portal_account_id)
    branches = await _account_branches(db, user.portal_account_id)
    has_credentials = bool(credentials)
    has_branches = bool(branches)
    step = _step_from_state(user, has_credentials, has_branches)
    if step == 'done' and user.onboarding_completed_at is None:
        user.onboarding_completed_at = datetime.utcnow()
        await db.commit()
    return {
        'success': True,
        'data': {
            'step': step,
            'email_verified': user.email_verified_at is not None,
            'has_credentials': has_credentials,
            'credentials': [
                {'id': item.id, 'source_type': SOURCE_YCLIENTS, 'title': item.title, 'is_active': bool(item.is_active)}
                for item in credentials
            ],
            'branches': branches,
            'completed_at': user.onboarding_completed_at.isoformat() if user.onboarding_completed_at else None,
        },
    }


@router.post('/credentials')
async def onboarding_credentials(
    body: OnboardingCredentialsRequest,
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _require_owner(user)

    source_type = normalize_source_type(body.source_type)
    adapter = adapter_from_payload(
        source_type,
        partner_token=body.partner_token,
        login=body.login,
        password=body.password,
    )
    if not adapter.authenticate():
        raise HTTPException(status_code=400, detail='Data source authentication failed')

    available = [item.as_payload() for item in adapter.list_branches()]
    if not available:
        raise HTTPException(status_code=400, detail='Data source returned no companies for these credentials')

    conflicting = []
    for item in available:
        existing = (
            await db.execute(
                select(PortalBranch).where(PortalBranch.company_id == item['company_id'])
            )
        ).scalar_one_or_none()
        if existing is not None and existing.portal_account_id != user.portal_account_id:
            conflicting.append(item['company_id'])
    if conflicting:
        raise HTTPException(
            status_code=409,
            detail=f'Companies already linked to another tenant: {sorted(conflicting)}',
        )

    try:
        credential = new_credential(
            portal_account_id=user.portal_account_id,
            title=body.title,
            partner_token=body.partner_token,
            login=body.login,
            password=body.password,
        )
    except CredentialsConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    db.add(credential)
    await db.flush()
    await mark_credential_success_async(db, credential.id)
    await log_portal_audit(
        db,
        actor_user_id=user.id,
        portal_account_id=user.portal_account_id,
        action='yclients_credentials.created',
        target_type='yclients_credential',
        target_id=credential.id,
        metadata={'source': 'onboarding'},
    )
    await db.commit()
    await db.refresh(credential)

    return {
        'success': True,
        'data': {
            'source_type': source_type,
            'credential_id': credential.id,
            'companies': available,
        },
    }


@router.post('/branches')
async def onboarding_branches(
    body: OnboardingBranchesRequest,
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    _require_owner(user)
    credential = (
        await db.execute(
            select(YClientsCredential).where(
                YClientsCredential.id == body.credential_id,
                YClientsCredential.portal_account_id == user.portal_account_id,
            )
        )
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(status_code=404, detail='Credential not found for this tenant')

    source_type = normalize_source_type(body.source_type)
    company_ids = sorted({int(cid) for cid in body.company_ids})
    if not company_ids:
        raise HTTPException(status_code=400, detail='Pick at least one branch')

    adapter = adapter_from_credential(source_type, credential)
    if not adapter.authenticate():
        await mark_credential_failure_async(db, credential.id, 'YClients re-authentication failed')
        await db.commit()
        raise HTTPException(status_code=400, detail='Data source re-authentication failed')
    await mark_credential_success_async(db, credential.id)

    await adapter.materialize_branches(db, user.portal_account_id, company_ids)

    for cid in company_ids:
        link = (
            await db.execute(
                select(YClientsCredentialCompany).where(
                    YClientsCredentialCompany.company_id == cid
                )
            )
        ).scalar_one_or_none()
        if link is None:
            db.add(YClientsCredentialCompany(credential_id=credential.id, company_id=cid))
        else:
            link.credential_id = credential.id

    user.onboarding_completed_at = datetime.utcnow()
    await log_portal_audit(
        db,
        actor_user_id=user.id,
        portal_account_id=user.portal_account_id,
        action='onboarding.completed',
        target_type='portal_account',
        target_id=user.portal_account_id,
        metadata={'source_type': source_type, 'credential_id': credential.id, 'company_ids': company_ids},
    )
    sync_job = await SyncJobService().async_enqueue_job(
        db,
        'full',
        'onboarding',
        portal_account_id=user.portal_account_id,
        credential_id=credential.id,
        company_ids=company_ids,
    )
    await db.commit()
    return {
        'success': True,
        'data': {
            'source_type': source_type,
            'company_ids': company_ids,
            'sync_job_id': sync_job.id,
            'sync_status': sync_job.status,
            'completed_at': user.onboarding_completed_at.isoformat(),
        },
    }
