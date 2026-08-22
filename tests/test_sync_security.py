import pytest
from fastapi import HTTPException

import api


def test_sync_token_validation_contract(monkeypatch):
    invalid_cases = [
        ('', 'anything'),
        ('   ', 'anything'),
        ('test-sync-token', None),
        ('test-sync-token', ''),
        ('test-sync-token', 'wrong'),
        ('test-sync-token', ' test-sync-token '),
    ]
    for configured_token, presented_token in invalid_cases:
        monkeypatch.setattr(api, 'SYNC_API_TOKEN', configured_token)
        with pytest.raises(HTTPException) as exc_info:
            api.require_sync_token(presented_token)
        assert exc_info.value.status_code == 401
        assert exc_info.value.detail == 'Invalid sync token'

    monkeypatch.setattr(api, 'SYNC_API_TOKEN', '  test-sync-token  ')
    assert api.require_sync_token('test-sync-token') is None
