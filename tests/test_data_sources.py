from data_sources import YClientsDataSourceAdapter


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
