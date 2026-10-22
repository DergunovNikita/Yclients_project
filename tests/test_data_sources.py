
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from clock import utcnow
from data_sources import SOURCE_YCLIENTS, YClientsDataSourceAdapter, normalize_source_type
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


def test_a_failed_group_request_is_not_an_empty_network():
    class FailingAPI(FakeYClientsAPI):
        def get_groups(self):
            return None

    adapter = YClientsDataSourceAdapter('partner', 'login', 'password', api_factory=FailingAPI)

    with pytest.raises(HTTPException) as caught:
        adapter.list_branches()

    assert caught.value.status_code == 400
    assert caught.value.detail == 'Data source authentication failed'


@pytest.mark.asyncio
async def test_yclients_materialize_branches_scopes_external_ids_by_tenant(async_session):
    async_session.add_all([
        PortalAccount(id=1, label='Tenant A', created_at=utcnow()),
        PortalAccount(id=2, label='Tenant B', created_at=utcnow()),
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

    branches = (
        (await async_session.execute(select(PortalBranch).order_by(PortalBranch.portal_account_id))).scalars().all()
    )
    assert [(branch.portal_account_id, branch.company_id) for branch in branches] == [
        (1, tenant_a_ids[0]),
        (2, tenant_b_ids[0]),
    ]


def test_normalize_source_type_rejects_an_unsupported_source():
    """`SUPPORTED_SOURCE_TYPES` is the gate every onboarding path goes through.

    This assertion used to live in tests/test_file_import.py, which named `file_import`
    specifically; that module is gone, but the gate it guarded still is the only thing
    stopping an unknown `source_type` from reaching an adapter that cannot serve it.
    """
    with pytest.raises(HTTPException) as excinfo:
        normalize_source_type('gestionale_csv')
    assert excinfo.value.status_code == 400
    assert 'Unsupported source_type' in excinfo.value.detail

    # The default and the only supported value both resolve without raising.
    assert normalize_source_type(None) == SOURCE_YCLIENTS
    assert normalize_source_type(' YClients ') == SOURCE_YCLIENTS


@pytest.mark.asyncio
async def test_materialize_branches_uses_the_prefetched_listing_when_given(async_session):
    async_session.add(PortalAccount(id=1, label='Tenant A', created_at=utcnow()))
    await async_session.flush()
    listing = [{'id': 10, 'title': 'Network', 'companies': [{'id': 101, 'title': 'Branch A'}]}]
    branches = YClientsDataSourceAdapter(
        'partner', 'login', 'password',
        api_factory=lambda *args, **kwargs: FakeYClientsAPI(*args, groups=listing, **kwargs),
    ).list_branches()
    # A second adapter would see an empty network: the company can only come from the prefetched listing.
    empty = YClientsDataSourceAdapter(
        'partner', 'login', 'password',
        api_factory=lambda *args, **kwargs: FakeYClientsAPI(*args, groups=[], **kwargs),
    )

    company_ids = await empty.materialize_branches(async_session, 1, [101], branches)
    await async_session.commit()

    company = await async_session.get(Company, company_ids[0])
    assert (company.external_id, company.title, company.portal_account_id) == (101, 'Branch A', 1)
