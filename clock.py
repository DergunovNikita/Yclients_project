"""Shared wall clock."""

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Naive UTC now: the timestamp convention of every DateTime column (what `datetime.utcnow()` returned)."""
    return datetime.now(UTC).replace(tzinfo=None)
