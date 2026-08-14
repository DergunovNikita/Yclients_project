"""Shared security rules for sync-control endpoints."""

import hmac

from fastapi import HTTPException


def validate_sync_token(presented_token: str | None, configured_token: str | None) -> None:
    """Reject sync access unless a non-empty configured token matches."""
    normalized_token = (configured_token or '').strip()
    if not normalized_token or not hmac.compare_digest(presented_token or '', normalized_token):
        raise HTTPException(status_code=401, detail='Invalid sync token')
