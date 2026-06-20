"""Portal authentication and user administration routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth_deps import get_current_user, require_roles
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
    hash_password,
    load_user_branch_ids,
    normalize_email,
    send_password_reset_email,
    send_account_credentials_email,
    is_deliverable_portal_email,
    send_verification_email,
    set_user_branches,
    user_can_login,
    verify_password,
)
from database import get_async_db
from models import Company, PortalUser, Staff
from portal_account_provision import provision_all_unlinked_staff, provision_staff_account
from portal_staff_sync import (
    deactivate_portal_user_staff,
    list_unlinked_staff,
    portal_user_syncs_to_staff,
    sync_all_portal_users_staff,
    sync_portal_user_staff,
)

router = APIRouter()


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


class AdminStaffCreateAccountRequest(BaseModel):
    email: EmailStr | None = None
    role: str = 'viewer'
    password: str | None = Field(default=None, min_length=8, max_length=128)


class DistributeCredentialsRequest(BaseModel):
    user_ids: list[int] = Field(min_length=1, max_length=500)


class AdminUserUpdateRequest(BaseModel):
    role: str | None = None
    is_active: bool | None = None
    full_name: str | None = None
    company_ids: list[int] | None = None


class AdminUserCreateRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str | None = Field(default=None, max_length=255)
    role: str
    company_ids: list[int] = Field(default_factory=list)


class AdminStaffUpdateRequest(BaseModel):
    full_name: str = Field(min_length=1, max_length=255)
    company_id: int
    position: str | None = Field(default=None, max_length=255)


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
        'is_active': user.is_active,
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
    return {
        'id': None,
        'staff_id': staff.id,
        'email': '—',
        'full_name': staff.name,
        'role': 'staff',
        'position': staff.position,
        'is_active': True,
        'email_verified': False,
        'company_ids': [staff.company_id],
        'is_portal_user': False,
        'manageable': manageable,
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
    assert_can_manage_staff(actor.role, actor_branch_ids, staff.company_id)
    return staff


@router.post('/register')
async def register(body: RegisterRequest, db: AsyncSession = Depends(get_async_db)):
    email = normalize_email(body.email)
    existing = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail='Email already registered')

    user = PortalUser(
        email=email,
        password_hash=hash_password(body.password),
        full_name=body.full_name,
        role='viewer',
        is_active=True,
        created_at=datetime.utcnow(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await send_verification_email(db, user)
    return {
        'success': True,
        'message': 'Регистрация успешна. Проверьте почту и перейдите по ссылке для подтверждения аккаунта.',
    }


@router.post('/login')
async def login(body: LoginRequest, db: AsyncSession = Depends(get_async_db)):
    email = normalize_email(body.email)
    user = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if user is None or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail='Invalid email or password')
    if not user_can_login(user):
        if user.email_verified_at is None:
            raise HTTPException(status_code=403, detail='Email not verified')
        raise HTTPException(status_code=403, detail='Account disabled')

    user.last_login_at = datetime.utcnow()
    await db.commit()
    branch_ids = await load_user_branch_ids(db, user.id)
    token = create_access_token(user.id, user.role)
    return {
        'success': True,
        'data': {
            'access_token': token,
            'token_type': 'bearer',
            'user': _user_payload(user, branch_ids),
        },
    }


@router.get('/me')
async def me(
    user: PortalUser = Depends(get_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    branch_ids = await load_user_branch_ids(db, user.id)
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
async def forgot_password(body: ForgotPasswordRequest, db: AsyncSession = Depends(get_async_db)):
    email = normalize_email(body.email)
    user = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if user is not None and user.is_active:
        await send_password_reset_email(db, user)
    return {
        'success': True,
        'message': 'Если аккаунт с таким email существует, на почту отправлена ссылка для сброса пароля.',
    }


@router.post('/reset-password')
async def reset_password(body: ResetPasswordRequest, db: AsyncSession = Depends(get_async_db)):
    user = await consume_email_token(db, body.token, TOKEN_PURPOSE_RESET)
    if user is None:
        raise HTTPException(status_code=400, detail='Invalid or expired token')
    user.password_hash = hash_password(body.password)
    user.initial_password = None
    user.password_changed_at = datetime.utcnow()
    await db.commit()
    return {'success': True, 'message': 'Пароль обновлён. Теперь можно войти в кабинет.'}


@router.post('/change-password')
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
    await db.commit()
    return {'success': True, 'message': 'Пароль успешно изменён.'}


async def _actor_branch_ids(db: AsyncSession, user: PortalUser) -> list[int]:
    if user.role == 'super_admin':
        return []
    return await load_user_branch_ids(db, user.id)


async def _validate_company_ids_exist(db: AsyncSession, company_ids: list[int]) -> None:
    if not company_ids:
        return
    existing = (await db.execute(select(Company.id).where(Company.id.in_(company_ids)))).scalars().all()
    missing = set(company_ids) - set(existing)
    if missing:
        raise HTTPException(status_code=400, detail=f'Unknown company ids: {sorted(missing)}')


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
            'company_ids': None if actor.role == 'super_admin' else branch_ids,
        },
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
    actor: PortalUser = Depends(require_roles(*USER_MANAGER_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
    users = (await db.execute(select(PortalUser).order_by(PortalUser.id.asc()))).scalars().all()
    await sync_all_portal_users_staff(db)
    await db.commit()

    payload = []
    show_passwords = actor.role in USER_ADMIN_ROLES
    staff_ids_by_user = await _load_staff_ids_by_portal_user(db, [user.id for user in users])
    for user in users:
        branch_ids = await load_user_branch_ids(db, user.id)
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

    allowed_staff_companies = None if actor.role == 'super_admin' else actor_branch_ids
    for staff in await list_unlinked_staff(db, allowed_staff_companies):
        manageable = can_manage_staff(actor.role, actor_branch_ids, staff.company_id)
        payload.append(_staff_payload(staff, manageable=manageable))
    return {'success': True, 'data': payload}


@router.post('/admin/users')
async def admin_create_user(
    body: AdminUserCreateRequest,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
    assert_can_assign_role(actor.role, body.role)
    validate_company_ids_for_role(actor.role, actor_branch_ids, body.role, body.company_ids)
    await _validate_company_ids_exist(db, body.company_ids)

    email = normalize_email(body.email)
    existing = (await db.execute(select(PortalUser).where(PortalUser.email == email))).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status_code=409, detail='Email already registered')

    user = PortalUser(
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
    await db.commit()
    await db.refresh(user)
    branch_ids = await load_user_branch_ids(db, user.id)
    return {
        'success': True,
        'data': _user_payload(user, branch_ids, manageable=None, show_initial_password=True),
    }


@router.patch('/admin/users/{user_id}')
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

    actor_branch_ids = await _actor_branch_ids(db, actor)
    current_branch_ids = await load_user_branch_ids(db, user.id)
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

    if body.company_ids is not None:
        await _validate_company_ids_exist(db, body.company_ids)
        await set_user_branches(db, user.id, body.company_ids)

    branch_ids = await load_user_branch_ids(db, user.id)
    await sync_portal_user_staff(db, user, branch_ids)
    await db.commit()
    return {'success': True, 'data': _user_payload(user, branch_ids, manageable=None)}


@router.delete('/admin/users/{user_id}')
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
    branch_ids = await load_user_branch_ids(db, user.id)
    assert_can_manage_user(actor.role, actor_branch_ids, user.role, branch_ids)

    await deactivate_portal_user_staff(db, user.id)
    await db.delete(user)
    await db.commit()
    return {'success': True, 'message': 'User deleted'}


@router.patch('/admin/staff/{staff_id}')
async def admin_update_staff(
    staff_id: int,
    body: AdminStaffUpdateRequest,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
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


@router.delete('/admin/staff/{staff_id}')
async def admin_delete_staff(
    staff_id: int,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
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


@router.post('/admin/provision-accounts')
async def admin_provision_accounts(
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
    allowed = None if actor.role == 'super_admin' else actor_branch_ids
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


@router.post('/admin/staff/{staff_id}/create-account')
async def admin_create_staff_account(
    staff_id: int,
    body: AdminStaffCreateAccountRequest,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
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
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
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
        branch_ids = await load_user_branch_ids(db, user.id)
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


@router.post('/admin/distribute-credentials')
async def admin_distribute_credentials(
    body: DistributeCredentialsRequest,
    actor: PortalUser = Depends(require_roles(*USER_ADMIN_ROLES)),
    db: AsyncSession = Depends(get_async_db),
):
    actor_branch_ids = await _actor_branch_ids(db, actor)
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

        branch_ids = await load_user_branch_ids(db, user.id)
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
