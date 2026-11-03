"""Linking a portal account to the person's own YClients row: what the account sees, what moves, who may do it."""
from datetime import date, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import api
import auth_deps
from api import app
from clock import utcnow
from conftest import issue_access_token
from helpers import cached_password_hash
from models import (
    Appointment,
    Company,
    Group,
    ManualFactMetric,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    Staff,
)
from portal_staff_sync import sync_all_portal_users_staff

OWNER, BRANCH_ADMIN, OTHER_OWNER, ADMIN, SECOND_ADMIN, DEMO_OWNER, PLATFORM_ADMIN = 1, 2, 3, 10, 11, 4, 5
REAL_ANNA, REAL_BORIS, FIRED, ANNA_SECOND_BRANCH, OTHER_TENANT_ROW, WAITLIST = 100, 101, 102, 200, 300, 103
PLACEHOLDER = 104
MARCH = (date(2025, 3, 1), date(2025, 3, 31))


def _user(user_id, role, tenant=1, *, demo=False):
    return PortalUser(
        id=user_id, portal_account_id=tenant, email=f'{role}{user_id}@example.com',
        password_hash=cached_password_hash('Passw0rd!x'), full_name=f'User {user_id}', role=role, is_active=True,
        is_demo=demo, email_verified_at=utcnow(), created_at=utcnow(),
    )


def _fact(staff_id, value, code='reviews_qty', company_id=1):
    return ManualFactMetric(
        period_start=MARCH[0], period_end=MARCH[1], company_id=company_id, staff_id=staff_id,
        metric_code=code, value=value, updated_at=datetime(2025, 3, 28, 10),
    )


@pytest_asyncio.fixture
async def link_db(async_session, monkeypatch):
    """Tenant 1 (branches 1 and 2) with an admin account that has only mirror rows; tenant 2 (branch 3)."""
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add_all([
        Group(id=1, title='G'),
        PortalAccount(id=1, label='Tenant', created_at=utcnow()),
        PortalAccount(id=2, label='Other tenant', created_at=utcnow()),
    ])
    await async_session.flush()
    async_session.add_all([
        Company(id=1, title='Branch 1', timezone='UTC', group_id=1),
        Company(id=2, title='Branch 2', timezone='UTC', group_id=1),
        Company(id=3, title='Branch 3', timezone='UTC', group_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        PortalBranch(portal_account_id=1, company_id=1),
        PortalBranch(portal_account_id=1, company_id=2),
        PortalBranch(portal_account_id=2, company_id=3),
        _user(OWNER, 'owner'),
        _user(BRANCH_ADMIN, 'branch_admin'),
        _user(OTHER_OWNER, 'owner', tenant=2),
        _user(DEMO_OWNER, 'owner', demo=True),
        _user(ADMIN, 'admin'),
        _user(SECOND_ADMIN, 'admin'),
        Staff(id=REAL_ANNA, external_id=REAL_ANNA, name='Anna', position='Администратор', company_id=1),
        Staff(id=REAL_BORIS, external_id=REAL_BORIS, name='Boris', position='Администратор', company_id=1),
        Staff(id=WAITLIST, external_id=WAITLIST, name='Лист ожидания', company_id=1),
        Staff(id=PLACEHOLDER, external_id=PLACEHOLDER, name='Администратор Ковалевской', company_id=1),
        Staff(id=FIRED, external_id=FIRED, name='Gone', position='Администратор', company_id=1, fired=1),
        Staff(id=ANNA_SECOND_BRANCH, external_id=ANNA_SECOND_BRANCH, name='Anna', position='Администратор',
              company_id=2),
        Staff(id=OTHER_TENANT_ROW, external_id=OTHER_TENANT_ROW, name='Elsewhere', position='Администратор',
              company_id=3),
    ])
    await async_session.flush()
    async_session.add_all([
        PortalUserBranch(user_id=ADMIN, company_id=1),
        PortalUserBranch(user_id=ADMIN, company_id=2),
        PortalUserBranch(user_id=SECOND_ADMIN, company_id=1),
        PortalUserBranch(user_id=BRANCH_ADMIN, company_id=1),
    ])
    await async_session.flush()
    await sync_all_portal_users_staff(async_session, 1)
    await async_session.commit()
    return async_session


async def _mirror_id(db, user_id, company_id):
    return (
        await db.execute(
            select(Staff.id).where(Staff.portal_user_id == user_id, Staff.company_id == company_id, Staff.fired == 0)
        )
    ).scalar_one()


async def _request(db, user_id, role, method, url, **kwargs):
    token = await issue_access_token(db, user_id, role)

    async def override_db():
        yield db

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.request(method, url, headers={'Authorization': f'Bearer {token}'}, **kwargs)
    finally:
        app.dependency_overrides.clear()


async def _link(db, staff_id, *, actor=OWNER, role='owner', user=ADMIN):
    return await _request(db, actor, role, 'POST', f'/auth/admin/users/{user}/staff-links', json={'staff_id': staff_id})


async def _facts(db, staff_id):
    rows = (await db.execute(select(ManualFactMetric).where(ManualFactMetric.staff_id == staff_id))).scalars().all()
    return {(row.period_start, row.metric_code): row.value for row in rows}


@pytest.mark.asyncio
async def test_linking_gives_the_account_the_numbers_of_the_real_row(link_db):
    db = link_db
    # A barber is the role clamped to its own row; an administrator reads the whole branch and would not show the link.
    (await db.get(PortalUser, ADMIN)).role = 'barber'
    db.add_all([
        Appointment(id=1, company_id=1, client_id=1, staff_id=REAL_ANNA, date=date(2025, 3, 3),
                    datetime=datetime(2025, 3, 3, 12), attendance=1, create_date=datetime(2025, 3, 1, 9)),
        Appointment(id=2, company_id=1, client_id=2, staff_id=REAL_ANNA, date=date(2025, 3, 4),
                    datetime=datetime(2025, 3, 4, 12), attendance=1, create_date=datetime(2025, 3, 1, 9)),
    ])
    mirror = await _mirror_id(db, ADMIN, 1)
    db.add_all([_fact(mirror, 2.0), _fact(REAL_ANNA, 1.0)])
    await db.commit()
    params = {'start_date': '2025-03-01', 'end_date': '2025-03-31', 'company_id': 1}

    async def own_visits():
        response = await _request(db, ADMIN, 'barber', 'GET', '/dashboard/bundle', params=params)
        assert response.status_code == 200, response.text
        metrics = response.json()['data']['summary']['visit_metrics']
        return metrics['first_visits'] + metrics['repeat_visits']

    async def own_staff():
        response = await _request(db, ADMIN, 'barber', 'GET', '/dashboard/staff', params={'company_id': 1})
        assert response.status_code == 200, response.text
        return [row['id'] for row in response.json()['data']]

    assert await own_visits() == 0
    assert await own_staff() == [mirror]

    response = await _link(db, REAL_ANNA)

    assert response.status_code == 200, response.text
    assert response.json()['data'] == {
        'staff_id': REAL_ANNA, 'company_id': 1, 'retired_staff_ids': [mirror], 'moved_fact_values': 1,
    }
    assert await own_staff() == [REAL_ANNA]
    assert await own_visits() > 0
    # The mirror's review count joined the row's own; nothing is left on the retired mirror.
    assert await _facts(db, REAL_ANNA) == {(MARCH[0], 'reviews_qty'): 3.0}
    assert await _facts(db, mirror) == {}
    retired = await db.get(Staff, mirror)
    assert (retired.fired, retired.portal_user_id) == (1, None)
    actions = (await db.execute(select(PortalAuditEvent.action).order_by(PortalAuditEvent.id))).scalars().all()
    assert 'portal_user.staff_linked' in actions and 'manual_fact.merged_from_mirror' in actions
    # The branch the user was not linked in keeps its mirror.
    assert await _mirror_id(db, ADMIN, 2)


@pytest.mark.asyncio
async def test_the_manual_fact_editor_of_a_linked_account_edits_the_real_row(link_db):
    db = link_db
    mirror = await _mirror_id(db, ADMIN, 1)
    db.add(_fact(mirror, 4.0))
    await db.commit()

    assert (await _link(db, REAL_ANNA)).status_code == 200
    editor = await _request(
        db, ADMIN, 'admin', 'GET', '/dashboard/plan/reviews_fact', params={'month': '2025-03', 'company_id': 1}
    )

    assert editor.status_code == 200, editor.text
    assert {row['staff_id']: row['value'] for row in editor.json()['data']['rows']} == {REAL_ANNA: 4.0}


@pytest.mark.asyncio
async def test_refusals_leave_nothing_behind(link_db):
    db = link_db
    mirror = await _mirror_id(db, ADMIN, 1)
    db.add(_fact(mirror, 2.0))
    await db.commit()

    assert (await _link(db, REAL_ANNA)).status_code == 200
    # The same row again, and a second row of a branch that already has its link.
    assert (await _link(db, REAL_ANNA)).status_code == 409
    assert (await _link(db, REAL_BORIS)).status_code == 409
    # A row linked to somebody else.
    assert (await _link(db, REAL_ANNA, user=SECOND_ADMIN)).status_code == 409
    # Not a row an account can take: fired, a mirror, outside the user's branches, another tenant, unknown.
    assert (await _link(db, FIRED, user=SECOND_ADMIN)).status_code == 404
    assert (await _link(db, mirror, user=SECOND_ADMIN)).status_code == 404
    assert (await _link(db, OTHER_TENANT_ROW, user=SECOND_ADMIN)).status_code == 404
    assert (await _link(db, 99999, user=SECOND_ADMIN)).status_code == 404
    assert (await _link(db, ANNA_SECOND_BRANCH, user=SECOND_ADMIN)).status_code == 400

    assert (await db.get(Staff, REAL_ANNA)).portal_user_id == ADMIN
    assert (await db.get(Staff, REAL_BORIS)).portal_user_id is None
    assert (await db.get(Staff, ANNA_SECOND_BRANCH)).portal_user_id is None
    # The refused calls did not touch the second account's own mirror.
    assert await _mirror_id(db, SECOND_ADMIN, 1)


@pytest.mark.asyncio
async def test_only_admin_and_barber_accounts_take_a_link(link_db):
    response = await _link(link_db, REAL_ANNA, user=BRANCH_ADMIN)

    assert response.status_code == 400
    assert (await link_db.get(Staff, REAL_ANNA)).portal_user_id is None


@pytest.mark.asyncio
async def test_who_may_link(link_db):
    db = link_db
    # A branch admin reaches users inside their own branches only; ADMIN also works in branch 2.
    assert (await _link(db, REAL_ANNA, actor=BRANCH_ADMIN, role='branch_admin', user=ADMIN)).status_code == 403
    assert (await _link(db, REAL_ANNA, actor=BRANCH_ADMIN, role='branch_admin', user=SECOND_ADMIN)).status_code == 200
    # Another tenant's owner cannot tell the account exists.
    assert (await _link(db, REAL_BORIS, actor=OTHER_OWNER, role='owner', user=ADMIN)).status_code == 404
    # Nobody manages themselves, and the demo account is read-only.
    assert (await _link(db, REAL_BORIS, actor=ADMIN, role='admin', user=ADMIN)).status_code == 403
    assert (await _link(db, REAL_BORIS, actor=DEMO_OWNER, role='owner', user=ADMIN)).status_code == 403
    assert (await db.get(Staff, REAL_BORIS)).portal_user_id is None


@pytest.mark.asyncio
async def test_candidates_are_the_free_active_rows_of_the_users_branches(link_db):
    db = link_db
    listing = await _request(db, OWNER, 'owner', 'GET', f'/auth/admin/users/{ADMIN}/staff-links')

    assert listing.status_code == 200, listing.text
    branches = {item['company_id']: item for item in listing.json()['data']['branches']}
    assert sorted(branches) == [1, 2]
    assert [row['staff_id'] for row in branches[1]['candidates']] == [REAL_ANNA, REAL_BORIS]
    assert [row['staff_id'] for row in branches[2]['candidates']] == [ANNA_SECOND_BRANCH]
    assert branches[1]['linked'] == []
    # A service row is not on offer, and a hand-made request cannot take it either.
    assert (await _link(db, WAITLIST)).status_code == 404
    assert (await _link(db, PLACEHOLDER)).status_code == 404

    assert (await _link(db, REAL_ANNA)).status_code == 200
    after = await _request(db, OWNER, 'owner', 'GET', f'/auth/admin/users/{ADMIN}/staff-links')
    branches = {item['company_id']: item for item in after.json()['data']['branches']}
    assert [row['staff_id'] for row in branches[1]['linked']] == [REAL_ANNA]
    assert branches[1]['candidates'] == []
    # Linked elsewhere, so not on offer to a second account.
    other = await _request(db, OWNER, 'owner', 'GET', f'/auth/admin/users/{SECOND_ADMIN}/staff-links')
    assert [row['staff_id'] for row in other.json()['data']['branches'][0]['candidates']] == [REAL_BORIS]


@pytest.mark.asyncio
async def test_a_platform_admin_links_inside_the_tenant_of_the_account(link_db):
    db = link_db
    db.add(_user(PLATFORM_ADMIN, 'platform_admin', tenant=None))
    await db.commit()

    # Elsewhere's row belongs to another tenant: out of reach even for the platform admin.
    assert (await _link(db, OTHER_TENANT_ROW, actor=PLATFORM_ADMIN, role='platform_admin')).status_code == 404
    assert (await _link(db, REAL_ANNA, actor=PLATFORM_ADMIN, role='platform_admin')).status_code == 200


@pytest.mark.asyncio
async def test_unlinking_releases_the_row_and_brings_the_mirror_back(link_db):
    db = link_db
    assert (await _link(db, REAL_ANNA)).status_code == 200

    response = await _request(db, OWNER, 'owner', 'DELETE', f'/auth/admin/users/{ADMIN}/staff-links/{REAL_ANNA}')

    assert response.status_code == 200, response.text
    released = await db.get(Staff, REAL_ANNA)
    # The row is YClients' own: released, never fired.
    assert (released.portal_user_id, released.fired) == (None, 0)
    assert await _mirror_id(db, ADMIN, 1) != REAL_ANNA
    # Not linked any more, so a second release finds nothing, and the row can go to somebody else.
    again = await _request(db, OWNER, 'owner', 'DELETE', f'/auth/admin/users/{ADMIN}/staff-links/{REAL_ANNA}')
    assert again.status_code == 404
    assert (await _link(db, REAL_ANNA, user=SECOND_ADMIN)).status_code == 200


@pytest.mark.asyncio
async def test_a_row_cannot_be_released_by_somebody_it_does_not_belong_to(link_db):
    db = link_db
    assert (await _link(db, REAL_ANNA)).status_code == 200

    response = await _request(db, OWNER, 'owner', 'DELETE', f'/auth/admin/users/{SECOND_ADMIN}/staff-links/{REAL_ANNA}')

    assert response.status_code == 404
    assert (await db.get(Staff, REAL_ANNA)).portal_user_id == ADMIN


@pytest.mark.asyncio
async def test_a_linked_row_keeps_its_name_and_position_when_the_account_is_edited(link_db):
    db = link_db
    assert (await _link(db, REAL_ANNA)).status_code == 200

    update = await _request(
        db, OWNER, 'owner', 'PATCH', f'/auth/admin/users/{ADMIN}', json={'full_name': 'Renamed', 'role': 'barber'}
    )

    assert update.status_code == 200, update.text
    row = await db.get(Staff, REAL_ANNA)
    assert (row.name, row.position, row.fired) == ('Anna', 'Администратор', 0)


@pytest.mark.asyncio
async def test_deleting_the_account_releases_a_linked_row_instead_of_firing_it(link_db):
    db = link_db
    assert (await _link(db, REAL_ANNA)).status_code == 200

    response = await _request(db, OWNER, 'owner', 'DELETE', f'/auth/admin/users/{ADMIN}')

    assert response.status_code == 200, response.text
    row = await db.get(Staff, REAL_ANNA)
    assert (row.portal_user_id, row.fired) == (None, 0)


@pytest.mark.asyncio
async def test_listing_users_does_not_wake_a_retired_mirror(link_db):
    db = link_db
    mirror = await _mirror_id(db, ADMIN, 1)
    assert (await _link(db, REAL_ANNA)).status_code == 200

    listing = await _request(db, OWNER, 'owner', 'GET', '/auth/admin/users')

    assert listing.status_code == 200
    assert (await db.get(Staff, mirror)).fired == 1
    active = (await db.execute(select(Staff.id).where(Staff.portal_user_id == ADMIN, Staff.fired == 0))).scalars().all()
    assert sorted(active) == sorted([REAL_ANNA, await _mirror_id(db, ADMIN, 2)])


@pytest.mark.asyncio
async def test_ids_outside_the_database_range_are_refused_before_the_database(link_db):
    db = link_db
    too_big = 2**31

    assert (await _link(db, too_big)).status_code == 422
    assert (await _link(db, 0)).status_code == 422
    assert (await _link(db, REAL_ANNA, user=too_big)).status_code == 422
    unlink = await _request(db, OWNER, 'owner', 'DELETE', f'/auth/admin/users/{ADMIN}/staff-links/{too_big}')
    assert unlink.status_code == 422


@pytest.mark.asyncio
async def test_a_mirror_made_before_the_portal_marker_existed_is_not_a_linked_row(link_db):
    """The old API inserts mirrors as source 'yclients' without external_id until it restarts: still a mirror."""
    db = link_db
    old_mirror = Staff(id=900, name='Old mirror', position='admin', company_id=2, portal_user_id=SECOND_ADMIN, fired=0)
    db.add(old_mirror)
    db.add(PortalUserBranch(user_id=SECOND_ADMIN, company_id=2))
    await db.commit()

    listed = await _request(db, OWNER, 'owner', 'GET', f'/auth/admin/users/{SECOND_ADMIN}/staff-links')
    branch = next(row for row in listed.json()['data']['branches'] if row['company_id'] == 2)
    assert branch['linked'] == []
    # Nobody can pick it as a person either: it is not a row of YClients.
    assert (await _link(db, 900, user=ADMIN)).status_code == 404
    await sync_all_portal_users_staff(db, 1)
    await db.commit()
    assert (await db.get(Staff, 900)).fired == 0


@pytest.mark.asyncio
async def test_the_user_list_marks_accounts_whose_branch_has_no_linked_row(link_db):
    db = link_db

    async def needs_link():
        listing = await _request(db, OWNER, 'owner', 'GET', '/auth/admin/users')
        assert listing.status_code == 200, listing.text
        return {row['id']: row['needs_staff_link'] for row in listing.json()['data'] if row['is_portal_user']}

    before = await needs_link()
    # A mirror row is the portal's own placeholder, not a link; owners and managers are not clamped to a row at all.
    assert (before[ADMIN], before[SECOND_ADMIN], before[OWNER], before[BRANCH_ADMIN]) == (True, True, False, False)

    assert (await _link(db, REAL_ANNA, user=SECOND_ADMIN)).status_code == 200
    # ADMIN works in two branches and gets a row in only one of them: still short of one.
    assert (await _link(db, REAL_BORIS, user=ADMIN)).status_code == 200
    after = await needs_link()
    assert (after[SECOND_ADMIN], after[ADMIN]) == (False, True)

    assert (await _link(db, ANNA_SECOND_BRANCH, user=ADMIN)).status_code == 200
    assert (await needs_link())[ADMIN] is False


@pytest.mark.asyncio
async def test_service_rows_are_not_listed_as_staff_without_an_account(link_db):
    db = link_db
    listing = await _request(db, OWNER, 'owner', 'GET', '/auth/admin/users')

    assert listing.status_code == 200, listing.text
    unlinked = {row['staff_id'] for row in listing.json()['data'] if not row['is_portal_user']}
    assert {REAL_ANNA, REAL_BORIS} <= unlinked
    assert not {WAITLIST, PLACEHOLDER} & unlinked
