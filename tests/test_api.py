from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient

import api
from api import app
from auth_service import create_access_token, hash_password
from models import (
    Company,
    GoodCatalog,
    GoodTransaction,
    Group,
    PortalAccount,
    PortalBranch,
    PortalUser,
    PortalUserBranch,
    ServiceCatalog,
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
        exported = await client.get(
            '/export/csv/goods_transactions',
            headers={'Authorization': f'Bearer {token}'},
        )

    app.dependency_overrides.clear()

    assert companies.status_code == 200
    assert [row['id'] for row in companies.json()['data']] == [1]
    assert forbidden.status_code == 403
    assert '1,,1,,,,,2.0,,10.0,,,,1' in exported.text
    assert '2,,1,,,,,1.0,,20.0,,,,2' not in exported.text


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
    assert 'id,document_id,type_id,good_id,good_title,storage_id,storage_title,amount,cost_per_unit,cost,discount,master_id,client_id,company_id' in content
    assert '1,,1,,,,,2.0,,10.0,,,,7' in content
    assert '2,,3,,,,,1.0,,5.0,,,,7' in content


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
