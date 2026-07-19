from datetime import date, datetime
import json

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import api
from api import app
from auth_service import create_access_token, hash_password
from models import (
    Client,
    Appointment,
    Comment,
    Company,
    GoodCatalog,
    GoodTransaction,
    Group,
    PortalAccount,
    PortalAuditEvent,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    ServiceCatalog,
    Staff,
    StaffSchedule,
)


@pytest.mark.asyncio
async def test_api_key_blocks_unauthorized_requests(async_session, monkeypatch):
    monkeypatch.setattr(api, 'API_KEY', 'secret123')
    import auth_deps

    monkeypatch.setattr(auth_deps, 'API_KEY', 'secret123')
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        r1 = await client.get('/companies')
        assert r1.status_code == 401

        r2 = await client.get('/companies', headers={'X-API-Key': 'wrong'})
        assert r2.status_code == 401

        r3 = await client.get('/companies', headers={'X-API-Key': 'secret123'})
        assert r3.status_code == 200

        r4 = await client.get('/health')
        assert r4.status_code == 200

    app.dependency_overrides.clear()
    monkeypatch.setattr(api, 'API_KEY', '')
    monkeypatch.setattr(auth_deps, 'API_KEY', '')
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)


@pytest.mark.asyncio
async def test_companies_endpoint_applies_pagination(async_session):
    group = Group(id=1, title='Group')
    async_session.add(group)
    async_session.add_all([
        Company(id=1, title='A', group_id=1),
        Company(id=2, title='B', group_id=1),
        Company(id=3, title='C', group_id=1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/companies', params={'limit': 2, 'offset': 1})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload['total'] == 3
    assert payload['limit'] == 2
    assert payload['offset'] == 1
    assert [item['id'] for item in payload['data']] == [2, 3]


@pytest.mark.asyncio
async def test_legacy_api_filters_jwt_users_by_tenant_scope(async_session, monkeypatch):
    import auth_deps

    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(Group(id=1, title='Group'))
    async_session.add_all([
        Company(id=1, title='Tenant A Branch', group_id=1),
        Company(id=2, title='Tenant B Branch', group_id=1),
    ])
    async_session.add(PortalAccount(id=1, label='Tenant A', created_at=datetime.utcnow()))
    async_session.add(PortalAccount(id=2, label='Tenant B', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(PortalBranch(portal_account_id=2, company_id=2))
    async_session.add(
        PortalUser(
            id=100,
            portal_account_id=1,
            email='tenant-a@example.com',
            password_hash=hash_password('TenantA123!'),
            full_name='Tenant A',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    async_session.add(PortalUserBranch(user_id=100, company_id=1))
    async_session.add(GoodTransaction(id=1, company_id=1, type_id=1, amount=2.0, cost=10.0))
    async_session.add(GoodTransaction(id=2, company_id=2, type_id=1, amount=1.0, cost=20.0))
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(100, 'manager')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        companies = await client.get('/companies', headers={'Authorization': f'Bearer {token}'})
        forbidden = await client.get(
            '/goods_transactions',
            params={'company_id': 2},
            headers={'Authorization': f'Bearer {token}'},
        )
        own_financial = await client.get(
            '/goods_transactions',
            params={'company_id': 1},
            headers={'Authorization': f'Bearer {token}'},
        )
        exported = await client.get(
            '/export/csv/goods_transactions',
            headers={'Authorization': f'Bearer {token}'},
        )

    app.dependency_overrides.clear()

    assert companies.status_code == 200
    assert [row['id'] for row in companies.json()['data']] == [1]
    assert forbidden.status_code == 403
    assert own_financial.status_code == 403
    assert exported.status_code == 403


@pytest.mark.asyncio
async def test_groups_endpoint_scopes_jwt_users_to_accessible_branches(async_session, monkeypatch):
    import auth_deps

    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add_all([
        Group(id=1, title='Tenant A Group'),
        Group(id=2, title='Tenant B Group'),
        Company(id=1, title='Tenant A Branch', group_id=1),
        Company(id=2, title='Tenant B Branch', group_id=2),
        PortalAccount(id=1, label='Tenant A', created_at=datetime.utcnow()),
        PortalBranch(portal_account_id=1, company_id=1),
        PortalUser(
            id=110,
            portal_account_id=1,
            email='manager-groups@example.com',
            password_hash=hash_password('Manager123!'),
            full_name='Manager',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUserBranch(user_id=110, company_id=1),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(110, 'manager')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/groups', headers={'Authorization': f'Bearer {token}'})

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    assert response.status_code == 200
    assert response.json()['data'] == [{'id': 1, 'title': 'Tenant A Group', 'companies_count': 1}]


@pytest.mark.asyncio
async def test_raw_api_staff_scopes_linked_viewer_and_exports(async_session, monkeypatch):
    import auth_deps

    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(Group(id=1, title='Group'))
    async_session.add(Company(id=1, title='Branch', group_id=1))
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(
        PortalUser(
            id=120,
            portal_account_id=1,
            email='linked-viewer@example.com',
            password_hash=hash_password('Viewer123!'),
            full_name='Linked Viewer',
            role='viewer',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    async_session.add(PortalUserBranch(user_id=120, company_id=1))
    async_session.add_all([
        Staff(id=10, name='Linked Staff', company_id=1, portal_user_id=120),
        Staff(id=11, name='Other Staff', company_id=1),
        Appointment(id=10, company_id=1, staff_id=10, client_id=1, date=date(2025, 1, 10)),
        Appointment(id=11, company_id=1, staff_id=11, client_id=2, date=date(2025, 1, 10)),
        Comment(id=10, company_id=1, master_id=10, text='Own review', date=datetime(2025, 1, 10)),
        Comment(id=11, company_id=1, master_id=11, text='Other review', date=datetime(2025, 1, 10)),
        StaffSchedule(id=10, company_id=1, staff_id=10, date=date(2025, 1, 10)),
        StaffSchedule(id=11, company_id=1, staff_id=11, date=date(2025, 1, 10)),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(120, 'viewer')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        appointments = await client.get('/appointments', headers={'Authorization': f'Bearer {token}'})
        forbidden = await client.get(
            '/appointments',
            params={'staff_id': 11},
            headers={'Authorization': f'Bearer {token}'},
        )
        comments = await client.get('/comments', headers={'Authorization': f'Bearer {token}'})
        schedules_csv = await client.get('/export/csv/staff_schedules', headers={'Authorization': f'Bearer {token}'})

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    assert appointments.status_code == 200
    assert [row['staff_id'] for row in appointments.json()['data']] == [10]
    assert forbidden.status_code == 403
    assert comments.status_code == 200
    assert [row['master_id'] for row in comments.json()['data']] == [10]
    assert schedules_csv.status_code == 200
    assert ',10,' in schedules_csv.text
    assert ',11,' not in schedules_csv.text


@pytest.mark.asyncio
async def test_catalog_money_fields_are_hidden_from_non_financial_roles(async_session, monkeypatch):
    import auth_deps

    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', True)
    async_session.add(Group(id=1, title='Group'))
    async_session.add(Company(id=1, title='Branch', group_id=1))
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(
        PortalUser(
            id=130,
            portal_account_id=1,
            email='manager-catalog@example.com',
            password_hash=hash_password('Manager123!'),
            full_name='Manager',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        )
    )
    async_session.add(PortalUserBranch(user_id=130, company_id=1))
    async_session.add(
        ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Cut',
            price_min=1500,
            updated_at=datetime.utcnow(),
        )
    )
    async_session.add(
        GoodCatalog(
            company_id=1,
            good_id=20,
            title='Wax',
            cost=100,
            actual_cost=80,
            updated_at=datetime.utcnow(),
        )
    )
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(130, 'manager')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        services = await client.get('/services', headers={'Authorization': f'Bearer {token}'})
        goods = await client.get('/goods', headers={'Authorization': f'Bearer {token}'})
        filtered = await client.get('/services', params={'min_price': 1000}, headers={'Authorization': f'Bearer {token}'})

    app.dependency_overrides.clear()
    monkeypatch.setattr(auth_deps, 'AUTH_REQUIRE_LOGIN', False)

    assert services.status_code == 200
    assert services.json()['data'][0]['price_min'] is None
    assert goods.status_code == 200
    assert goods.json()['data'][0]['cost'] is None
    assert goods.json()['data'][0]['actual_cost'] is None
    assert filtered.status_code == 403


async def _seed_client_pii_scope(async_session):
    async_session.add(Group(id=1, title='Group'))
    async_session.add_all([
        Company(id=1, title='Tenant A Branch', group_id=1),
        Company(id=2, title='Tenant B Branch', group_id=1),
    ])
    async_session.add(PortalAccount(id=1, label='Tenant A', created_at=datetime.utcnow()))
    async_session.add(PortalAccount(id=2, label='Tenant B', created_at=datetime.utcnow()))
    async_session.add(PortalBranch(portal_account_id=1, company_id=1))
    async_session.add(PortalBranch(portal_account_id=2, company_id=2))
    async_session.add_all([
        PortalUser(
            id=201,
            portal_account_id=1,
            email='manager@example.com',
            password_hash=hash_password('Manager123!'),
            full_name='Manager',
            role='manager',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUser(
            id=202,
            portal_account_id=1,
            email='viewer@example.com',
            password_hash=hash_password('Viewer123!'),
            full_name='Viewer',
            role='viewer',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
        PortalUser(
            id=203,
            portal_account_id=None,
            email='platform@example.com',
            password_hash=hash_password('Platform123!'),
            full_name='Platform',
            role='platform_admin',
            is_active=True,
            email_verified_at=datetime.utcnow(),
            created_at=datetime.utcnow(),
        ),
    ])
    async_session.add_all([
        PortalUserBranch(user_id=201, company_id=1),
        PortalUserBranch(user_id=202, company_id=1),
    ])
    async_session.add_all([
        Client(
            id=1,
            name='Tenant A Client',
            phone='+111111111',
            email='tenant-a-client@example.com',
            visits_count=3,
            company_id=1,
        ),
        Client(
            id=2,
            name='Tenant B Client',
            phone='+222222222',
            email='tenant-b-client@example.com',
            visits_count=5,
            company_id=2,
        ),
    ])
    await async_session.commit()


@pytest.mark.asyncio
async def test_clients_endpoint_requires_jwt_user_even_when_login_not_required(async_session):
    await _seed_client_pii_scope(async_session)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/clients')

    app.dependency_overrides.clear()

    assert response.status_code == 401
    assert response.json()['detail'] == 'Authentication required'


@pytest.mark.asyncio
async def test_clients_endpoint_rejects_viewer_role(async_session):
    await _seed_client_pii_scope(async_session)

    async def override_db():
        yield async_session

    token = create_access_token(202, 'viewer')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/clients', headers={'Authorization': f'Bearer {token}'})

    app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json()['detail'] == 'Insufficient permissions'


@pytest.mark.asyncio
async def test_clients_endpoint_scopes_manager_and_logs_safe_audit(async_session):
    await _seed_client_pii_scope(async_session)

    async def override_db():
        yield async_session

    token = create_access_token(201, 'manager')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/clients', headers={'Authorization': f'Bearer {token}'})
        forbidden = await client.get(
            '/clients',
            params={'company_id': 2},
            headers={'Authorization': f'Bearer {token}'},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert [row['id'] for row in response.json()['data']] == [1]
    assert response.json()['data'][0]['phone'] == '+111111111'
    assert forbidden.status_code == 403
    assert forbidden.json()['detail'] == 'Branch not allowed'

    audit = (
        await async_session.execute(
            select(PortalAuditEvent).where(PortalAuditEvent.action == 'client_pii.read')
        )
    ).scalar_one()
    assert audit.actor_user_id == 201
    assert audit.portal_account_id == 1
    assert audit.metadata_json == {
        'company_id': None,
        'min_visits': None,
        'limit': 1000,
        'offset': 0,
        'row_count': 1,
        'total': 1,
    }
    serialized_metadata = json.dumps(audit.metadata_json)
    assert 'tenant-a-client@example.com' not in serialized_metadata
    assert '+111111111' not in serialized_metadata
    assert 'token' not in serialized_metadata.lower()


@pytest.mark.asyncio
async def test_platform_admin_clients_requires_selected_tenant(async_session):
    await _seed_client_pii_scope(async_session)

    async def override_db():
        yield async_session

    token = create_access_token(203, 'platform_admin')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        missing_tenant = await client.get('/clients', headers={'Authorization': f'Bearer {token}'})
        tenant_b = await client.get(
            '/clients',
            headers={'Authorization': f'Bearer {token}', 'X-Portal-Account-Id': '2'},
        )

    app.dependency_overrides.clear()

    assert missing_tenant.status_code == 400
    assert missing_tenant.json()['detail'] == 'X-Portal-Account-Id is required'
    assert tenant_b.status_code == 200
    assert [row['id'] for row in tenant_b.json()['data']] == [2]


@pytest.mark.asyncio
async def test_clients_csv_export_is_scoped_and_audited(async_session):
    await _seed_client_pii_scope(async_session)

    async def override_db():
        yield async_session

    token = create_access_token(201, 'manager')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/export/csv/clients', headers={'Authorization': f'Bearer {token}'})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert 'Tenant A Client' in response.text
    assert '+111111111' in response.text
    assert 'Tenant B Client' not in response.text
    assert '+222222222' not in response.text
    audit = (
        await async_session.execute(
            select(PortalAuditEvent).where(PortalAuditEvent.action == 'client_pii.export')
        )
    ).scalar_one()
    assert audit.actor_user_id == 201
    assert audit.portal_account_id == 1
    assert audit.metadata_json == {'table': 'clients', 'format': 'csv'}


@pytest.mark.asyncio
async def test_company_scoped_csv_export_uses_jwt_branch_scope(async_session):
    await _seed_client_pii_scope(async_session)
    async_session.add_all([
        GoodCatalog(
            company_id=1,
            good_id=10,
            title='Tenant A Shampoo',
            updated_at=datetime.utcnow(),
        ),
        GoodCatalog(
            company_id=2,
            good_id=20,
            title='Tenant B Shampoo',
            updated_at=datetime.utcnow(),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    token = create_access_token(201, 'manager')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/export/csv/good_catalog', headers={'Authorization': f'Bearer {token}'})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert 'Tenant A Shampoo' in response.text
    assert 'Tenant B Shampoo' not in response.text


@pytest.mark.asyncio
async def test_clients_csv_export_rejects_non_user_or_viewer_access(async_session, monkeypatch):
    import auth_deps

    await _seed_client_pii_scope(async_session)

    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'API_KEY', 'legacy-api-key')
    monkeypatch.setattr(auth_deps, 'API_KEY', 'legacy-api-key')
    viewer_token = create_access_token(202, 'viewer')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        anonymous = await client.get('/export/csv/clients')
        api_key_only = await client.get('/export/csv/clients', headers={'X-API-Key': 'legacy-api-key'})
        viewer = await client.get(
            '/export/csv/clients',
            headers={'Authorization': f'Bearer {viewer_token}'},
        )

    app.dependency_overrides.clear()

    assert anonymous.status_code == 401
    assert api_key_only.status_code == 401
    assert api_key_only.json()['detail'] == 'Authentication required'
    assert viewer.status_code == 403
    assert viewer.json()['detail'] == 'Insufficient permissions'


@pytest.mark.asyncio
async def test_sync_trigger_queues_job(async_session, monkeypatch):
    captured = {}

    class DummyJob:
        id = 42
        mode = 'incremental'
        initiator = 'dashboard'
        portal_account_id = None
        credential_id = None
        company_ids = []

    async def fake_enqueue(self, db, mode, initiator, **kwargs):
        captured['mode'] = mode
        captured['initiator'] = initiator
        captured.update(kwargs)
        return DummyJob()

    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    monkeypatch.setattr(api.SyncJobService, 'async_enqueue_job', fake_enqueue)
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/sync/trigger',
            json={'mode': 'incremental', 'initiator': 'dashboard'},
            headers={'X-Sync-Token': 'test-sync-token'},
        )

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        'status': 'queued',
        'job_id': 42,
        'mode': 'incremental',
        'initiator': 'dashboard',
        'portal_account_id': None,
        'credential_id': None,
        'company_ids': [],
    }
    assert captured == {
        'mode': 'incremental',
        'initiator': 'dashboard',
        'portal_account_id': None,
        'credential_id': None,
        'company_ids': None,
    }


@pytest.mark.asyncio
async def test_sync_trigger_rejects_unconfigured_token(async_session, monkeypatch):
    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', '')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        no_header = await client.post('/sync/trigger', json={'mode': 'incremental'})
        empty_header = await client.post(
            '/sync/trigger',
            json={'mode': 'incremental'},
            headers={'X-Sync-Token': ''},
        )
        arbitrary_header = await client.post(
            '/sync/trigger',
            json={'mode': 'incremental'},
            headers={'X-Sync-Token': 'anything'},
        )

    app.dependency_overrides.clear()

    assert no_header.status_code == 401
    assert empty_header.status_code == 401
    assert arbitrary_header.status_code == 401


@pytest.mark.asyncio
async def test_sync_routes_reject_missing_or_wrong_token(async_session, monkeypatch):
    async def override_db():
        yield async_session

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        missing_trigger = await client.post('/sync/trigger', json={'mode': 'incremental'})
        wrong_trigger = await client.post(
            '/sync/trigger',
            json={'mode': 'incremental'},
            headers={'X-Sync-Token': 'wrong'},
        )
        missing_status = await client.get('/sync/status')
        wrong_status = await client.get('/sync/status', headers={'X-Sync-Token': 'wrong'})

    app.dependency_overrides.clear()

    assert missing_trigger.status_code == 401
    assert wrong_trigger.status_code == 401
    assert missing_status.status_code == 401
    assert wrong_status.status_code == 401


@pytest.mark.asyncio
async def test_sync_status_preserves_payload_and_offloads_sync_lookup(async_session, monkeypatch):
    async def override_db():
        yield async_session

    sync_payload = {'status': 'idle'}
    queue_payload = {'queued': 0, 'running': 0}
    offloaded = []

    def fake_get_sync_status():
        return sync_payload

    async def fake_to_thread(func, *args):
        offloaded.append(func)
        return func(*args)

    class FakeSyncJobService:
        async def async_get_status_payload(self, db, *, portal_account_id=None):
            assert db is async_session
            assert portal_account_id is None
            return queue_payload

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', 'test-sync-token')
    monkeypatch.setattr(api, 'get_sync_status', fake_get_sync_status)
    monkeypatch.setattr(api.asyncio, 'to_thread', fake_to_thread)
    monkeypatch.setattr(api, 'SyncJobService', FakeSyncJobService)
    app.dependency_overrides[api.get_async_db] = override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/sync/status', headers={'X-Sync-Token': 'test-sync-token'})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {'sync': sync_payload, 'queue': queue_payload}
    assert offloaded == [fake_get_sync_status]


@pytest.mark.asyncio
async def test_csv_export_streams_rows(async_session):
    async_session.add(GoodTransaction(id=1, company_id=7, type_id=1, amount=2.0, cost=10.0))
    async_session.add(GoodTransaction(id=2, company_id=7, type_id=3, amount=1.0, cost=5.0))
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/export/csv/goods_transactions')

    app.dependency_overrides.clear()

    assert response.status_code == 200
    content = response.text
    assert (
        'id,external_id,source_type,document_id,type_id,good_id,good_title,storage_id,storage_title,'
        'amount,cost_per_unit,cost,discount,master_id,client_id,company_id,date'
    ) in content
    assert '1,,yclients,,1,,,,,2.0,,10.0,,,,7,' in content
    assert '2,,yclients,,3,,,,,1.0,,5.0,,,,7,' in content


@pytest.mark.asyncio
async def test_goods_transactions_endpoint_no_longer_depends_on_date_params(async_session):
    async_session.add(
        GoodTransaction(
            id=10,
            company_id=9,
            type_id=1,
            good_id=99,
            good_title='Archived pomade',
            amount=1.0,
            cost=1.0,
            date=datetime(2026, 1, 2, 3, 4, 5),
        )
    )
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/goods_transactions', params={'company_id': 9, 'date_from': '2026-01-01'})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload['total'] == 1
    assert payload['data'][0]['id'] == 10
    assert payload['data'][0]['good_title'] == 'Archived pomade'
    assert payload['data'][0]['date'] == '2026-01-02T03:04:05'


@pytest.mark.asyncio
async def test_services_endpoint_reads_branch_scoped_catalog(async_session):
    async_session.add(Group(id=1, title='Group'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add_all([
        ServiceCatalog(
            company_id=1,
            service_id=10,
            title='Воск',
            price_min=500.0,
            duration=900,
            category_title='Уход',
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        ),
        ServiceCatalog(
            company_id=2,
            service_id=10,
            title='Воск',
            price_min=550.0,
            duration=900,
            category_title='Уход',
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/services', params={'company_id': 2})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload['total'] == 1
    assert payload['data'][0]['id'] == 10
    assert payload['data'][0]['company_id'] == 2
    assert payload['data'][0]['price_min'] == 550.0


@pytest.mark.asyncio
async def test_goods_endpoint_reads_branch_scoped_catalog(async_session):
    async_session.add(Group(id=1, title='Group'))
    async_session.add(Company(id=1, title='Salon 1', group_id=1))
    async_session.add(Company(id=2, title='Salon 2', group_id=1))
    async_session.add_all([
        GoodCatalog(
            company_id=1,
            good_id=100,
            title='Paste A',
            cost=1000.0,
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        ),
        GoodCatalog(
            company_id=2,
            good_id=100,
            title='Paste A',
            cost=1200.0,
            updated_at=datetime(2026, 1, 1, 0, 0, 0),
        ),
    ])
    await async_session.commit()

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.get('/goods', params={'company_id': 1})

    app.dependency_overrides.clear()

    assert response.status_code == 200
    payload = response.json()
    assert payload['total'] == 1
    assert payload['data'][0]['good_id'] == 100
    assert payload['data'][0]['company_id'] == 1
    assert payload['data'][0]['cost'] == 1000.0
