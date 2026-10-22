from datetime import datetime

import pytest
from cryptography.fernet import InvalidToken

from helpers import sqlite_session_with_system
from models import PortalAccount, YClientsCredential, YClientsCredentialCompany
from yclients_credentials import (
    decrypt_secret,
    decrypted_credential,
    encrypt_secret,
    load_active_credentials_sync,
    load_credentials_for_companies_async,
    load_credentials_for_companies_sync,
    mark_credential_failure_async,
    mark_credential_failure_sync,
    mark_credential_success_async,
    mark_credential_success_sync,
)


def _sync_credential_session():
    return sqlite_session_with_system([
        PortalAccount.__table__,
        YClientsCredential.__table__,
        YClientsCredentialCompany.__table__,
    ])


def _credential(**overrides) -> YClientsCredential:
    now = datetime(2026, 1, 1, 10, 0, 0)
    values = {
        'portal_account_id': 1,
        'title': 'Credential',
        'partner_token_encrypted': 'partner',
        'login_encrypted': 'login',
        'password_encrypted': 'password',
        'is_active': True,
        'needs_reauth': False,
        'created_at': now,
        'updated_at': now,
    }
    values.update(overrides)
    return YClientsCredential(**values)


@pytest.mark.parametrize('loader_name', ['active', 'companies'])
def test_sync_credential_loaders_persist_sanitized_decrypt_failures(monkeypatch, loader_name):
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', '')
    seeded_at = datetime(2026, 1, 1, 10, 0, 0)
    with _sync_credential_session() as session:
        session.add(PortalAccount(id=1, label='Tenant', created_at=seeded_at))
        session.add(_credential(id=1, created_at=seeded_at, updated_at=seeded_at))
        session.add(YClientsCredentialCompany(credential_id=1, company_id=1))
        session.commit()

        result = (
            load_active_credentials_sync(session)
            if loader_name == 'active'
            else load_credentials_for_companies_sync(session, [1])
        )

        assert result == ([] if loader_name == 'active' else {})
        session.rollback()
        session.expire_all()
        saved = session.get(YClientsCredential, 1)
        assert saved.needs_reauth is True
        assert saved.last_error == 'Decrypt failed: InvalidToken'
        assert saved.last_error_at > seeded_at
        assert saved.updated_at >= saved.last_error_at


@pytest.mark.asyncio
async def test_async_credential_loader_persists_decrypt_failure(async_session, monkeypatch):
    """The async loader must commit its own bookkeeping, like its sync sibling does: its caller
    (get_async_db()) only closes the session, so an uncommitted needs_reauth flag or rotated-key
    re-encryption would otherwise be silently discarded on every request.
    """
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', '')
    seeded_at = datetime(2026, 1, 1, 10, 0, 0)
    credential = _credential(id=1, created_at=seeded_at, updated_at=seeded_at)
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=seeded_at))
    async_session.add(credential)
    async_session.add(YClientsCredentialCompany(credential_id=1, company_id=1))
    await async_session.commit()

    result = await load_credentials_for_companies_async(async_session, [1])

    assert result == {}
    assert credential.needs_reauth is True
    assert credential.last_error == 'Decrypt failed: InvalidToken'
    assert credential.last_error_at > seeded_at
    assert credential.updated_at >= credential.last_error_at

    # Durable, not merely staged in memory: a defensive rollback plus a fresh read from the
    # DB must still show the failure bookkeeping.
    await async_session.rollback()
    await async_session.refresh(credential)
    assert credential.needs_reauth is True
    assert credential.last_error == 'Decrypt failed: InvalidToken'
    assert credential.last_error_at > seeded_at
    assert credential.updated_at >= credential.last_error_at


def test_mark_credential_success_sync_clears_error_state():
    stale_error_at = datetime(2026, 1, 2, 10, 0, 0)
    with _sync_credential_session() as session:
        session.add(PortalAccount(id=1, label='Tenant', created_at=datetime(2026, 1, 1, 0, 0, 0)))
        session.add(_credential(id=1, needs_reauth=True, last_error='Auth failed', last_error_at=stale_error_at))
        session.commit()

        mark_credential_success_sync(session, 1)

        saved = session.get(YClientsCredential, 1)
        assert saved.needs_reauth is False
        assert saved.last_error is None
        assert saved.last_error_at is None
        assert saved.last_used_at is not None
        assert saved.updated_at is not None


def test_mark_credential_failure_sync_truncates_error_and_keeps_last_used_at():
    last_used_at = datetime(2026, 1, 2, 10, 0, 0)
    with _sync_credential_session() as session:
        session.add(PortalAccount(id=1, label='Tenant', created_at=datetime(2026, 1, 1, 0, 0, 0)))
        session.add(_credential(id=1, last_used_at=last_used_at))
        session.commit()

        mark_credential_failure_sync(session, 1, 'x' * 1205)

        saved = session.get(YClientsCredential, 1)
        assert saved.needs_reauth is True
        assert saved.last_error == 'x' * 1000
        assert saved.last_error_at is not None
        assert saved.last_used_at == last_used_at
        assert saved.updated_at is not None


@pytest.mark.asyncio
async def test_mark_credential_success_async_clears_error_state(async_session):
    stale_error_at = datetime(2026, 1, 2, 10, 0, 0)
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime(2026, 1, 1, 0, 0, 0)))
    async_session.add(_credential(id=1, needs_reauth=True, last_error='Auth failed', last_error_at=stale_error_at))
    await async_session.commit()

    await mark_credential_success_async(async_session, 1)

    saved = await async_session.get(YClientsCredential, 1)
    assert saved.needs_reauth is False
    assert saved.last_error is None
    assert saved.last_error_at is None
    assert saved.last_used_at is not None
    assert saved.updated_at is not None


@pytest.mark.asyncio
async def test_mark_credential_failure_async_truncates_error_and_keeps_last_used_at(async_session):
    last_used_at = datetime(2026, 1, 2, 10, 0, 0)
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime(2026, 1, 1, 0, 0, 0)))
    async_session.add(_credential(id=1, last_used_at=last_used_at))
    await async_session.commit()

    await mark_credential_failure_async(async_session, 1, 'x' * 1205)

    saved = await async_session.get(YClientsCredential, 1)
    assert saved.needs_reauth is True
    assert saved.last_error == 'x' * 1000
    assert saved.last_error_at is not None
    assert saved.last_used_at == last_used_at
    assert saved.updated_at is not None


def test_a_rotated_key_decrypts_through_the_old_key_and_the_stored_value_is_reencrypted(monkeypatch):
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'first-key')
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', '')
    credential = _credential(
        partner_token_encrypted=encrypt_secret('partner-1'),
        login_encrypted=encrypt_secret('login-1'),
        password_encrypted=encrypt_secret('password-1'),
    )
    old_token = credential.login_encrypted

    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'second-key')
    with pytest.raises(InvalidToken):
        decrypt_secret(old_token)

    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', 'first-key')
    value = decrypted_credential(credential)
    assert (value.partner_token, value.login, value.password) == ('partner-1', 'login-1', 'password-1')

    # Re-encrypted under the new key: it no longer needs the old one.
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', '')
    assert decrypt_secret(credential.login_encrypted) == 'login-1'
