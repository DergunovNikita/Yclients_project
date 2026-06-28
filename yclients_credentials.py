"""Encrypted YClients credentials storage and lookup helpers."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from cryptography.fernet import Fernet
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from config import PORTAL_CREDENTIALS_ENCRYPTION_KEY, PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD
from models import Company, YClientsCredential, YClientsCredentialCompany


class CredentialsConfigError(RuntimeError):
    """Raised when credentials encryption is not configured."""


@dataclass(frozen=True)
class YClientsCredentialValue:
    id: int | None
    title: str
    partner_token: str
    login: str
    password: str
    company_ids: tuple[int, ...] = ()
    portal_account_id: int | None = None


def _fernet_from_key(raw_key: str) -> Fernet:
    if not raw_key:
        raise CredentialsConfigError('PORTAL_CREDENTIALS_ENCRYPTION_KEY is not configured')
    try:
        return Fernet(raw_key.encode('utf-8'))
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode('utf-8')).digest())
        return Fernet(derived)


def _fernet() -> Fernet:
    return _fernet_from_key(
        os.getenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', PORTAL_CREDENTIALS_ENCRYPTION_KEY).strip()
    )


def _old_fernet() -> Fernet | None:
    raw_key = os.getenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD).strip()
    if not raw_key:
        return None
    return _fernet_from_key(raw_key)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode('utf-8')).decode('ascii')


def _decrypt_secret_with_source(value: str) -> tuple[str, bool]:
    encrypted = value.encode('ascii')
    try:
        return _fernet().decrypt(encrypted).decode('utf-8'), False
    except Exception:
        old = _old_fernet()
        if old is None:
            raise
        return old.decrypt(encrypted).decode('utf-8'), True


def decrypt_secret(value: str) -> str:
    decrypted, _needs_reencrypt = _decrypt_secret_with_source(value)
    return decrypted


def credential_fingerprint(partner_token: str, login: str) -> str:
    raw = f'{partner_token.strip()}\0{login.strip()}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def credential_payload(
    credential: YClientsCredential,
    company_ids: Iterable[int],
) -> dict:
    return {
        'id': credential.id,
        'portal_account_id': credential.portal_account_id,
        'title': credential.title,
        'is_active': bool(credential.is_active),
        'company_ids': sorted(int(item) for item in company_ids),
        'has_partner_token': bool(credential.partner_token_encrypted),
        'has_login': bool(credential.login_encrypted),
        'has_password': bool(credential.password_encrypted),
        'last_used_at': credential.last_used_at.isoformat() if credential.last_used_at else None,
        'last_error_at': credential.last_error_at.isoformat() if credential.last_error_at else None,
        'last_error': credential.last_error,
        'needs_reauth': bool(credential.needs_reauth),
        'created_at': credential.created_at.isoformat() if credential.created_at else None,
        'updated_at': credential.updated_at.isoformat() if credential.updated_at else None,
    }


def decrypted_credential(
    credential: YClientsCredential,
    company_ids: Iterable[int] = (),
) -> YClientsCredentialValue:
    partner_token, partner_old = _decrypt_secret_with_source(credential.partner_token_encrypted)
    login, login_old = _decrypt_secret_with_source(credential.login_encrypted)
    password, password_old = _decrypt_secret_with_source(credential.password_encrypted)
    if partner_old or login_old or password_old:
        credential.partner_token_encrypted = encrypt_secret(partner_token)
        credential.login_encrypted = encrypt_secret(login)
        credential.password_encrypted = encrypt_secret(password)
        credential.updated_at = datetime.utcnow()
    fingerprint = credential_fingerprint(partner_token, login)
    if credential.credential_fingerprint != fingerprint:
        credential.credential_fingerprint = fingerprint
    return YClientsCredentialValue(
        id=credential.id,
        title=credential.title,
        partner_token=partner_token,
        login=login,
        password=password,
        company_ids=tuple(sorted(int(item) for item in company_ids)),
        portal_account_id=credential.portal_account_id,
    )


def load_active_credentials_sync(
    db: Session,
    portal_account_id: int | None = None,
) -> list[YClientsCredentialValue]:
    """Return decrypted active credentials with their assigned company ids."""
    stmt = select(YClientsCredential).where(YClientsCredential.is_active.is_(True))
    if portal_account_id is not None:
        stmt = stmt.where(YClientsCredential.portal_account_id == portal_account_id)
    credentials = db.execute(stmt.order_by(YClientsCredential.id.asc())).scalars().all()
    if not credentials:
        return []

    credential_ids = [credential.id for credential in credentials]
    rows = db.execute(
        select(
            YClientsCredentialCompany.credential_id,
            YClientsCredentialCompany.company_id,
        ).where(YClientsCredentialCompany.credential_id.in_(credential_ids))
    ).all()
    companies_by_credential: dict[int, list[int]] = {}
    for credential_id, company_id in rows:
        companies_by_credential.setdefault(int(credential_id), []).append(int(company_id))
    values: list[YClientsCredentialValue] = []
    for credential in credentials:
        try:
            values.append(decrypted_credential(credential, companies_by_credential.get(int(credential.id), [])))
        except Exception as exc:
            credential.needs_reauth = True
            credential.last_error_at = datetime.utcnow()
            credential.last_error = f'Decrypt failed: {exc.__class__.__name__}'
            credential.updated_at = datetime.utcnow()
    db.commit()
    return values


async def load_credential_company_map_async(db: AsyncSession) -> dict[int, int]:
    rows = (
        await db.execute(
            select(
                YClientsCredentialCompany.company_id,
                YClientsCredentialCompany.credential_id,
            )
        )
    ).all()
    return {int(company_id): int(credential_id) for company_id, credential_id in rows}


async def load_credentials_for_companies_async(
    db: AsyncSession,
    company_ids: Iterable[int],
) -> dict[int, YClientsCredentialValue]:
    normalized_company_ids = [int(item) for item in dict.fromkeys(company_ids)]
    if not normalized_company_ids:
        return {}

    assignments = (
        await db.execute(
            select(
                YClientsCredentialCompany.company_id,
                YClientsCredentialCompany.credential_id,
                YClientsCredential,
            )
            .join(YClientsCredential, YClientsCredential.id == YClientsCredentialCompany.credential_id)
            .where(
                YClientsCredentialCompany.company_id.in_(normalized_company_ids),
                YClientsCredential.is_active.is_(True),
            )
        )
    ).all()

    result: dict[int, YClientsCredentialValue] = {}
    for company_id, _credential_id, credential in assignments:
        try:
            result[int(company_id)] = decrypted_credential(credential, [int(company_id)])
        except Exception as exc:
            credential.needs_reauth = True
            credential.last_error_at = datetime.utcnow()
            credential.last_error = f'Decrypt failed: {exc.__class__.__name__}'
            credential.updated_at = datetime.utcnow()
    return result


def load_credentials_for_companies_sync(
    db: Session,
    company_ids: Iterable[int],
) -> dict[int, YClientsCredentialValue]:
    normalized_company_ids = [int(item) for item in dict.fromkeys(company_ids)]
    if not normalized_company_ids:
        return {}

    assignments = (
        db.execute(
            select(
                YClientsCredentialCompany.company_id,
                YClientsCredentialCompany.credential_id,
                YClientsCredential,
            )
            .join(YClientsCredential, YClientsCredential.id == YClientsCredentialCompany.credential_id)
            .where(
                YClientsCredentialCompany.company_id.in_(normalized_company_ids),
                YClientsCredential.is_active.is_(True),
            )
        )
        .all()
    )

    result: dict[int, YClientsCredentialValue] = {}
    for company_id, _credential_id, credential in assignments:
        try:
            result[int(company_id)] = decrypted_credential(credential, [int(company_id)])
        except Exception as exc:
            credential.needs_reauth = True
            credential.last_error_at = datetime.utcnow()
            credential.last_error = f'Decrypt failed: {exc.__class__.__name__}'
            credential.updated_at = datetime.utcnow()
    db.commit()
    return result


async def set_credential_companies(
    db: AsyncSession,
    credential_id: int,
    company_ids: list[int],
) -> None:
    if company_ids:
        existing = (await db.execute(select(Company.id).where(Company.id.in_(company_ids)))).scalars().all()
        missing = set(company_ids) - set(existing)
        if missing:
            raise ValueError(f'Unknown company ids: {sorted(missing)}')

    await db.execute(
        delete(YClientsCredentialCompany).where(
            YClientsCredentialCompany.credential_id == credential_id
        )
    )
    if company_ids:
        await db.execute(
            delete(YClientsCredentialCompany).where(
                YClientsCredentialCompany.company_id.in_(company_ids)
            )
        )
    for company_id in sorted(set(company_ids)):
        db.add(YClientsCredentialCompany(credential_id=credential_id, company_id=int(company_id)))


async def list_credential_payloads(
    db: AsyncSession,
    portal_account_id: int | None = None,
) -> list[dict]:
    stmt = select(YClientsCredential)
    if portal_account_id is not None:
        stmt = stmt.where(YClientsCredential.portal_account_id == portal_account_id)
    credentials = (await db.execute(stmt.order_by(YClientsCredential.id.asc()))).scalars().all()
    assignments = (
        await db.execute(
            select(
                YClientsCredentialCompany.credential_id,
                YClientsCredentialCompany.company_id,
            )
        )
    ).all()
    companies_by_credential: dict[int, list[int]] = {}
    for credential_id, company_id in assignments:
        companies_by_credential.setdefault(int(credential_id), []).append(int(company_id))
    return [
        credential_payload(credential, companies_by_credential.get(int(credential.id), []))
        for credential in credentials
    ]


def new_credential(
    portal_account_id: int,
    title: str,
    partner_token: str,
    login: str,
    password: str,
    is_active: bool = True,
) -> YClientsCredential:
    now = datetime.utcnow()
    return YClientsCredential(
        portal_account_id=portal_account_id,
        title=title.strip(),
        partner_token_encrypted=encrypt_secret(partner_token.strip()),
        login_encrypted=encrypt_secret(login.strip()),
        password_encrypted=encrypt_secret(password),
        is_active=is_active,
        credential_fingerprint=credential_fingerprint(partner_token, login),
        created_at=now,
        updated_at=now,
    )


def update_credential_secrets(
    credential: YClientsCredential,
    *,
    title: str | None = None,
    partner_token: str | None = None,
    login: str | None = None,
    password: str | None = None,
    is_active: bool | None = None,
) -> None:
    if title is not None:
        credential.title = title.strip()
    if partner_token is not None:
        credential.partner_token_encrypted = encrypt_secret(partner_token.strip())
    if login is not None:
        credential.login_encrypted = encrypt_secret(login.strip())
    if password is not None:
        credential.password_encrypted = encrypt_secret(password)
    if is_active is not None:
        credential.is_active = bool(is_active)
    if partner_token is not None or login is not None:
        try:
            partner_value = partner_token.strip() if partner_token is not None else decrypt_secret(credential.partner_token_encrypted)
            login_value = login.strip() if login is not None else decrypt_secret(credential.login_encrypted)
            credential.credential_fingerprint = credential_fingerprint(partner_value, login_value)
        except Exception:
            credential.credential_fingerprint = None
    if partner_token is not None or login is not None or password is not None:
        credential.needs_reauth = False
        credential.last_error = None
        credential.last_error_at = None
    credential.updated_at = datetime.utcnow()


def mark_credential_success_sync(db: Session, credential_id: int | None) -> None:
    if credential_id is None:
        return
    credential = db.get(YClientsCredential, credential_id)
    if credential is None:
        return
    credential.last_used_at = datetime.utcnow()
    credential.needs_reauth = False
    credential.last_error = None
    credential.last_error_at = None
    credential.updated_at = datetime.utcnow()
    db.commit()


def mark_credential_failure_sync(db: Session, credential_id: int | None, error: str) -> None:
    if credential_id is None:
        return
    credential = db.get(YClientsCredential, credential_id)
    if credential is None:
        return
    credential.needs_reauth = True
    credential.last_error_at = datetime.utcnow()
    credential.last_error = str(error)[:1000]
    credential.updated_at = datetime.utcnow()
    db.commit()


async def mark_credential_success_async(db: AsyncSession, credential_id: int | None) -> None:
    if credential_id is None:
        return
    credential = await db.get(YClientsCredential, credential_id)
    if credential is None:
        return
    credential.last_used_at = datetime.utcnow()
    credential.needs_reauth = False
    credential.last_error = None
    credential.last_error_at = None
    credential.updated_at = datetime.utcnow()


async def mark_credential_failure_async(db: AsyncSession, credential_id: int | None, error: str) -> None:
    if credential_id is None:
        return
    credential = await db.get(YClientsCredential, credential_id)
    if credential is None:
        return
    credential.needs_reauth = True
    credential.last_error_at = datetime.utcnow()
    credential.last_error = str(error)[:1000]
    credential.updated_at = datetime.utcnow()
