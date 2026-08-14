import pytest
from fastapi import HTTPException

import api
import dashboard_routes


@pytest.mark.parametrize(
    ('module', 'validator'),
    [
        (api, api.require_sync_token),
        (dashboard_routes, dashboard_routes._require_sync_token),
    ],
)
def test_sync_token_validation_contract(monkeypatch, module, validator):
    invalid_cases = [
        ('', 'anything'),
        ('   ', 'anything'),
        ('test-sync-token', None),
        ('test-sync-token', ''),
        ('test-sync-token', 'wrong'),
        ('test-sync-token', ' test-sync-token '),
    ]
    for configured_token, presented_token in invalid_cases:
        monkeypatch.setattr(module, 'SYNC_API_TOKEN', configured_token)
        with pytest.raises(HTTPException) as exc_info:
            validator(presented_token)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == 'Invalid sync token'

    monkeypatch.setattr(module, 'SYNC_API_TOKEN', '  test-sync-token  ')
    assert validator('test-sync-token') is None
