from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import Base, PortalAccount, YClientsCredential
from yclients_credentials import (
    mark_credential_failure_async,
    mark_credential_failure_sync,
    mark_credential_success_async,
    mark_credential_success_sync,
)


@contextmanager
def _sync_credential_session():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=[
        PortalAccount.__table__,
        YClientsCredential.__table__,
    ])
    session_local = sessionmaker(bind=engine)
    session = session_local()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


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
