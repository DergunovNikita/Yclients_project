"""YClients credential administration: create, list, update, delete, and test.

Split out of `auth_routes.py` (still owns portal-user login/session/admin routes) purely to
keep that file a manageable size. Mounted into `auth_router` via `router.include_router(...)`,
so every URL stays under both `/auth/...` and `/dashboard/auth/...` (see api.py) unchanged.
"""

from __future__ import annotations

from typing import NoReturn

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth_deps import forbid_demo, require_roles
from auth_hierarchy import CREDENTIAL_ADMIN_ROLES
from auth_service import load_portal_account_branch_ids
from data_sources import SOURCE_YCLIENTS, authenticated_adapter_from_payload, normalize_source_type
from database import get_async_db
from models import PortalAccount, PortalUser, YClientsCredential
from portal_audit import log_portal_audit
from yclients_credentials import (
    CREDENTIAL_STORAGE_FAILED_DETAIL,
    CredentialsConfigError,
    decrypted_credential,
    list_credential_payloads,
    mark_credential_failure_async,
    mark_credential_success_async,
    new_credential,
    set_credential_companies,
    update_credential_secrets,
)

router = APIRouter()
CREDENTIAL_PAYLOAD_INVALID_DETAIL = 'Invalid credential request'
CREDENTIAL_TEST_FAILED_DETAIL = 'Data source authentication failed'
CREDENTIAL_DECRYPT_FAILED_DETAIL = 'Stored credentials could not be decrypted'
CREDENTIAL_SOURCE_UNSUPPORTED_DETAIL = 'Unsupported credential source type'
CREDENTIAL_TEXT_LIMITS = {
    'source_type': 32,
    'partner_token': 4096,
    'login': 255,
    'password': 255,
}


class YClientsCredentialCreateRequest(BaseModel):
    source_type: str = Field(default=SOURCE_YCLIENTS, min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    portal_account_id: int | None = None
    partner_token: str = Field(min_length=1, max_length=4096)
    login: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)
    is_active: bool = True
    company_ids: list[int] = Field(default_factory=list)


class YClientsCredentialUpdateRequest(BaseModel):
    source_type: str | None = Field(default=None, min_length=1, max_length=32)
    title: str | None = Field(default=None, min_length=1, max_length=255)
    portal_account_id: int | None = None
    partner_token: str | None = Field(default=None, min_length=1, max_length=4096)
    login: str | None = Field(default=None, min_length=1, max_length=255)
    password: str | None = Field(default=None, min_length=1, max_length=255)
    is_active: bool | None = None
    company_ids: list[int] | None = None


def _credentials_config_error(exc: CredentialsConfigError) -> HTTPException:
    return HTTPException(status_code=500, detail=CREDENTIAL_STORAGE_FAILED_DETAIL)


async def _load_credential(
    db: AsyncSession,
    credential_id: int,
    actor: PortalUser | None = None,
    active_portal_account_id: int | None = None,
) -> YClientsCredential:
    credential = (
        await db.execute(select(YClientsCredential).where(YClientsCredential.id == credential_id))
    ).scalar_one_or_none()
    if credential is None:
        raise HTTPException(status_code=404, detail='YClients credentials not found')
    if actor is not None:
        if actor.role == 'platform_admin':
            if active_portal_account_id is None:
                raise HTTPException(status_code=400, detail='X-Portal-Account-Id is required')
            if credential.portal_account_id != active_portal_account_id:
                raise HTTPException(status_code=404, detail='YClients credentials not found')
        elif credential.portal_account_id != actor.portal_account_id:
            raise HTTPException(status_code=404, detail='YClients credentials not found')
    return credential


def _credential_account_id(actor: PortalUser, requested: int | None = None) -> int:
    if actor.role == 'platform_admin':
        if requested is None:
            raise HTTPException(status_code=400, detail='portal_account_id is required')
        return requested
    if actor.portal_account_id is None:
        raise HTTPException(status_code=403, detail='Tenant account is required')
    if requested is not None and requested != actor.portal_account_id:
        raise HTTPException(status_code=403, detail='Cannot manage credentials for another tenant')
    return actor.portal_account_id


async def _validated_credential_account_id(
    db: AsyncSession,
    actor: PortalUser,
    requested: int | None = None,
) -> int:
    portal_account_id = _credential_account_id(actor, requested)
    if actor.role == 'platform_admin':
        tenant = await db.get(PortalAccount, portal_account_id)
        if tenant is None:
            raise HTTPException(status_code=404, detail='Portal account not found')
    return portal_account_id


async def _audit_credential_action_failure(
    db: AsyncSession,
    *,
    actor: PortalUser,
    action: str,
    portal_account_id: int | None,
    target_id: int | None = None,
    error_type: str | None = None,
    reason: str | None = None,
    source_type: str | None = None,
    requested_portal_account_id: int | None = None,
) -> None:
    metadata: dict = {'success': False}
    if source_type is not None:
        metadata['source_type'] = source_type
    if error_type is not None:
        metadata['error_type'] = error_type
    if reason is not None:
        metadata['reason'] = reason
    if requested_portal_account_id is not None:
        metadata['requested_portal_account_id'] = requested_portal_account_id
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=portal_account_id,
        action=action,
        target_type='yclients_credential',
        target_id=target_id,
        metadata=metadata,
    )


async def _raise_credential_create_failure(
    db: AsyncSession,
    actor: PortalUser,
    portal_account_id: int,
    exc: Exception,
) -> NoReturn:
    error_type = 'HTTPException' if isinstance(exc, HTTPException) else exc.__class__.__name__
    await _audit_credential_action_failure(
        db,
        actor=actor,
        action='yclients_credentials.create_failed',
        portal_account_id=portal_account_id,
        error_type=error_type,
    )
    await db.commit()
    status_code = exc.status_code if isinstance(exc, HTTPException) else 500
    raise HTTPException(status_code=status_code, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc


def _credential_failure_audit_tenant_id(actor: PortalUser) -> int | None:
    if actor.role == 'platform_admin':
        return None
    return actor.portal_account_id


def _credential_body_string(body: dict, key: str) -> str | None:
    value = body.get(key)
    if not isinstance(value, str) or not value:
        return None
    if len(value) > CREDENTIAL_TEXT_LIMITS[key]:
        return None
    return value


async def _validate_credential_companies(
    db: AsyncSession,
    portal_account_id: int,
    company_ids: list[int],
) -> None:
    if not company_ids:
        return
    existing = set(await load_portal_account_branch_ids(db, portal_account_id))
    missing = sorted(set(company_ids) - existing)
    if missing:
        raise HTTPException(status_code=403, detail=f'Companies outside tenant: {missing}')


def _test_source_credentials(source_type: str | None, partner_token: str, login: str, password: str) -> dict:
    adapter = authenticated_adapter_from_payload(
        source_type,
        partner_token=partner_token,
        login=login,
        password=password,
    )
    return {
        'success': True,
        'source_type': adapter.source_type,
        'message': 'Data source credentials are valid',
    }


async def _sync_credential_companies_from_yclients(
    db: AsyncSession,
    portal_account_id: int,
    partner_token: str,
    login: str,
    password: str,
    source_type: str = SOURCE_YCLIENTS,
) -> list[int]:
    adapter = authenticated_adapter_from_payload(
        source_type,
        partner_token=partner_token,
        login=login,
        password=password,
    )
    company_ids = sorted(item.company_id for item in adapter.list_branches())
    materialized_company_ids = await adapter.materialize_branches(db, portal_account_id, company_ids)
    return sorted(set(materialized_company_ids))


@router.get('/admin/yclients-credentials')
async def admin_list_yclients_credentials(
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*CREDENTIAL_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        portal_account_id = await _validated_credential_account_id(db, actor, x_portal_account_id)
    except HTTPException:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.listed',
            portal_account_id=_credential_failure_audit_tenant_id(actor),
            requested_portal_account_id=x_portal_account_id,
            error_type='tenant_selection_failed',
        )
        await db.commit()
        raise
    payloads = await list_credential_payloads(db, portal_account_id)
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=portal_account_id,
        action='yclients_credentials.listed',
        target_type='yclients_credential',
        metadata={'success': True, 'count': len(payloads)},
    )
    await db.commit()
    return {'success': True, 'data': payloads}


@router.post('/admin/yclients-credentials', dependencies=[Depends(forbid_demo)])
async def admin_create_yclients_credentials(
    body: YClientsCredentialCreateRequest,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*CREDENTIAL_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    source_type = normalize_source_type(body.source_type)
    if actor.role == 'platform_admin' and body.portal_account_id is not None and x_portal_account_id is not None:
        if body.portal_account_id != x_portal_account_id:
            raise HTTPException(status_code=400, detail='portal_account_id does not match X-Portal-Account-Id')
    portal_account_id = await _validated_credential_account_id(db, actor, x_portal_account_id or body.portal_account_id)
    try:
        _test_source_credentials(source_type, body.partner_token, body.login, body.password)
    except Exception as exc:
        await _raise_credential_create_failure(db, actor, portal_account_id, exc)
    company_ids = list(body.company_ids)
    if not company_ids:
        try:
            company_ids = await _sync_credential_companies_from_yclients(
                db,
                portal_account_id,
                body.partner_token,
                body.login,
                body.password,
                source_type,
            )
        except Exception as exc:
            await _raise_credential_create_failure(db, actor, portal_account_id, exc)
    await _validate_credential_companies(db, portal_account_id, company_ids)
    try:
        credential = new_credential(
            portal_account_id,
            body.title,
            body.partner_token,
            body.login,
            body.password,
            body.is_active,
        )
    except CredentialsConfigError as exc:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.create_failed',
            portal_account_id=portal_account_id,
            error_type=exc.__class__.__name__,
        )
        await db.commit()
        raise _credentials_config_error(exc) from exc

    db.add(credential)
    await db.flush()
    await mark_credential_success_async(db, credential.id)
    try:
        await set_credential_companies(db, credential.id, company_ids)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=portal_account_id,
        action='yclients_credentials.created',
        target_type='yclients_credential',
        target_id=credential.id,
        metadata={'source_type': source_type, 'company_ids': company_ids, 'is_active': credential.is_active},
    )
    await db.commit()
    payloads = await list_credential_payloads(db, portal_account_id)
    created_payload = next((item for item in payloads if item['id'] == credential.id), None)
    return {'success': True, 'data': created_payload}


@router.patch('/admin/yclients-credentials/{credential_id}', dependencies=[Depends(forbid_demo)])
async def admin_update_yclients_credentials(
    credential_id: int,
    body: YClientsCredentialUpdateRequest,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*CREDENTIAL_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    if body.source_type is not None:
        normalize_source_type(body.source_type)
    credential = await _load_credential(db, credential_id, actor, x_portal_account_id)
    if body.portal_account_id is not None and body.portal_account_id != credential.portal_account_id:
        if actor.role != 'platform_admin':
            raise HTTPException(status_code=403, detail='Cannot move credentials between tenants')
        await _validated_credential_account_id(db, actor, body.portal_account_id)
        if body.company_ids is None:
            raise HTTPException(status_code=400, detail='company_ids is required when moving credentials')
        await _validate_credential_companies(db, body.portal_account_id, body.company_ids)
        credential.portal_account_id = body.portal_account_id
    try:
        update_credential_secrets(
            credential,
            title=body.title,
            partner_token=body.partner_token,
            login=body.login,
            password=body.password,
            is_active=body.is_active,
        )
    except CredentialsConfigError as exc:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.update_failed',
            portal_account_id=credential.portal_account_id,
            target_id=credential.id,
            error_type=exc.__class__.__name__,
        )
        await db.commit()
        raise _credentials_config_error(exc) from exc

    if body.company_ids is not None:
        await _validate_credential_companies(db, credential.portal_account_id, body.company_ids)
        try:
            await set_credential_companies(db, credential.id, body.company_ids)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=credential.portal_account_id,
        action='yclients_credentials.updated',
        target_type='yclients_credential',
        target_id=credential.id,
        metadata={
            'company_ids': body.company_ids,
            'is_active': credential.is_active,
            'secrets_rotated': any([
                body.partner_token is not None,
                body.login is not None,
                body.password is not None,
            ]),
        },
    )
    await db.commit()
    return {'success': True, 'data': await list_credential_payloads(db, credential.portal_account_id)}


@router.delete('/admin/yclients-credentials/{credential_id}', dependencies=[Depends(forbid_demo)])
async def admin_delete_yclients_credentials(
    credential_id: int,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*CREDENTIAL_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    credential = await _load_credential(db, credential_id, actor, x_portal_account_id)
    portal_account_id = credential.portal_account_id
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=portal_account_id,
        action='yclients_credentials.deleted',
        target_type='yclients_credential',
        target_id=credential.id,
        metadata={'title': credential.title},
    )
    await db.delete(credential)
    await db.commit()
    return {'success': True, 'message': 'YClients credentials deleted'}


@router.post('/admin/yclients-credentials/{credential_id}/test', dependencies=[Depends(forbid_demo)])
async def admin_test_saved_yclients_credentials(
    credential_id: int,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*CREDENTIAL_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        credential = await _load_credential(db, credential_id, actor, x_portal_account_id)
    except HTTPException:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.tested',
            portal_account_id=_credential_failure_audit_tenant_id(actor),
            requested_portal_account_id=x_portal_account_id,
            target_id=credential_id,
            error_type='credential_lookup_failed',
        )
        await db.commit()
        raise
    try:
        value = decrypted_credential(credential)
    except Exception as exc:
        await mark_credential_failure_async(db, credential.id, CREDENTIAL_DECRYPT_FAILED_DETAIL)
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.tested',
            portal_account_id=credential.portal_account_id,
            target_id=credential.id,
            source_type=SOURCE_YCLIENTS,
            error_type=exc.__class__.__name__,
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=CREDENTIAL_DECRYPT_FAILED_DETAIL) from exc
    try:
        result = _test_source_credentials(SOURCE_YCLIENTS, value.partner_token, value.login, value.password)
    except HTTPException as exc:
        await mark_credential_failure_async(db, credential.id, CREDENTIAL_TEST_FAILED_DETAIL)
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.tested',
            portal_account_id=credential.portal_account_id,
            target_id=credential.id,
            source_type=SOURCE_YCLIENTS,
            error_type='HTTPException',
        )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
    except Exception as exc:
        await mark_credential_failure_async(db, credential.id, CREDENTIAL_TEST_FAILED_DETAIL)
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.tested',
            portal_account_id=credential.portal_account_id,
            target_id=credential.id,
            source_type=SOURCE_YCLIENTS,
            error_type=exc.__class__.__name__,
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
    await mark_credential_success_async(db, credential.id)
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=credential.portal_account_id,
        action='yclients_credentials.tested',
        target_type='yclients_credential',
        target_id=credential.id,
        metadata={'source_type': SOURCE_YCLIENTS, 'success': True},
    )
    await db.commit()
    return result


@router.post('/admin/yclients-credentials/test', dependencies=[Depends(forbid_demo)])
async def admin_test_yclients_credentials_payload(
    body: dict = Body(default_factory=dict),
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*CREDENTIAL_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        portal_account_id = await _validated_credential_account_id(db, actor, x_portal_account_id)
    except HTTPException:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.payload_tested',
            portal_account_id=_credential_failure_audit_tenant_id(actor),
            requested_portal_account_id=x_portal_account_id,
            error_type='tenant_selection_failed',
        )
        await db.commit()
        raise
    raw_source_type = body.get('source_type')
    if raw_source_type is not None and _credential_body_string(body, 'source_type') is None:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.payload_tested',
            portal_account_id=portal_account_id,
            source_type='invalid',
            reason='invalid_payload',
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=CREDENTIAL_PAYLOAD_INVALID_DETAIL)
    try:
        source_type = normalize_source_type(_credential_body_string(body, 'source_type') or SOURCE_YCLIENTS)
    except HTTPException as exc:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.payload_tested',
            portal_account_id=portal_account_id,
            source_type='invalid',
            error_type='unsupported_source_type',
        )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=CREDENTIAL_SOURCE_UNSUPPORTED_DETAIL) from exc
    partner_token = _credential_body_string(body, 'partner_token')
    login = _credential_body_string(body, 'login')
    password = _credential_body_string(body, 'password')
    if not all((partner_token, login, password)):
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.payload_tested',
            portal_account_id=portal_account_id,
            source_type=source_type,
            reason='invalid_payload',
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=CREDENTIAL_PAYLOAD_INVALID_DETAIL)
    try:
        result = _test_source_credentials(source_type, partner_token, login, password)
    except HTTPException:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.payload_tested',
            portal_account_id=portal_account_id,
            source_type=source_type,
            error_type='HTTPException',
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=CREDENTIAL_TEST_FAILED_DETAIL)
    except Exception as exc:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.payload_tested',
            portal_account_id=portal_account_id,
            source_type=source_type,
            error_type=exc.__class__.__name__,
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=portal_account_id,
        action='yclients_credentials.payload_tested',
        target_type='yclients_credential',
        metadata={'source_type': source_type, 'success': True},
    )
    await db.commit()
    return result
