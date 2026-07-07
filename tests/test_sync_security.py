import pytest
from fastapi import HTTPException

import dashboard_routes


def test_dashboard_sync_token_rejects_unconfigured_token(monkeypatch):
    monkeypatch.setattr(dashboard_routes, 'SYNC_API_TOKEN', '')

    with pytest.raises(HTTPException) as exc_info:
        dashboard_routes._require_sync_token('anything')

    assert exc_info.value.status_code == 401


def test_dashboard_sync_token_rejects_missing_or_wrong_token(monkeypatch):
    monkeypatch.setattr(dashboard_routes, 'SYNC_API_TOKEN', 'test-sync-token')

    for token in (None, '', 'wrong'):
        with pytest.raises(HTTPException) as exc_info:
            dashboard_routes._require_sync_token(token)
        assert exc_info.value.status_code == 401


def test_dashboard_sync_token_accepts_correct_token(monkeypatch):
    monkeypatch.setattr(dashboard_routes, 'SYNC_API_TOKEN', 'test-sync-token')

    assert dashboard_routes._require_sync_token('test-sync-token') is None
