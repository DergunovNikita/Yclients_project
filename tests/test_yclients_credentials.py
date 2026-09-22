from contextlib import contextmanager
from datetime import datetime

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from models import Base, PortalAccount, YClientsCredential, YClientsCredentialCompany
from yclients_credentials import (
    load_active_credentials_sync,
    load_credentials_for_companies_async,
    load_credentials_for_companies_sync,
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
        YClientsCredentialCompany.__table__,
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


def test_only_the_read_path_loads_credentials_async():
    """Pin the caller chain that makes the bookkeeping commit safe — as far as it can be.

    Its commit rides on a request-scoped session, and that is safe only because every caller
    is a read path — no other pending work exists for the commit to flush. That property
    cannot be asserted at runtime: the function's own select() autoflushes first, so a
    caller's pending object has already left db.new by the time any guard could look (tried,
    and it silently committed the thing it was meant to block).

    So pin it at the source level instead, two levels deep: this function's callers and
    fetch_record_stats's. A new entry point fails here, which is the moment to decide whether
    it is read-only or whether the bookkeeping needs its own session.

    Be clear about the limit: the property that actually makes the commit safe is that the
    whole REQUEST stages no other writes, and that is decided by the endpoint, not by either
    of these two functions. A future mutating endpoint that also renders a summary would keep
    this test green and still have its partial work committed early. Closing that properly
    means giving the bookkeeping its own session; this pin only guarantees the chain cannot
    grow a new branch unnoticed.
    """
    root = Path(__file__).resolve().parent.parent
    # '.claude' holds git worktrees — separate checkouts of this same repo, whose copies
    # of yclients_analytics.py would otherwise show up as extra callers.
    skip_dirs = {'.venv', 'venv', 'env', '.env', 'node_modules', '__pycache__', 'tests',
                 '.git', '.claude', 'build', 'dist'}
    callers = set()
    # rglob, not glob: scripts/ and alembic/ are exactly where a new caller would plausibly
    # land (scripts/backfill_staff_schedules.py already imports the sync sibling), and a
    # root-only scan would pass while missing it.
    record_stats_callers = set()
    for path in root.rglob('*.py'):
        if skip_dirs & set(path.relative_to(root).parts):
            continue
        # errors='ignore': a stray non-UTF-8 .py anywhere in the tree should not turn this
        # into a UnicodeDecodeError instead of the assertion message it exists to produce.
        text = path.read_text(encoding='utf-8', errors='ignore')
        relative = str(path.relative_to(root))
        # Match the CALL form, not the bare name: both functions are discussed by name in
        # comments (including in yclients_credentials.py itself), and prose is not a caller.
        # Also match a bare import, which is how an alias would evade the call-form check
        # (`from yclients_credentials import load_... as _load` then `_load(db, ...)`).
        if path.name != 'yclients_credentials.py' and (
            'load_credentials_for_companies_async(' in text
            or 'import load_credentials_for_companies_async' in text
        ):
            callers.add(relative)
        if path.name != 'yclients_analytics.py' and (
            'fetch_record_stats(' in text or 'import fetch_record_stats' in text
        ):
            record_stats_callers.add(relative)

    assert callers == {'yclients_analytics.py'}, (
        'load_credentials_for_companies_async gained a caller. It commits on a shared '
        'request-scoped session; confirm the new caller stages no other writes, or give the '
        f'bookkeeping its own session. Callers now: {sorted(callers)}'
    )
    assert record_stats_callers == {'dashboard_service.py'}, (
        'fetch_record_stats gained a caller, which puts a new entry point on the chain that '
        'reaches the credential bookkeeping commit. Confirm it is a read path. Callers now: '
        f'{sorted(record_stats_callers)}'
    )
