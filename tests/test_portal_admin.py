"""Admin listing, staff mirroring, bulk provisioning, tenant claims and credential-route plumbing."""

import logging
import secrets
from contextlib import contextmanager

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from clock import utcnow
import api
import auth_routes
import portal_account_provision
import portal_staff_sync
import yclients_credentials
from api import app
from auth_service import DUMMY_PASSWORD_HASH, verify_password
from conftest import issue_access_token
from helpers import cached_password_hash
from data_sources import YClientsDataSourceAdapter, companies_claimed_elsewhere
from models import (
    Company,
    Group,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalEmailToken,
    PortalUser,
    PortalUserBranch,
    Staff,
)
from portal_account_provision import provision_all_unlinked_staff, provision_staff_account


def _user(user_id, role, email=None, tenant=1, **fields):
    return PortalUser(
        id=user_id,
        portal_account_id=tenant,
        email=email or f'user{user_id}@example.com',
        password_hash=cached_password_hash('Password12345!'),
        full_name=f'User {user_id}',
        role=role,
        is_active=True,
        email_verified_at=utcnow(),
        created_at=utcnow(),
        **fields,
    )


@pytest_asyncio.fixture
async def tenant_db(async_session, monkeypatch):
    """One tenant with two branches and an owner; background mail runs on the same in-memory database."""
    monkeypatch.setattr(
        auth_routes, 'get_async_session_factory', lambda: async_sessionmaker(async_session.bind, expire_on_commit=False)
    )
    async_session.add(Group(id=1, title='G1'))
    async_session.add_all([Company(id=1, title='Branch 1', group_id=1), Company(id=2, title='Branch 2', group_id=1)])
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=utcnow()))
    async_session.add_all(
        [PortalBranch(portal_account_id=1, company_id=1), PortalBranch(portal_account_id=1, company_id=2)]
    )
    await async_session.flush()
    async_session.add(_user(1, 'owner', 'owner@example.com'))
    await async_session.commit()
    return async_session


@pytest.fixture
def auth_log(caplog):
    """`yclients.auth` does not propagate to the root logger, so caplog has to be attached to it directly."""
    logger = logging.getLogger('yclients.auth')
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.WARNING, logger='yclients.auth')
    yield caplog
    logger.removeHandler(caplog.handler)


@contextmanager
def count_statements(session):
    """Collect the SQL a block sends to the database."""
    statements: list[str] = []

    def record(conn, cursor, statement, *_):
        statements.append(statement)

    engine = session.bind.sync_engine
    event.listen(engine, 'before_cursor_execute', record)
    try:
        yield statements
    finally:
        event.remove(engine, 'before_cursor_execute', record)


def _mentions(statements, table):
    return [statement for statement in statements if f'FROM {table}' in statement or f'JOIN {table}' in statement]


# ---------- AUTH-15: staff mirror sync, admin lists ----------


@pytest.mark.asyncio
async def test_staff_mirror_walk_reads_in_bulk_and_reports_whether_it_wrote(tenant_db):
    for user_id in range(10, 16):
        tenant_db.add(_user(user_id, 'manager'))
        tenant_db.add(PortalUserBranch(user_id=user_id, company_id=1 + user_id % 2))
    await tenant_db.commit()

    with count_statements(tenant_db) as first:
        changed = await portal_staff_sync.sync_all_portal_users_staff(tenant_db, 1)
    await tenant_db.commit()
    with count_statements(tenant_db) as second:
        unchanged = await portal_staff_sync.sync_all_portal_users_staff(tenant_db, 1)

    assert changed is True
    assert unchanged is False
    mirrored = await tenant_db.scalar(
        select(func.count()).select_from(Staff).where(Staff.portal_user_id.in_(range(10, 16)))
    )
    assert mirrored == 6
    # Six users: branches and staff rows are two reads for the whole walk, not two per user.
    assert len(_mentions(second, 'system.portal_user_branches')) == 1
    assert len(_mentions(second, 'staff')) == 1
    assert len(_mentions(first, 'system.portal_user_branches')) == 1


@pytest.mark.asyncio
async def test_a_get_of_the_user_list_writes_nothing_when_no_mirror_is_missing(tenant_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    tenant_db.add(_user(10, 'manager'))
    tenant_db.add(PortalUserBranch(user_id=10, company_id=1))
    await tenant_db.commit()

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    headers = {'Authorization': f'Bearer {await issue_access_token(tenant_db, 1, "owner")}'}
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            first = await client.get('/auth/admin/users', headers=headers)
            with count_statements(tenant_db) as statements:
                second = await client.get('/auth/admin/users', headers=headers)
    finally:
        app.dependency_overrides.clear()

    assert first.status_code == second.status_code == 200
    assert not [s for s in statements if s.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))]
    manager = next(row for row in second.json()['data'] if row.get('id') == 10 or row.get('user_id') == 10)
    assert manager['company_ids'] == [1]


@pytest.mark.asyncio
async def test_branch_ids_for_a_list_of_users_match_the_per_user_loader(tenant_db):
    from auth_service import load_user_access_branch_ids

    tenant_db.add(PortalAccount(id=2, label='Other', created_at=utcnow()))
    tenant_db.add(Company(id=3, title='Branch 3', group_id=1))
    tenant_db.add(PortalBranch(portal_account_id=2, company_id=3))
    tenant_db.add_all(
        [
            _user(10, 'manager'),
            _user(11, 'barber'),
            _user(12, 'owner', tenant=2),
            _user(13, 'platform_admin', tenant=None),
        ]
    )
    tenant_db.add_all([
        PortalUserBranch(user_id=10, company_id=2),
        PortalUserBranch(user_id=10, company_id=1),
        PortalUserBranch(user_id=11, company_id=1),
    ])
    await tenant_db.commit()
    users = (await tenant_db.execute(select(PortalUser).order_by(PortalUser.id))).scalars().all()

    with count_statements(tenant_db) as statements:
        batched = await auth_routes._load_access_branch_ids_by_user(tenant_db, users)

    assert len(statements) == 2
    for user in users:
        assert batched[user.id] == await load_user_access_branch_ids(tenant_db, user), user.role


@pytest.mark.asyncio
async def test_synthetic_staff_id_race_is_retried_instead_of_failing(tenant_db, monkeypatch):
    tenant_db.add(Staff(id=500, name='Existing', company_id=1, fired=0))
    tenant_db.add(_user(10, 'manager'))
    await tenant_db.commit()
    real_next_id = portal_staff_sync._next_staff_id
    offered = []

    async def stale_then_fresh(db):
        # The first answer is an id a concurrent writer has already taken.
        offered.append(500 if not offered else await real_next_id(db))
        return offered[-1]

    monkeypatch.setattr(portal_staff_sync, '_next_staff_id', stale_then_fresh)
    user = await tenant_db.get(PortalUser, 10)
    changed = await portal_staff_sync.sync_portal_user_staff(tenant_db, user, [1, 2])
    await tenant_db.commit()

    assert changed is True
    assert offered[0] == 500
    rows = (await tenant_db.execute(select(Staff).where(Staff.portal_user_id == 10))).scalars().all()
    assert sorted(row.company_id for row in rows) == [1, 2]
    assert len({row.id for row in rows}) == 2 and 500 not in {row.id for row in rows}


@pytest.mark.asyncio
async def test_synthetic_staff_id_race_gives_up_after_a_few_attempts(tenant_db, monkeypatch):
    tenant_db.add(Staff(id=500, name='Existing', company_id=1, fired=0))
    tenant_db.add(_user(10, 'manager'))
    await tenant_db.commit()

    async def always_taken(db):
        return 500

    monkeypatch.setattr(portal_staff_sync, '_next_staff_id', always_taken)
    user = await tenant_db.get(PortalUser, 10)
    with pytest.raises(IntegrityError):
        await portal_staff_sync.sync_portal_user_staff(tenant_db, user, [1])


# ---------- AUTH-16: bulk provisioning ----------


async def _add_staff(session, count, *, company_id=1, start=9000):
    for offset in range(count):
        session.add(
            Staff(
                id=start + offset,
                name=f'Worker {offset}',
                email=f'worker{start + offset}@example.com',
                position='Барбер',
                company_id=company_id,
                fired=0,
                bookable=True,
            )
        )
    await session.commit()


@pytest.mark.asyncio
async def test_bulk_provisioning_query_count_does_not_grow_with_the_batch(tenant_db):
    await _add_staff(tenant_db, 2)
    with count_statements(tenant_db) as small:
        created, errors = await provision_all_unlinked_staff(tenant_db, None)
    await tenant_db.commit()
    assert (len(created), errors) == (2, [])

    await _add_staff(tenant_db, 12, start=9100)
    with count_statements(tenant_db) as large:
        created, errors = await provision_all_unlinked_staff(tenant_db, None)
    await tenant_db.commit()
    assert (len(created), errors) == (12, [])

    lookups = ('system.portal_users', 'system.portal_branches')
    for table in lookups:
        assert len(_mentions(large, table)) == len(_mentions(small, table)), table


@pytest.mark.asyncio
async def test_bulk_provisioned_accounts_get_the_shared_unusable_hash_and_one_sequence_fix(tenant_db, monkeypatch):
    await _add_staff(tenant_db, 3)
    sequence_fixes = []

    async def count_fix(db):
        sequence_fixes.append(1)

    monkeypatch.setattr(portal_account_provision, '_ensure_portal_user_id_sequence', count_fix)
    created, _ = await provision_all_unlinked_staff(tenant_db, None)
    await tenant_db.commit()

    assert len(created) == 3
    assert sequence_fixes == [1]
    hashes = (await tenant_db.execute(select(PortalUser.password_hash).where(PortalUser.id >= 9000))).scalars().all()
    assert set(hashes) == {DUMMY_PASSWORD_HASH}
    assert not verify_password('anything-at-all', DUMMY_PASSWORD_HASH)


@pytest.mark.asyncio
async def test_a_chosen_password_is_still_hashed_for_a_single_account(tenant_db):
    await _add_staff(tenant_db, 1)
    staff = await tenant_db.get(Staff, 9000)
    await provision_staff_account(tenant_db, staff, password='Chosen-Pass-123')
    await tenant_db.commit()

    stored = await tenant_db.scalar(select(PortalUser.password_hash).where(PortalUser.id == 9000))
    assert stored != DUMMY_PASSWORD_HASH
    assert verify_password('Chosen-Pass-123', stored)


@pytest.mark.asyncio
async def test_bulk_provisioning_reports_each_failing_row_and_keeps_going(tenant_db, monkeypatch, auth_log):
    await _add_staff(tenant_db, 4)
    tenant_db.add(Staff(id=9050, name='Zed No Email', position='Барбер', company_id=1, fired=0))
    tenant_db.add(
        Staff(id=9051, name='Zed Dup Email', email='worker9000@example.com', position='Барбер', company_id=1, fired=0)
    )
    await tenant_db.commit()
    real = portal_account_provision._provision

    async def flaky(db, staff, taken, **kwargs):
        if staff.id == 9001:
            raise RuntimeError('portal user id mismatch')
        if staff.id == 9002:
            # What a concurrent insert of the same login does: the row's own flush fails.
            db.add(PortalUser(id=staff.id, email='worker9002@example.com', password_hash='x', role='barber'))
            db.add(PortalUser(id=staff.id, email='worker9002@example.com', password_hash='x', role='barber'))
            await db.flush()
        return await real(db, staff, taken, **kwargs)

    monkeypatch.setattr(portal_account_provision, '_provision', flaky)
    created, errors = await provision_all_unlinked_staff(tenant_db, None)
    await tenant_db.commit()

    assert sorted(item.staff_id for item in created) == [9000, 9003]
    assert len(errors) == 4
    assert any('Real email is required for staff 9050' in error for error in errors)
    assert any('Email already registered' in error for error in errors)
    assert any('9001' in error for error in errors) and any('9002' in error for error in errors)
    # No exception text reaches the caller or the log; the staff id and the class name are enough.
    assert 'staff_id=9001 failed: RuntimeError' in auth_log.text
    assert 'mismatch' not in ' '.join(errors) + auth_log.text
    users = (await tenant_db.execute(select(PortalUser.id).where(PortalUser.id >= 9000))).scalars().all()
    assert sorted(users) == [9000, 9003]


# ---------- AUTH-6: mail is sent after the response ----------


@pytest.mark.asyncio
async def test_forgot_password_answers_alike_when_the_mail_cannot_be_sent(tenant_db, monkeypatch, auth_log):
    def smtp_down(to_email, subject, body):
        raise OSError('smtp.internal:587 refused')

    monkeypatch.setattr('auth_service.send_auth_email', smtp_down)

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            known = await client.post('/auth/forgot-password', json={'email': 'owner@example.com'})
            unknown = await client.post('/auth/forgot-password', json={'email': 'nobody@example.com'})
    finally:
        app.dependency_overrides.clear()

    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert 'auth email failed user_id=1 error=OSError' in auth_log.text
    assert 'smtp.internal' not in auth_log.text


@pytest.mark.asyncio
async def test_forgot_password_mail_is_sent_by_the_background_task(tenant_db, monkeypatch):
    sent = []
    monkeypatch.setattr('auth_service.send_auth_email', lambda to, subject, body: sent.append(to))

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            await client.post('/auth/forgot-password', json={'email': 'owner@example.com'})
    finally:
        app.dependency_overrides.clear()

    assert sent == ['owner@example.com']
    assert await tenant_db.scalar(select(func.count()).select_from(PortalEmailToken)) == 1


# ---------- round-4 L2: distribute-credentials after a database error ----------


@pytest.mark.asyncio
async def test_distribute_credentials_survives_a_database_error_outside_the_senders_own_try(tenant_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    for user_id in (12, 13):
        tenant_db.add(_user(user_id, 'barber'))
        tenant_db.add(PortalUserBranch(user_id=user_id, company_id=1))
    await tenant_db.commit()
    attempts = []

    async def broken_first(db, user):
        attempts.append(user.id)
        if len(attempts) == 1:
            # A failed flush leaves the session unusable until a rollback, with no rollback of its own.
            db.add(PortalUser(id=1, email='clash@example.com', password_hash='x', role='barber'))
            await db.flush()

    monkeypatch.setattr('auth_routes.send_account_invite_email', broken_first)

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test'
        ) as client:
            response = await client.post(
                '/auth/admin/distribute-credentials',
                headers={'Authorization': f'Bearer {await issue_access_token(tenant_db, 1, "owner")}'},
                json={'user_ids': [12, 13]},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert [item['user_id'] for item in data['sent']] == [13]
    assert [item['user_id'] for item in data['errors']] == [12]


@pytest.mark.asyncio
async def test_distribute_credentials_keeps_mailing_every_user_after_one_failure(tenant_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    user_ids = [30, 31, 32, 33]
    for user_id in user_ids:
        tenant_db.add(_user(user_id, 'barber'))
        tenant_db.add(PortalUserBranch(user_id=user_id, company_id=1))
    await tenant_db.commit()
    attempts = []

    async def fails_once(db, user):
        # The rollback of the first failure expires every loaded row; reading one that was not refreshed raises.
        attempts.append((user.id, user.email))
        if len(attempts) == 1:
            raise RuntimeError('smtp down')
        await db.commit()

    monkeypatch.setattr('auth_routes.send_account_invite_email', fails_once)

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, raise_app_exceptions=False), base_url='http://test'
        ) as client:
            response = await client.post(
                '/auth/admin/distribute-credentials',
                headers={'Authorization': f'Bearer {await issue_access_token(tenant_db, 1, "owner")}'},
                json={'user_ids': user_ids},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    data = response.json()['data']
    assert [item['user_id'] for item in data['errors']] == [30]
    assert [item['user_id'] for item in data['sent']] == [31, 32, 33]


@pytest.mark.asyncio
async def test_distribute_credentials_refreshes_each_user_at_most_once_after_failures(tenant_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    user_ids = list(range(20, 26))
    for user_id in user_ids:
        tenant_db.add(_user(user_id, 'barber'))
        tenant_db.add(PortalUserBranch(user_id=user_id, company_id=1))
    await tenant_db.commit()

    async def always_fails(db, user):
        raise RuntimeError('smtp down')

    monkeypatch.setattr('auth_routes.send_account_invite_email', always_fails)
    statements = []
    engine = tenant_db.bind.sync_engine
    event.listen(engine, 'before_cursor_execute', lambda *args: statements.append(args[2]))

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/auth/admin/distribute-credentials',
                headers={'Authorization': f'Bearer {await issue_access_token(tenant_db, 1, "owner")}'},
                json={'user_ids': user_ids},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [item['user_id'] for item in response.json()['data']['errors']] == user_ids
    by_id = 'FROM system.portal_users \nWHERE system.portal_users.id = ?'
    refreshes = [sql for sql in statements if sql.endswith(by_id)]
    # One refresh per user mailed after the first failure, not one per loaded user on every failure.
    assert len(refreshes) == len(user_ids) - 1


# ---------- AUTH-13: one cross-tenant claim check ----------


class _FakeYClients:
    def __init__(self, partner_token, login, password):
        pass

    def authenticate(self):
        return True

    def get_groups(self):
        return [{'id': 10, 'title': 'Network', 'companies': [{'id': 101, 'title': 'Claimed'}]}]


async def _tenant_with_claimed_location(session, *, with_users=True):
    session.add(PortalAccount(id=2, label='Other', created_at=utcnow()))
    await session.flush()
    session.add(
        Company(id=77, portal_account_id=1, source_type='yclients', external_id=101, title='Claimed', group_id=1)
    )
    await session.flush()
    session.add(PortalBranch(portal_account_id=1, company_id=77))
    if with_users:
        session.add(_user(20, 'owner', tenant=1))
    await session.commit()


@pytest.mark.asyncio
async def test_materialize_refuses_a_location_held_by_another_tenant(tenant_db):
    await _tenant_with_claimed_location(tenant_db)
    adapter = YClientsDataSourceAdapter('p', 'l', 'pw', api_factory=_FakeYClients)

    with pytest.raises(HTTPException) as refused:
        await adapter.materialize_branches(tenant_db, 2, [101])

    assert refused.value.status_code == 409
    assert refused.value.detail == 'Companies already linked to another tenant: [101]'
    assert await tenant_db.scalar(select(func.count()).select_from(Company).where(Company.portal_account_id == 2)) == 0
    assert (
        await tenant_db.scalar(
            select(func.count()).select_from(PortalBranch).where(PortalBranch.portal_account_id == 2)
        )
        == 0
    )


@pytest.mark.asyncio
async def test_a_tenant_may_relink_its_own_location_and_take_over_an_admin_only_one(tenant_db):
    await _tenant_with_claimed_location(tenant_db, with_users=False)
    # `owner@example.com` (user 1) already makes tenant 1 a business tenant; drop it to leave admins only.
    await tenant_db.delete(await tenant_db.get(PortalUser, 1))
    tenant_db.add(_user(30, 'platform_admin', tenant=None))
    await tenant_db.commit()

    assert await companies_claimed_elsewhere(tenant_db, 'yclients', [101], 1) == []
    assert await companies_claimed_elsewhere(tenant_db, 'yclients', [101], 2) == []
    tenant_db.add(_user(31, 'owner', tenant=1))
    await tenant_db.commit()
    assert await companies_claimed_elsewhere(tenant_db, 'yclients', [101], 1) == []
    assert await companies_claimed_elsewhere(tenant_db, 'yclients', [101], 2) == [101]
    assert await companies_claimed_elsewhere(tenant_db, 'yclients', [101, 999], None) == [101]


@pytest.mark.asyncio
async def test_admin_credential_create_without_company_ids_is_refused_for_a_claimed_location(tenant_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr('data_sources.YClientsAPI', _FakeYClients)
    await _tenant_with_claimed_location(tenant_db)
    tenant_db.add(_user(40, 'owner', tenant=2))
    await tenant_db.commit()

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            response = await client.post(
                '/auth/admin/yclients-credentials',
                headers={'Authorization': f'Bearer {await issue_access_token(tenant_db, 40, "owner")}'},
                json={'title': 'Mine', 'partner_token': 'p', 'login': 'l', 'password': 'pw'},
            )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()['detail'] == 'Companies already linked to another tenant: [101]'
    failed = (
        await tenant_db.execute(
            select(PortalAuditEvent).where(PortalAuditEvent.action == 'yclients_credentials.create_failed')
        )
    ).scalars().all()
    assert [event_row.metadata_json for event_row in failed] == [{'success': False, 'error_type': 'HTTPException'}]


# ---------- AUTH-17: credential routes ----------


@pytest.mark.asyncio
async def test_payload_test_rejects_unknown_fields_and_accepts_a_null_source_type(tenant_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)
    monkeypatch.setattr('data_sources.YClientsAPI', _FakeYClients)

    async def override_db():
        yield tenant_db

    headers = {'Authorization': f'Bearer {await issue_access_token(tenant_db, 1, "owner")}'}
    good = {'partner_token': 'p', 'login': 'l', 'password': 'pw'}
    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            null_source = await client.post(
                '/auth/admin/yclients-credentials/test', headers=headers, json={**good, 'source_type': None}
            )
            extra = await client.post(
                '/auth/admin/yclients-credentials/test', headers=headers, json={**good, 'is_active': True}
            )
            too_long = await client.post(
                '/auth/admin/yclients-credentials/test', headers=headers, json={**good, 'login': 'x' * 256}
            )
            number = await client.post(
                '/auth/admin/yclients-credentials/test', headers=headers, json={**good, 'source_type': 5}
            )
    finally:
        app.dependency_overrides.clear()

    assert null_source.status_code == 200
    for refused in (extra, too_long, number):
        assert refused.status_code == 400
        assert refused.json()['detail'] == 'Invalid credential request'
        assert 'xxx' not in refused.text


@pytest.mark.asyncio
async def test_listing_credentials_writes_no_audit_row(tenant_db, monkeypatch):
    monkeypatch.setattr('auth_deps.AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield tenant_db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            with count_statements(tenant_db) as statements:
                response = await client.get(
                    '/auth/admin/yclients-credentials',
                    headers={'Authorization': f'Bearer {await issue_access_token(tenant_db, 1, "owner")}'},
                )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert not [s for s in statements if 'portal_audit_events' in s]


# ---------- AUTH-22: key hygiene warning ----------


def test_passphrase_keys_are_warned_about_in_production_without_logging_the_key(monkeypatch, caplog):
    from cryptography.fernet import Fernet

    passphrase = 'a-long-passphrase-that-is-not-a-fernet-key-0123456789'
    monkeypatch.setattr(yclients_credentials, 'IS_PRODUCTION', True)
    monkeypatch.setattr(yclients_credentials, 'PORTAL_CREDENTIALS_ENCRYPTION_KEY', passphrase)
    monkeypatch.setattr(yclients_credentials, 'PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', Fernet.generate_key().decode())

    # Like `yclients.auth`, this logger stops propagating once api.py has configured the `yclients` tree.
    logger = logging.getLogger('yclients.credentials')
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger='yclients.credentials'):
            yclients_credentials.warn_if_passphrase_keys()
    finally:
        logger.removeHandler(caplog.handler)

    assert 'PORTAL_CREDENTIALS_ENCRYPTION_KEY is not a urlsafe-base64' in caplog.text
    assert 'KEY_OLD' not in caplog.text
    assert passphrase not in caplog.text


def test_a_real_fernet_key_or_a_dev_environment_is_not_warned_about(monkeypatch, caplog):
    from cryptography.fernet import Fernet

    monkeypatch.setattr(yclients_credentials, 'PORTAL_CREDENTIALS_ENCRYPTION_KEY', Fernet.generate_key().decode())
    monkeypatch.setattr(yclients_credentials, 'PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', '')
    with caplog.at_level(logging.WARNING, logger='yclients.credentials'):
        monkeypatch.setattr(yclients_credentials, 'IS_PRODUCTION', True)
        yclients_credentials.warn_if_passphrase_keys()
        monkeypatch.setattr(yclients_credentials, 'PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'passphrase')
        monkeypatch.setattr(yclients_credentials, 'IS_PRODUCTION', False)
        yclients_credentials.warn_if_passphrase_keys()

    assert caplog.text == ''


def test_a_full_entropy_hex_key_is_not_warned_about(monkeypatch, caplog):
    # `openssl rand -hex 32` is what the env examples tell operators to generate; its hash fallback is harmless.
    monkeypatch.setattr(yclients_credentials, 'IS_PRODUCTION', True)
    monkeypatch.setattr(yclients_credentials, 'PORTAL_CREDENTIALS_ENCRYPTION_KEY', secrets.token_hex(32))
    monkeypatch.setattr(yclients_credentials, 'PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', 'ab' * 16)
    # Like `yclients.auth`, this logger stops propagating once api.py has configured the `yclients` tree.
    logger = logging.getLogger('yclients.credentials')
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger='yclients.credentials'):
            yclients_credentials.warn_if_passphrase_keys()
    finally:
        logger.removeHandler(caplog.handler)

    assert 'PORTAL_CREDENTIALS_ENCRYPTION_KEY is not' not in caplog.text
    assert 'PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD is not' in caplog.text
