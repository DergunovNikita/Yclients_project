"""Portal authentication and user administration routes."""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth_deps import forbid_demo, get_current_user, require_roles
from auth_hierarchy import (
    USER_ADMIN_ROLES,
    USER_MANAGER_ROLES,
    assignable_roles,
    assert_can_assign_role,
    assert_can_manage_staff,
    assert_can_manage_user,
    can_list_user,
    can_manage_staff,
    can_manage_user,
    validate_company_ids_for_role,
)
from auth_service import (
    TOKEN_PURPOSE_RESET,
    TOKEN_PURPOSE_VERIFY,
    consume_email_token,
    create_access_token,
    email_cooldown_active,
    hash_password,
    load_portal_account_branch_ids,
    load_user_access_branch_ids,
    normalize_email,
    send_password_reset_email,
    send_account_credentials_email,
    is_deliverable_portal_email,
    send_verification_email,
    set_user_branches,
    user_can_login,
    verify_password,
)
from auth_sessions import (
    REFRESH_COOKIE_NAME,
    bump_user_token_version,
    clear_auth_cookies,
    enforce_csrf,
    issue_session,
    list_user_sessions,
    revoke_refresh,
    revoke_session_by_id,
    revoke_user_sessions,
    rotate_session,
    set_auth_cookies,
)
from data_sources import SOURCE_YCLIENTS, adapter_from_payload, normalize_source_type
from database import get_async_db
from models import Company, PortalAccount, PortalBranch, PortalUser, Staff, YClientsCredential
from portal_audit import log_portal_audit
from portal_account_provision import provision_all_unlinked_staff, provision_staff_account
from portal_staff_sync import (
    deactivate_portal_user_staff,
    list_unlinked_staff,
    sync_all_portal_users_staff,
    sync_portal_user_staff,
)
from yclients_credentials import (
    CredentialsConfigError,
    decrypted_credential,
    list_credential_payloads,
    mark_credential_failure_async,
    mark_credential_success_async,
    new_credential,
    set_credential_companies,
    update_credential_secrets,
)
from config import (
    AUTH_PUBLIC_REGISTRATION_ENABLED,
    AUTH_RATE_LIMIT_IP_MAX_REQUESTS,
    AUTH_RATE_LIMIT_MAX_REQUESTS,
    AUTH_RATE_LIMIT_WINDOW_SECONDS,
)

router = APIRouter()
AUTH_RATE_LIMIT_ROUTES = {
    'login': '/auth/login',
    'register': '/auth/register',
    'forgot_password': '/auth/forgot-password',
    'reset_password': '/auth/reset-password',
    'resend_verification': '/auth/resend-verification',
}
CREDENTIAL_PAYLOAD_INVALID_DETAIL = 'Invalid credential request'
CREDENTIAL_TEST_FAILED_DETAIL = 'Data source authentication failed'
CREDENTIAL_STORAGE_FAILED_DETAIL = 'Credential storage is not configured'
CREDENTIAL_DECRYPT_FAILED_DETAIL = 'Stored credentials could not be decrypted'
CREDENTIAL_SOURCE_UNSUPPORTED_DETAIL = 'Unsupported credential source type'
CREDENTIAL_TEXT_LIMITS = {
    'source_type': 32,
    'partner_token': 4096,
    'login': 255,
    'password': 255,
}


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str


class TokenRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)


class ResetPasswordRequest(BaseModel):
    token: str = Field(min_length=10, max_length=256)
    password: str = Field(min_length=8, max_length=128)


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class AdminStaffCreateAccountRequest(BaseModel):
    email: EmailStr | None = None
    role: str = 'viewer'
    password: str | None = Field(default=None, min_length=8, max_length=128)


class DistributeCredentialsRequest(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)


class AdminUserUpdateRequest(BaseModel):
    email: EmailStr | None = None
    role: str | None = None
    is_active: bool | None = None
    full_name: str | None = None
    company_ids: list[int] | None = None


class AdminUserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)
    role: str
    portal_account_id: int | None = None
    company_ids: list[int] = Field(default_factory=list)


class AdminStaffUpdateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    company_id: int
    position: str | None = Field(default=None, max_length=255)


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


def _user_payload(
    user: PortalUser,
    branch_ids: list[int],
    manageable: bool | None = None,
    *,
    show_initial_password: bool = False,
    staff_id: int | None = None,
) -> dict:
    payload = {
        'id': user.id,
        'staff_id': staff_id,
        'email': user.email,
        'full_name': user.full_name,
        'role': user.role,
        'portal_account_id': user.portal_account_id,
        'is_active': user.is_active,
        'is_demo': bool(user.is_demo),
        'email_verified': user.email_verified_at is not None,
        'company_ids': branch_ids,
        'is_portal_user': True,
        'manageable': manageable,
        'password_changed': user.password_changed_at is not None,
    }
    if show_initial_password and user.initial_password:
        payload['initial_password'] = user.initial_password
    return payload


def _staff_payload(staff: Staff, manageable: bool = False) -> dict:
    email = normalize_email(staff.email or '') if staff.email else ''
    can_create_account = bool(email and is_deliverable_portal_email(email))
    return {
        'id': None,
        'staff_id': staff.id,
        'email': email or '—',
        'full_name': staff.name,
        'role': 'staff',
        'position': staff.position,
        'is_active': True,
        'email_verified': False,
        'company_ids': [staff.company_id],
        'is_portal_user': False,
        'manageable': manageable,
        'can_create_account': can_create_account,
    }


async def _load_manageable_staff(
    db: AsyncSession,
    staff_id: int,
    actor: PortalUser,
    actor_branch_ids: list[int],
) -> Staff:
    staff = (
        await db.execute(
            select(Staff).where(
                Staff.id == staff_id,
                Staff.portal_user_id.is_(None),
                Staff.fired == 0,
            )
        )
    ).scalar_one_or_none()
    if staff is None:
        raise HTTPException(status_code=404, detail='Staff member not found')
    if actor.role == 'platform_admin' and staff.company_id not in actor_branch_ids:
        raise HTTPException(status_code=404, detail='Staff member not found')
    assert_can_manage_staff(actor.role, actor_branch_ids, staff.company_id)
    return staff


@router.post('/register')
async def register(body: RegisterRequest, request: Request, db: AsyncSession = Depends(get_async_db)):
    if not AUTH_PUBLIC_REGISTRATION_ENABLED:
        raise HTTPException(status_code=403, detail='Public registration is disabled')

    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, AUTH_RATE_LIMIT_ROUTES['register'], email)
    existing = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail='Email already registered')

    user = PortalUser(
        portal_account_id=None,
        email=email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role='owner',
        is_active=True,
        email_verified_at=datetime.utcnow(),
        created_at=datetime.utcnow(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return {
        'success': True,
        'message': 'Регистрация успешна. Можно войти и подключить источник данных.',
    }


def _access_token_for(user: PortalUser) -> str:
    return create_access_token(user.id, user.role, user.token_version)


def _session_payload(user: PortalUser, branch_ids: list[int], csrf_token: str) -> dict:
    return {
        'csrf_token': csrf_token,
        'user': _user_payload(user, branch_ids),
    }


async def _finalize_session_response(db: AsyncSession, response: Response, user: PortalUser, session) -> dict:
    await db.commit()
    set_auth_cookies(response, session)
    branch_ids = await load_user_access_branch_ids(db, user)
    return {
        'success': True,
        'data': _session_payload(user, branch_ids, session.csrf_token),
    }


@router.post('/login')
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_db),
):
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, AUTH_RATE_LIMIT_ROUTES['login'], email)
    user = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail='Invalid email or password')
    if not user_can_login(user):
        raise HTTPException(status_code=403, detail='Account disabled')

    user.last_login_at = datetime.utcnow()
    session = await issue_session(db, user, request, access_token_fn=_access_token_for)
    return await _finalize_session_response(db, response, user, session)


# Best-effort in-memory rate limit for the unauthenticated demo login (per process,
# per client IP). Caps refresh-token churn from abuse without pulling in a dependency.
_DEMO_LOGIN_WINDOW_SECONDS = 60.0
_DEMO_LOGIN_MAX_PER_WINDOW = 10
_demo_login_hits: dict[str, list[float]] = {}
_auth_rate_limit_hits: dict[tuple[str, str, str, str], list[float]] = {}


def _prune_rate_limit_hits(now: float, window_seconds: float) -> None:
    for key, timestamps in list(_auth_rate_limit_hits.items()):
        recent = [ts for ts in timestamps if now - ts < window_seconds]
        if recent:
            _auth_rate_limit_hits[key] = recent
        else:
            _auth_rate_limit_hits.pop(key, None)


def _auth_rate_limit_client_ip(request: Request) -> str:
    return request.client.host if request.client else 'unknown'


def _check_rate_limit_bucket(
    key: tuple[str, str, str, str],
    max_requests: int,
    window_seconds: float,
    now: float,
) -> None:
    if max_requests <= 0:
        return
    recent = _auth_rate_limit_hits.get(key, [])
    if len(recent) >= max_requests:
        retry_after = max(1, int(window_seconds - (now - recent[0])))
        raise HTTPException(
            status_code=429,
            detail='Too many authentication attempts, try again shortly',
            headers={'Retry-After': str(retry_after)},
        )
    recent.append(now)
    _auth_rate_limit_hits[key] = recent


def enforce_auth_rate_limit(request: Request, route: str, email: str | None = None) -> None:
    max_email_requests = int(AUTH_RATE_LIMIT_MAX_REQUESTS)
    max_ip_requests = int(AUTH_RATE_LIMIT_IP_MAX_REQUESTS)
    window_seconds = float(AUTH_RATE_LIMIT_WINDOW_SECONDS)
    if (max_email_requests <= 0 and max_ip_requests <= 0) or window_seconds <= 0:
        return

    now = time.monotonic()
    _prune_rate_limit_hits(now, window_seconds)
    ip = _auth_rate_limit_client_ip(request)
    _check_rate_limit_bucket(('ip', route, ip, ''), max_ip_requests, window_seconds, now)
    if email:
        _check_rate_limit_bucket(
            ('email_ip', route, ip, normalize_email(email)),
            max_email_requests,
            window_seconds,
            now,
        )


def _demo_login_allowed(ip: str | None) -> bool:
    now = time.monotonic()
    key = ip or 'unknown'
    recent = [ts for ts in _demo_login_hits.get(key, ()) if now - ts < _DEMO_LOGIN_WINDOW_SECONDS]
    if len(recent) >= _DEMO_LOGIN_MAX_PER_WINDOW:
        _demo_login_hits[key] = recent
        return False
    recent.append(now)
    _demo_login_hits[key] = recent
    return True


@router.post('/demo-login')
async def demo_login(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_db),
):
    """Passwordless login into the shared read-only demo tenant.

    Issues a normal session for the seeded demo owner (``is_demo=True``). Returns
    503 when the demo tenant has not been provisioned (see scripts/seed_demo.py).
    """
    if not _demo_login_allowed(_auth_rate_limit_client_ip(request)):
        raise HTTPException(status_code=429, detail='Too many demo login attempts, try again shortly')

    user = (
        await db.execute(
            select(PortalUser)
            .where(
                PortalUser.is_demo.is_(True),
                PortalUser.role == 'owner',
                PortalUser.is_active.is_(True),
            )
            .order_by(PortalUser.id.asc())
            .limit(1)
        )
    ).scalars().first()
    if user is None:
        raise HTTPException(status_code=503, detail='Demo mode is not available')

    user.last_login_at = datetime.utcnow()
    session = await issue_session(db, user, request, access_token_fn=_access_token_for)
    return await _finalize_session_response(db, response, user, session)


@router.post('/refresh')
async def refresh(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_db),
):
    enforce_csrf(request, allow_bearer_skip=False)
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if not raw_refresh:
        raise HTTPException(status_code=401, detail='Missing refresh token')
    user, session = await rotate_session(db, raw_refresh, request, access_token_fn=_access_token_for)
    return await _finalize_session_response(db, response, user, session)


@router.post('/logout')
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_async_db),
):
    enforce_csrf(request, allow_bearer_skip=False)
    raw_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    if raw_refresh:
        await revoke_refresh(db, raw_refresh)
        await db.commit()
    clear_auth_cookies(response)
    return {'success': True}


@router.post('/logout-all', dependencies=[Depends(forbid_demo)])
async def logout_all(
    response: Response,
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    await revoke_user_sessions(db, user.id)
    bump_user_token_version(user)
    await db.commit()
    clear_auth_cookies(response)
    return {'success': True}


@router.post('/logout-others', dependencies=[Depends(forbid_demo)])
async def logout_others(
    request: Request,
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    current_refresh = request.cookies.get(REFRESH_COOKIE_NAME)
    await revoke_user_sessions(db, user.id, except_refresh=current_refresh)
    await db.commit()
    return {'success': True}


@router.get('/sessions')
async def list_sessions(
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    sessions = await list_user_sessions(db, user.id)
    return {
        'success': True,
        'data': [
            {
                'id': item.id,
                'device_label': item.device_label,
                'created_at': item.created_at.isoformat() if item.created_at else None,
                'last_used_at': item.last_used_at.isoformat() if item.last_used_at else None,
                'expires_at': item.expires_at.isoformat() if item.expires_at else None,
            }
            for item in sessions
        ],
    }


@router.delete('/sessions/{session_id}', dependencies=[Depends(forbid_demo)])
async def delete_session(
    session_id: int,
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    removed = await revoke_session_by_id(db, user.id, session_id)
    await db.commit()
    if not removed:
        raise HTTPException(status_code=404, detail='Session not found')
    return {'success': True}


@router.get('/me')
async def me(
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    branch_ids = await load_user_access_branch_ids(db, user)
    return {'success': True, 'data': _user_payload(user, branch_ids, manageable=None)}


@router.post('/verify-email')
async def verify_email(body: TokenRequest, db: AsyncSession = Depends(get_async_db)):
    user = await consume_email_token(db, body.token, TOKEN_PURPOSE_VERIFY)
    if user is None:
        raise HTTPException(status_code=400, detail='Invalid or expired token')
    user.email_verified_at = datetime.utcnow()
    await db.commit()
    return {'success': True, 'message': 'Email подтверждён. Теперь можно войти в кабинет.'}


@router.post('/forgot-password')
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, AUTH_RATE_LIMIT_ROUTES['forgot_password'], email)
    user = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if user is not None and user.is_active and not email_cooldown_active(user.password_reset_sent_at):
        await send_password_reset_email(db, user)
    return {
        'success': True,
        'message': 'Если аккаунт с таким email существует, на почту отправлена ссылка для сброса пароля.',
    }


@router.post('/resend-verification')
async def resend_verification(
    body: ResendVerificationRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    email = normalize_email(body.email)
    enforce_auth_rate_limit(request, AUTH_RATE_LIMIT_ROUTES['resend_verification'], email)
    user = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if user is not None and user.email_verified_at is None and not email_cooldown_active(user.email_verification_sent_at):
        await send_verification_email(db, user)
    return {
        'success': True,
        'message': 'Если аккаунт существует и не подтверждён — письмо отправлено повторно.',
    }


@router.post('/reset-password')
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    enforce_auth_rate_limit(request, AUTH_RATE_LIMIT_ROUTES['reset_password'])
    user = await consume_email_token(db, body.token, TOKEN_PURPOSE_RESET)
    if user is None:
        raise HTTPException(status_code=400, detail='Invalid or expired token')
    user.password_hash = hash_password(body.password)
    user.initial_password = None
    user.password_changed_at = datetime.utcnow()
    bump_user_token_version(user)
    await revoke_user_sessions(db, user.id)
    await db.commit()
    return {'success': True, 'message': 'Пароль обновлён. Теперь можно войти в кабинет.'}


@router.post('/change-password', dependencies=[Depends(forbid_demo)])
async def change_password(
    body: ChangePasswordRequest,
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    if not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail='Неверный текущий пароль')
    if body.current_password == body.new_password:
        raise HTTPException(status_code=400, detail='Новый пароль должен отличаться от текущего')

    user.password_hash = hash_password(body.new_password)
    user.initial_password = None
    user.password_changed_at = datetime.utcnow()
    bump_user_token_version(user)
    await revoke_user_sessions(db, user.id)
    await db.commit()
    return {'success': True, 'message': 'Пароль успешно изменён. Войдите снова.'}


async def _actor_branch_ids(db: AsyncSession, user: PortalUser) -> list[int]:
    return await load_user_access_branch_ids(db, user)


async def _active_admin_portal_account_id(
    db: AsyncSession,
    actor: PortalUser,
    x_portal_account_id: int | None,
) -> int | None:
    if actor.role != 'platform_admin':
        return actor.portal_account_id
    if x_portal_account_id is None:
        raise HTTPException(status_code=400, detail='X-Portal-Account-Id is required')
    account = await db.get(PortalAccount, x_portal_account_id)
    if account is None:
        raise HTTPException(status_code=400, detail='Unknown portal_account_id')
    return int(x_portal_account_id)


async def _active_admin_branch_ids(
    db: AsyncSession,
    actor: PortalUser,
    x_portal_account_id: int | None,
) -> list[int]:
    if actor.role == 'platform_admin':
        portal_account_id = await _active_admin_portal_account_id(db, actor, x_portal_account_id)
        return await load_portal_account_branch_ids(db, portal_account_id)
    return await _actor_branch_ids(db, actor)


def _same_tenant(actor: PortalUser, target: PortalUser) -> bool:
    if actor.role == 'platform_admin':
        return True
    return actor.portal_account_id is not None and actor.portal_account_id == target.portal_account_id


async def _validate_company_ids_in_scope(
    db: AsyncSession,
    actor: PortalUser,
    company_ids: list[int],
) -> None:
    await _validate_company_ids_exist(db, company_ids)
    if actor.role == 'platform_admin':
        return
    tenant_company_ids = set(await load_portal_account_branch_ids(db, actor.portal_account_id))
    invalid = sorted(set(company_ids) - tenant_company_ids)
    if invalid:
        raise HTTPException(status_code=403, detail=f'Companies outside tenant: {invalid}')


async def _validate_company_ids_exist(db: AsyncSession, company_ids: list[int]) -> None:
    if not company_ids:
        return
    existing = (await db.execute(select(Company.id).where(Company.id.in_(company_ids)))).scalars().all()
    missing = set(company_ids) - set(existing)
    if missing:
        raise HTTPException(status_code=400, detail=f'Unknown company ids: {sorted(missing)}')


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
    error_type: str,
    requested_portal_account_id: int | None = None,
) -> None:
    metadata = {'success': False, 'error_type': error_type}
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
    normalized = normalize_source_type(source_type)
    adapter = adapter_from_payload(
        normalized,
        partner_token=partner_token,
        login=login,
        password=password,
    )
    if not adapter.authenticate():
        raise HTTPException(status_code=400, detail='Data source authentication failed')
    return {'success': True, 'source_type': normalized, 'message': 'Data source credentials are valid'}


async def _sync_credential_companies_from_yclients(
    db: AsyncSession,
    portal_account_id: int,
    partner_token: str,
    login: str,
    password: str,
    source_type: str = SOURCE_YCLIENTS,
) -> list[int]:
    adapter = adapter_from_payload(
        source_type,
        partner_token=partner_token,
        login=login,
        password=password,
    )
    if not adapter.authenticate():
        raise HTTPException(status_code=400, detail='Data source authentication failed')
    company_ids = sorted(item.company_id for item in adapter.list_branches())
    materialized_company_ids = await adapter.materialize_branches(db, portal_account_id, company_ids)
    return sorted(set(materialized_company_ids))


@router.get('/admin/meta')
async def admin_meta(
    actor: PortalUser = Depends(require_roles(*USER_MANAGER_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    branch_ids = await _actor_branch_ids(db, actor)
    return {
        'success': True,
        'data': {
            'role': actor.role,
            'can_manage_users': actor.role in USER_ADMIN_ROLES,
            'assignable_roles': assignable_roles(actor.role) if actor.role in USER_ADMIN_ROLES else [],
            'portal_account_id': actor.portal_account_id,
            'company_ids': None if actor.role == 'platform_admin' else branch_ids,
        },
    }


@router.get('/portal-accounts')
@router.get('/admin/portal-accounts')
async def admin_list_portal_accounts(
    _actor: PortalUser = Depends(require_roles('platform_admin')),
    db: AsyncSession = Depends(get_async_db),
):
    rows = (
        await db.execute(
            select(
                PortalAccount.id,
                PortalAccount.label,
                PortalAccount.created_at,
                func.count(PortalBranch.company_id).label('branch_count'),
            )
            .outerjoin(PortalBranch, PortalBranch.portal_account_id == PortalAccount.id)
            .group_by(PortalAccount.id, PortalAccount.label, PortalAccount.created_at)
            .order_by(PortalAccount.id.asc())
        )
    ).all()
    return {
        'success': True,
        'data': [
            {
                'id': row.id,
                'label': row.label,
                'created_at': row.created_at.isoformat() if row.created_at else None,
                'branch_count': int(row.branch_count or 0),
            }
            for row in rows
        ],
    }


async def _load_staff_ids_by_portal_user(db: AsyncSession, portal_user_ids: list[int]) -> dict[int, int]:
    if not portal_user_ids:
        return {}
    rows = (
        await db.execute(
            select(Staff.id, Staff.portal_user_id).where(Staff.portal_user_id.in_(portal_user_ids))
        )
    ).all()
    mapping: dict[int, int] = {}
    for staff_id, portal_user_id in rows:
        mapping.setdefault(portal_user_id, staff_id)
    return mapping


@router.get('/admin/users')
async def admin_list_users(
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*USER_MANAGER_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    active_portal_account_id = await _active_admin_portal_account_id(db, actor, x_portal_account_id)
    actor_branch_ids = await _active_admin_branch_ids(db, actor, x_portal_account_id)
    users = (await db.execute(select(PortalUser).order_by(PortalUser.id.asc()))).scalars().all()
    await sync_all_portal_users_staff(db)
    await db.commit()

    payload = []
    show_passwords = actor.role in USER_ADMIN_ROLES
    staff_ids_by_user = await _load_staff_ids_by_portal_user(db, [user.id for user in users])
    for user in users:
        if actor.role == 'platform_admin' and user.portal_account_id != active_portal_account_id:
            continue
        if not _same_tenant(actor, user):
            continue
        branch_ids = await load_user_access_branch_ids(db, user)
        if can_list_user(actor.role, actor_branch_ids, user.role, branch_ids):
            manageable = user.id != actor.id and can_manage_user(
                actor.role, actor_branch_ids, user.role, branch_ids
            )
            payload.append(
                _user_payload(
                    user,
                    branch_ids,
                    manageable=manageable,
                    show_initial_password=show_passwords and can_list_user(
                        actor.role, actor_branch_ids, user.role, branch_ids
                    ),
                    staff_id=staff_ids_by_user.get(user.id),
                )
            )

    allowed_staff_companies = actor_branch_ids
    for staff in await list_unlinked_staff(db, allowed_staff_companies):
        manageable = can_manage_staff(actor.role, actor_branch_ids, staff.company_id)
        payload.append(_staff_payload(staff, manageable=manageable))
    return {'success': True, 'data': payload}


@router.post('/admin/users', dependencies=[Depends(forbid_demo)])
async def admin_create_user(
    body: AdminUserCreateRequest,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
    assert_can_assign_role(actor.role, body.role)
    validate_company_ids_for_role(actor.role, actor_branch_ids, body.role, body.company_ids)
    await _validate_company_ids_in_scope(db, actor, body.company_ids)

    portal_account_id = actor.portal_account_id
    if actor.role == 'platform_admin':
        if body.role == 'owner':
            if body.portal_account_id is not None:
                account = await db.get(PortalAccount, body.portal_account_id)
                if account is None:
                    raise HTTPException(status_code=400, detail='Unknown portal_account_id')
                portal_account_id = account.id
            else:
                account = PortalAccount(
                    label=(body.full_name or body.email).strip(),
                    created_at=datetime.utcnow(),
                )
                db.add(account)
                await db.flush()
                portal_account_id = account.id
        elif body.role == 'platform_admin':
            portal_account_id = None
        elif body.portal_account_id is not None:
            account = await db.get(PortalAccount, body.portal_account_id)
            if account is None:
                raise HTTPException(status_code=400, detail='Unknown portal_account_id')
            portal_account_id = account.id
        else:
            raise HTTPException(status_code=400, detail='portal_account_id is required for tenant users')

    if body.company_ids and portal_account_id is not None:
        tenant_company_ids = set(await load_portal_account_branch_ids(db, portal_account_id))
        invalid = sorted(set(body.company_ids) - tenant_company_ids)
        if invalid:
            raise HTTPException(status_code=403, detail=f'Companies outside tenant: {invalid}')

    email = normalize_email(body.email)
    existing = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail='Email already registered')

    user = PortalUser(
        portal_account_id=portal_account_id,
        email=email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role=body.role,
        is_active=True,
        email_verified_at=datetime.utcnow(),
        initial_password=body.password,
        created_at=datetime.utcnow(),
    )
    db.add(user)
    await db.flush()
    if body.company_ids:
        await set_user_branches(db, user.id, body.company_ids)
    branch_ids = body.company_ids or []
    await sync_portal_user_staff(db, user, branch_ids)
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=portal_account_id,
        action='portal_user.created',
        target_type='portal_user',
        target_id=user.id,
        metadata={'role': user.role, 'company_ids': branch_ids},
    )
    await db.commit()
    await db.refresh(user)
    branch_ids = await load_user_access_branch_ids(db, user)
    return {
        'success': True,
        'data': _user_payload(user, branch_ids, manageable=None, show_initial_password=True),
    }


@router.patch('/admin/users/{user_id}', dependencies=[Depends(forbid_demo)])
async def admin_update_user(
    user_id: int,
    body: AdminUserUpdateRequest,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    user = (await db.execute(select(PortalUser).where(PortalUser.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail='User not found')
    if user.id == actor.id:
        raise HTTPException(status_code=403, detail='Cannot manage your own account here')
    if not _same_tenant(actor, user):
        raise HTTPException(status_code=403, detail='Cannot manage user from another tenant')

    actor_branch_ids = await _actor_branch_ids(db, actor)
    current_branch_ids = await load_user_access_branch_ids(db, user)
    assert_can_manage_user(actor.role, actor_branch_ids, user.role, current_branch_ids)

    next_role = body.role if body.role is not None else user.role
    next_company_ids = body.company_ids if body.company_ids is not None else current_branch_ids

    if body.role is not None and body.role != user.role:
        assert_can_assign_role(actor.role, body.role)
    if body.role is not None or body.company_ids is not None:
        validate_company_ids_for_role(actor.role, actor_branch_ids, next_role, next_company_ids)

    if body.role is not None:
        user.role = body.role
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.email is not None:
        email = normalize_email(body.email)
        if not email:
            raise HTTPException(status_code=400, detail='Email must not be empty')
        if email != user.email:
            existing = (
                await db.execute(select(PortalUser).where(PortalUser.email == email))
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(status_code=409, detail='Email already registered')
            user.email = email
            user.email_verified_at = datetime.utcnow()

    if user.role == 'platform_admin':
        user.portal_account_id = None
        await set_user_branches(db, user.id, [])
    elif body.company_ids is not None:
        await _validate_company_ids_in_scope(db, actor, body.company_ids)
        await set_user_branches(db, user.id, body.company_ids)

    branch_ids = await load_user_access_branch_ids(db, user)
    await sync_portal_user_staff(db, user, branch_ids)
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=user.portal_account_id,
        action='portal_user.updated',
        target_type='portal_user',
        target_id=user.id,
        metadata={
            'role': user.role,
            'is_active': user.is_active,
            'company_ids': branch_ids,
        },
    )
    await db.commit()
    return {'success': True, 'data': _user_payload(user, branch_ids, manageable=None)}


@router.get('/admin/yclients-credentials')
async def admin_list_yclients_credentials(
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles('platform_admin', 'owner')),
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
    actor: PortalUser = Depends(require_roles('platform_admin', 'owner')),
    db: AsyncSession = Depends(get_async_db),
):
    source_type = normalize_source_type(body.source_type)
    if actor.role == 'platform_admin' and body.portal_account_id is not None and x_portal_account_id is not None:
        if body.portal_account_id != x_portal_account_id:
            raise HTTPException(status_code=400, detail='portal_account_id does not match X-Portal-Account-Id')
    portal_account_id = await _validated_credential_account_id(db, actor, x_portal_account_id or body.portal_account_id)
    try:
        _test_source_credentials(source_type, body.partner_token, body.login, body.password)
    except HTTPException as exc:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.create_failed',
            portal_account_id=portal_account_id,
            error_type='HTTPException',
        )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
    except Exception as exc:
        await _audit_credential_action_failure(
            db,
            actor=actor,
            action='yclients_credentials.create_failed',
            portal_account_id=portal_account_id,
            error_type=exc.__class__.__name__,
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
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
        except HTTPException as exc:
            await _audit_credential_action_failure(
                db,
                actor=actor,
                action='yclients_credentials.create_failed',
                portal_account_id=portal_account_id,
                error_type='HTTPException',
            )
            await db.commit()
            raise HTTPException(status_code=exc.status_code, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
        except Exception as exc:
            await _audit_credential_action_failure(
                db,
                actor=actor,
                action='yclients_credentials.create_failed',
                portal_account_id=portal_account_id,
                error_type=exc.__class__.__name__,
            )
            await db.commit()
            raise HTTPException(status_code=500, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
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
    actor: PortalUser = Depends(require_roles('platform_admin', 'owner')),
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
    actor: PortalUser = Depends(require_roles('platform_admin', 'owner')),
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
    actor: PortalUser = Depends(require_roles('platform_admin', 'owner')),
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
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=credential.portal_account_id,
            action='yclients_credentials.tested',
            target_type='yclients_credential',
            target_id=credential.id,
            metadata={'source_type': SOURCE_YCLIENTS, 'success': False, 'error_type': exc.__class__.__name__},
        )
        await db.commit()
        raise HTTPException(status_code=500, detail=CREDENTIAL_DECRYPT_FAILED_DETAIL) from exc
    try:
        result = _test_source_credentials(SOURCE_YCLIENTS, value.partner_token, value.login, value.password)
    except HTTPException as exc:
        await mark_credential_failure_async(db, credential.id, CREDENTIAL_TEST_FAILED_DETAIL)
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=credential.portal_account_id,
            action='yclients_credentials.tested',
            target_type='yclients_credential',
            target_id=credential.id,
            metadata={'source_type': SOURCE_YCLIENTS, 'success': False, 'error_type': 'HTTPException'},
        )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=CREDENTIAL_TEST_FAILED_DETAIL) from exc
    except Exception as exc:
        await mark_credential_failure_async(db, credential.id, CREDENTIAL_TEST_FAILED_DETAIL)
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=credential.portal_account_id,
            action='yclients_credentials.tested',
            target_type='yclients_credential',
            target_id=credential.id,
            metadata={'source_type': SOURCE_YCLIENTS, 'success': False, 'error_type': exc.__class__.__name__},
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
    actor: PortalUser = Depends(require_roles('platform_admin', 'owner')),
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
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=portal_account_id,
            action='yclients_credentials.payload_tested',
            target_type='yclients_credential',
            metadata={'source_type': 'invalid', 'success': False, 'reason': 'invalid_payload'},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=CREDENTIAL_PAYLOAD_INVALID_DETAIL)
    try:
        source_type = normalize_source_type(_credential_body_string(body, 'source_type') or SOURCE_YCLIENTS)
    except HTTPException as exc:
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=portal_account_id,
            action='yclients_credentials.payload_tested',
            target_type='yclients_credential',
            metadata={'source_type': 'invalid', 'success': False, 'error_type': 'unsupported_source_type'},
        )
        await db.commit()
        raise HTTPException(status_code=exc.status_code, detail=CREDENTIAL_SOURCE_UNSUPPORTED_DETAIL) from exc
    partner_token = _credential_body_string(body, 'partner_token')
    login = _credential_body_string(body, 'login')
    password = _credential_body_string(body, 'password')
    if not all((partner_token, login, password)):
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=portal_account_id,
            action='yclients_credentials.payload_tested',
            target_type='yclients_credential',
            metadata={'source_type': source_type, 'success': False, 'reason': 'invalid_payload'},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=CREDENTIAL_PAYLOAD_INVALID_DETAIL)
    try:
        result = _test_source_credentials(source_type, partner_token, login, password)
    except HTTPException:
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=portal_account_id,
            action='yclients_credentials.payload_tested',
            target_type='yclients_credential',
            metadata={'source_type': source_type, 'success': False, 'error_type': 'HTTPException'},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail=CREDENTIAL_TEST_FAILED_DETAIL)
    except Exception as exc:
        await log_portal_audit(
            db,
            actor_user_id=actor.id,
            portal_account_id=portal_account_id,
            action='yclients_credentials.payload_tested',
            target_type='yclients_credential',
            metadata={'source_type': source_type, 'success': False, 'error_type': exc.__class__.__name__},
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


@router.delete('/admin/users/{user_id}', dependencies=[Depends(forbid_demo)])
async def admin_delete_user(
    user_id: int,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    user = (await db.execute(select(PortalUser).where(PortalUser.id == user_id))).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail='User not found')
    if user.id == actor.id:
        raise HTTPException(status_code=403, detail='Cannot delete your own account')

    actor_branch_ids = await _actor_branch_ids(db, actor)
    if not _same_tenant(actor, user):
        raise HTTPException(status_code=403, detail='Cannot delete user from another tenant')
    branch_ids = await load_user_access_branch_ids(db, user)
    assert_can_manage_user(actor.role, actor_branch_ids, user.role, branch_ids)

    await deactivate_portal_user_staff(db, user.id)
    await log_portal_audit(
        db,
        actor_user_id=actor.id,
        portal_account_id=user.portal_account_id,
        action='portal_user.deleted',
        target_type='portal_user',
        target_id=user.id,
        metadata={'role': user.role, 'company_ids': branch_ids},
    )
    await db.delete(user)
    await db.commit()
    return {'success': True, 'message': 'User deleted'}


@router.patch('/admin/staff/{staff_id}', dependencies=[Depends(forbid_demo)])
async def admin_update_staff(
    staff_id: int,
    body: AdminStaffUpdateRequest,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _active_admin_branch_ids(db, actor, x_portal_account_id)
    staff = await _load_manageable_staff(db, staff_id, actor, actor_branch_ids)

    if body.company_id != staff.company_id:
        assert_can_manage_staff(actor.role, actor_branch_ids, body.company_id)

    await _validate_company_ids_exist(db, [body.company_id])

    staff.name = body.full_name.strip()
    staff.company_id = body.company_id
    staff.position = body.position.strip() if body.position else None
    await db.commit()
    return {
        'success': True,
        'data': _staff_payload(staff, manageable=True),
    }


@router.delete('/admin/staff/{staff_id}', dependencies=[Depends(forbid_demo)])
async def admin_delete_staff(
    staff_id: int,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _active_admin_branch_ids(db, actor, x_portal_account_id)
    staff = await _load_manageable_staff(db, staff_id, actor, actor_branch_ids)
    staff.fired = 1
    await db.commit()
    return {'success': True, 'message': 'Staff member removed'}


def _provisioned_payload(account) -> dict:
    return {
        'staff_id': account.staff_id,
        'user_id': account.user_id,
        'email': account.email,
        'full_name': account.full_name,
        'initial_password': account.initial_password,
        'company_id': account.company_id,
        'role': account.role,
    }


@router.post('/admin/provision-accounts', dependencies=[Depends(forbid_demo)])
async def admin_provision_accounts(
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _active_admin_branch_ids(db, actor, x_portal_account_id)
    allowed = actor_branch_ids
    created, errors = await provision_all_unlinked_staff(db, allowed)
    await db.commit()
    return {
        'success': True,
        'data': {
            'created': [_provisioned_payload(item) for item in created],
            'errors': errors,
            'created_count': len(created),
        },
    }


@router.post('/admin/staff/{staff_id}/create-account', dependencies=[Depends(forbid_demo)])
async def admin_create_staff_account(
    staff_id: int,
    body: AdminStaffCreateAccountRequest,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _active_admin_branch_ids(db, actor, x_portal_account_id)
    staff = await _load_manageable_staff(db, staff_id, actor, actor_branch_ids)
    assert_can_assign_role(actor.role, body.role)
    validate_company_ids_for_role(actor.role, actor_branch_ids, body.role, [staff.company_id])

    try:
        account = await provision_staff_account(
            db,
            staff,
            email=body.email,
            role=body.role,
            password=body.password,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    await db.commit()
    return {'success': True, 'data': _provisioned_payload(account)}


@router.get('/admin/initial-passwords')
async def admin_list_initial_passwords(
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    active_portal_account_id = await _active_admin_portal_account_id(db, actor, x_portal_account_id)
    actor_branch_ids = await _active_admin_branch_ids(db, actor, x_portal_account_id)
    users = (
        await db.execute(
            select(PortalUser)
            .where(PortalUser.initial_password.is_not(None))
            .order_by(PortalUser.id.asc())
        )
    ).scalars().all()

    payload = []
    staff_ids_by_user = await _load_staff_ids_by_portal_user(db, [user.id for user in users])
    for user in users:
        if actor.role == 'platform_admin' and user.portal_account_id != active_portal_account_id:
            continue
        if not _same_tenant(actor, user):
            continue
        branch_ids = await load_user_access_branch_ids(db, user)
        if not can_list_user(actor.role, actor_branch_ids, user.role, branch_ids):
            continue
        staff_id = staff_ids_by_user.get(user.id, user.id)
        payload.append({
            'user_id': user.id,
            'staff_id': staff_id,
            'email': user.email,
            'full_name': user.full_name,
            'role': user.role,
            'company_ids': branch_ids,
            'initial_password': user.initial_password,
        })
    return {'success': True, 'data': payload}


@router.post('/admin/distribute-credentials', dependencies=[Depends(forbid_demo)])
async def admin_distribute_credentials(
    body: DistributeCredentialsRequest,
    x_portal_account_id: int | None = Header(default=None),
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    active_portal_account_id = await _active_admin_portal_account_id(db, actor, x_portal_account_id)
    actor_branch_ids = await _active_admin_branch_ids(db, actor, x_portal_account_id)
    unique_ids = sorted(set(body.user_ids))
    users = (
        await db.execute(select(PortalUser).where(PortalUser.id.in_(unique_ids)))
    ).scalars().all()
    users_by_id = {user.id: user for user in users}

    sent: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    for user_id in unique_ids:
        user = users_by_id.get(user_id)
        if user is None:
            errors.append({'user_id': user_id, 'reason': 'User not found'})
            continue

        if actor.role == 'platform_admin' and user.portal_account_id != active_portal_account_id:
            errors.append({'user_id': user_id, 'email': user.email, 'reason': 'Access denied'})
            continue
        if not _same_tenant(actor, user):
            errors.append({'user_id': user_id, 'email': user.email, 'reason': 'Access denied'})
            continue
        branch_ids = await load_user_access_branch_ids(db, user)
        if not can_manage_user(actor.role, actor_branch_ids, user.role, branch_ids):
            errors.append({'user_id': user_id, 'email': user.email, 'reason': 'Access denied'})
            continue
        if not user.initial_password:
            skipped.append({'user_id': user_id, 'email': user.email, 'reason': 'No initial password stored'})
            continue
        if not is_deliverable_portal_email(user.email):
            skipped.append({
                'user_id': user_id,
                'email': user.email,
                'reason': 'Synthetic login address (@portal.local) — specify a real email',
            })
            continue

        try:
            send_account_credentials_email(user, user.initial_password)
        except Exception as exc:
            errors.append({'user_id': user_id, 'email': user.email, 'reason': str(exc)})
            continue

        sent.append({'user_id': user.id, 'email': user.email})

    return {
        'success': True,
        'data': {
            'sent': sent,
            'skipped': skipped,
            'errors': errors,
            'sent_count': len(sent),
        },
    }
