
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from clock import utcnow
from data_sources import SOURCE_YCLIENTS, YClientsDataSourceAdapter, normalize_source_type
from models import Company, Group, PortalAccount, PortalBranch, PortalUser


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
async def test_a_tenant_takes_over_a_branch_of_an_empty_tenant_with_its_history_not_a_copy(async_session):
    """The location has a `Company` with an external id: the row changes tenant, a second one would read it twice."""
    async_session.add_all([
        PortalAccount(id=1, label='Prepared tenant', created_at=utcnow()),
        PortalAccount(id=2, label='Owner tenant', created_at=utcnow()),
    ])
    await async_session.flush()

    def adapter_for(title):
        return YClientsDataSourceAdapter(
            'partner',
            'login',
            'password',
            api_factory=lambda *args, **kwargs: FakeYClientsAPI(
                *args,
                groups=[{'id': 10, 'title': 'Network', 'companies': [{'id': 101, 'title': title}]}],
                **kwargs,
            ),
        )

    prepared_ids = await adapter_for('Branch A').materialize_branches(async_session, 1, [101])
    taken_ids = await adapter_for('Branch B').materialize_branches(async_session, 2, [101])
    await async_session.commit()

    assert taken_ids == prepared_ids
    company = (await async_session.execute(select(Company))).scalar_one()
    assert (company.portal_account_id, company.external_id, company.title) == (2, 101, 'Branch B')
    branch = (await async_session.execute(select(PortalBranch))).scalar_one()
    assert (branch.portal_account_id, branch.company_id) == (2, company.id)
    group = await async_session.get(Group, company.group_id)
    assert (group.portal_account_id, group.external_id) == (2, 10)


@pytest.mark.asyncio
async def test_a_company_row_of_a_tenant_with_users_is_not_taken_only_copied(async_session):
    """A live tenant's sync creates rows for the whole network without a `PortalBranch`: they are not up for grabs."""
    async_session.add_all([
        PortalAccount(id=1, label='Live tenant', created_at=utcnow()),
        PortalAccount(id=2, label='Owner tenant', created_at=utcnow()),
    ])
    await async_session.flush()
    async_session.add(PortalUser(
        id=1, portal_account_id=1, email='owner1@example.com', password_hash='x', role='owner', is_active=True,
        created_at=utcnow(),
    ))
    live = YClientsDataSourceAdapter(
        'partner', 'login', 'password',
        api_factory=lambda *args, **kwargs: FakeYClientsAPI(
            *args, groups=[{'id': 10, 'title': 'Network', 'companies': [{'id': 101, 'title': 'A'}]}], **kwargs
        ),
    )
    await live.materialize_branches(async_session, 1, [101])
    await async_session.execute(delete(PortalBranch))
    await async_session.commit()

    taken = await live.materialize_branches(async_session, 2, [101])
    await async_session.commit()

    companies = (await async_session.execute(select(Company).order_by(Company.id))).scalars().all()
    assert [(c.portal_account_id, c.external_id) for c in companies] == [(1, 101), (2, 101)]
    assert taken == [companies[1].id]


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
