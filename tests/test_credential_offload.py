"""Blocking YClients calls in onboarding / credential routes must stay off the event loop."""

import threading
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import func, select

import onboarding_routes
import yclients_credential_routes
from models import Company, Group, PortalAccount, PortalUser, YClientsCredential
from onboarding_routes import OnboardingCredentialsRequest
from yclients_credential_routes import YClientsCredentialCreateRequest

GROUPS = [{'id': 10, 'title': 'Network', 'companies': [{'id': 101, 'title': 'Branch A'}]}]


@pytest.fixture
def fake_api(monkeypatch):
    calls = {'authenticate': [], 'get_groups': []}

    class FakeYClientsAPI:
        def __init__(self, partner_token, login, password):
            pass

        def authenticate(self):
            calls['authenticate'].append(threading.get_ident())
            return True

        def get_groups(self):
            calls['get_groups'].append(threading.get_ident())
            return GROUPS

    monkeypatch.setattr('data_sources.YClientsAPI', FakeYClientsAPI)
    monkeypatch.setenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', 'test-encryption-key')
    return calls


@pytest_asyncio.fixture
async def owner(async_session):
    async_session.add(PortalAccount(id=1, label='Tenant', created_at=datetime.utcnow()))
    await async_session.flush()
    return PortalUser(id=1, portal_account_id=1, email='owner@example.com', role='owner', full_name='Owner')


@pytest.mark.asyncio
async def test_onboarding_credentials_talk_to_yclients_off_the_loop(async_session, owner, fake_api):
    response = await onboarding_routes.onboarding_credentials(
        OnboardingCredentialsRequest(partner_token='p', login='l', password='pw'),
        user=owner,
        db=async_session,
    )

    assert [item['company_id'] for item in response['data']['companies']] == [101]
    loop_thread = threading.get_ident()
    assert fake_api['authenticate'] and fake_api['get_groups']
    assert loop_thread not in fake_api['authenticate'] + fake_api['get_groups']


@pytest.mark.asyncio
async def test_admin_create_logs_in_once_and_lists_branches_once(async_session, owner, fake_api):
    response = await yclients_credential_routes.admin_create_yclients_credentials(
        YClientsCredentialCreateRequest(title='Main', partner_token='p', login='l', password='pw'),
        x_portal_account_id=None,
        actor=owner,
        db=async_session,
    )

    assert response['data']['id']
    assert len(fake_api['authenticate']) == 1
    assert len(fake_api['get_groups']) == 1
    loop_thread = threading.get_ident()
    assert loop_thread not in fake_api['authenticate'] + fake_api['get_groups']
    assert await async_session.scalar(select(func.count()).select_from(YClientsCredential)) == 1
    assert await async_session.scalar(select(func.count()).select_from(Company)) == 1
    assert await async_session.scalar(select(func.count()).select_from(Group)) == 1
