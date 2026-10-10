"""Shared security rules for sync-control endpoints."""

import hmac

from fastapi import HTTPException


def secure_equals(presented: str | None, expected: str) -> bool:
    """Constant-time string comparison that also accepts non-ASCII input (bytes, not str)."""
    return hmac.compare_digest((presented or '').encode('utf-8'), expected.encode('utf-8'))


def validate_sync_token(presented_token: str | None, configured_token: str | None) -> None:
    """Reject sync access unless a non-empty configured token matches."""
    normalized_token = (configured_token or '').strip()
    if not normalized_token or not secure_equals(presented_token, normalized_token):
        raise HTTPException(status_code=401, detail='Invalid sync token')
