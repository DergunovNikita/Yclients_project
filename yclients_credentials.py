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

from config import LOGIN, PARTNER_TOKEN, PASSWORD
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
    is_fallback: bool = False


def _fernet() -> Fernet:
    raw_key = os.getenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', '').strip()
    if not raw_key:
        raise CredentialsConfigError('PORTAL_CREDENTIALS_ENCRYPTION_KEY is not configured')
    try:
        return Fernet(raw_key.encode('utf-8'))
    except Exception:
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw_key.encode('utf-8')).digest())
        return Fernet(derived)


def encrypt_secret(value: str) -> str:
    return _fernet().encrypt(value.encode('utf-8')).decode('ascii')


def decrypt_secret(value: str) -> str:
    return _fernet().decrypt(value.encode('ascii')).decode('utf-8')


def fallback_credentials() -> YClientsCredentialValue | None:
    values = (PARTNER_TOKEN.strip(), LOGIN.strip(), PASSWORD.strip())
    if not all(values):
        return None
    return YClientsCredentialValue(
        id=None,
        title='Environment credentials',
        partner_token=values[0],
        login=values[1],
        password=values[2],
        is_fallback=True,
    )


def credential_payload(
    credential: YClientsCredential,
    company_ids: Iterable[int],
) -> dict:
    return {
        'id': credential.id,
        'title': credential.title,
        'is_active': bool(credential.is_active),
        'company_ids': sorted(int(item) for item in company_ids),
        'has_partner_token': bool(credential.partner_token_encrypted),
        'has_login': bool(credential.login_encrypted),
        'has_password': bool(credential.password_encrypted),
        'created_at': credential.created_at.isoformat() if credential.created_at else None,
        'updated_at': credential.updated_at.isoformat() if credential.updated_at else None,
    }


def decrypted_credential(
    credential: YClientsCredential,
    company_ids: Iterable[int] = (),
) -> YClientsCredentialValue:
    return YClientsCredentialValue(
        id=credential.id,
        title=credential.title,
        partner_token=decrypt_secret(credential.partner_token_encrypted),
        login=decrypt_secret(credential.login_encrypted),
        password=decrypt_secret(credential.password_encrypted),
        company_ids=tuple(sorted(int(item) for item in company_ids)),
    )


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
) -> tuple[dict[int, YClientsCredentialValue], YClientsCredentialValue | None]:
    normalized_company_ids = [int(item) for item in dict.fromkeys(company_ids)]
    if not normalized_company_ids:
        return {}, fallback_credentials()

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
        result[int(company_id)] = decrypted_credential(credential, [int(company_id)])

    fallback = fallback_credentials()
    for company_id in normalized_company_ids:
        if company_id not in result and fallback is not None:
            result[company_id] = fallback
    return result, fallback


def load_credentials_for_companies_sync(
    db: Session,
    company_ids: Iterable[int],
) -> tuple[dict[int, YClientsCredentialValue], YClientsCredentialValue | None]:
    normalized_company_ids = [int(item) for item in dict.fromkeys(company_ids)]
    if not normalized_company_ids:
        return {}, fallback_credentials()

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
        result[int(company_id)] = decrypted_credential(credential, [int(company_id)])

    fallback = fallback_credentials()
    for company_id in normalized_company_ids:
        if company_id not in result and fallback is not None:
            result[company_id] = fallback
    return result, fallback


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


async def list_credential_payloads(db: AsyncSession) -> list[dict]:
    credentials = (
        await db.execute(select(YClientsCredential).order_by(YClientsCredential.id.asc()))
    ).scalars().all()
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
    title: str,
    partner_token: str,
    login: str,
    password: str,
    is_active: bool = True,
) -> YClientsCredential:
    now = datetime.utcnow()
    return YClientsCredential(
        title=title.strip(),
        partner_token_encrypted=encrypt_secret(partner_token.strip()),
        login_encrypted=encrypt_secret(login.strip()),
        password_encrypted=encrypt_secret(password),
        is_active=is_active,
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
    credential.updated_at = datetime.utcnow()
