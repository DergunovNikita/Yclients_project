from datetime import datetime

import pytest
from sqlalchemy import select

from data_sources import YClientsDataSourceAdapter
from models import Company, Group, PortalAccount, PortalBranch


class FakeYClientsAPI:
    def __init__(self, partner_token, login, password, groups=None, auth=True):
        self.partner_token = partner_token
        self.login = login
        self.password = password
        self.groups = groups or []
        self.auth = auth

    def authenticate(self):
        return self.auth

    def get_groups(self):
        return self.groups


def test_yclients_adapter_authenticates_successfully():
    adapter = YClientsDataSourceAdapter(
        'partner',
        'login',
        'password',
        api_factory=lambda *args, **kwargs: FakeYClientsAPI(*args, auth=True, **kwargs),
    )

    assert adapter.authenticate() is True


def test_yclients_adapter_authentication_failure():
    adapter = YClientsDataSourceAdapter(
        'partner',
        'login',
        'wrong',
        api_factory=lambda *args, **kwargs: FakeYClientsAPI(*args, auth=False, **kwargs),
    )

    assert adapter.authenticate() is False


def test_yclients_adapter_lists_branches_from_groups():
    adapter = YClientsDataSourceAdapter(
        'partner',
        'login',
        'password',
        api_factory=lambda *args, **kwargs: FakeYClientsAPI(
            *args,
            groups=[
                {
                    'id': 10,
                    'title': 'Network',
                    'companies': [
                        {'id': 101, 'title': 'Branch A'},
                        {'id': 102, 'title': 'Branch B'},
                    ],
                }
            ],
            **kwargs,
        ),
    )

    assert [item.as_payload() for item in adapter.list_branches()] == [
        {'group_id': 10, 'group_title': 'Network', 'company_id': 101, 'title': 'Branch A'},
        {'group_id': 10, 'group_title': 'Network', 'company_id': 102, 'title': 'Branch B'},
    ]


def test_yclients_adapter_handles_empty_groups():
    adapter = YClientsDataSourceAdapter(
        'partner',
        'login',
        'password',
        api_factory=lambda *args, **kwargs: FakeYClientsAPI(*args, groups=[], **kwargs),
    )

    assert adapter.list_branches() == []


@pytest.mark.asyncio
async def test_yclients_materialize_branches_scopes_external_ids_by_tenant(async_session):
    async_session.add_all([
        PortalAccount(id=1, label='Tenant A', created_at=datetime.utcnow()),
        PortalAccount(id=2, label='Tenant B', created_at=datetime.utcnow()),
    ])
    await async_session.flush()

    def adapter_for(title):
        return YClientsDataSourceAdapter(
            'partner',
            'login',
            'password',
            api_factory=lambda *args, **kwargs: FakeYClientsAPI(
                *args,
                groups=[
                    {
                        'id': 10,
                        'title': 'Network',
                        'companies': [
                            {'id': 101, 'title': title},
                        ],
                    }
                ],
                **kwargs,
            ),
        )

    tenant_a_ids = await adapter_for('Branch A').materialize_branches(async_session, 1, [101])
    tenant_b_ids = await adapter_for('Branch B').materialize_branches(async_session, 2, [101])
    await async_session.commit()

    assert tenant_a_ids != tenant_b_ids

    companies = (await async_session.execute(select(Company).order_by(Company.portal_account_id))).scalars().all()
    assert [(company.portal_account_id, company.external_id, company.id) for company in companies] == [
        (1, 101, tenant_a_ids[0]),
        (2, 101, tenant_b_ids[0]),
    ]
    assert [company.group_id for company in companies] != [None, None]

    groups = (await async_session.execute(select(Group).order_by(Group.portal_account_id))).scalars().all()
    assert [(group.portal_account_id, group.external_id) for group in groups] == [(1, 10), (2, 10)]

    branches = (await async_session.execute(select(PortalBranch).order_by(PortalBranch.portal_account_id))).scalars().all()
    assert [(branch.portal_account_id, branch.company_id) for branch in branches] == [
        (1, tenant_a_ids[0]),
        (2, tenant_b_ids[0]),
    ]
