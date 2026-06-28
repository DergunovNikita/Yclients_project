"""Append-only portal audit logging helpers."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from models import PortalAuditEvent


async def log_portal_audit(
    db: AsyncSession,
    *,
    actor_user_id: int | None,
    portal_account_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        PortalAuditEvent(
            actor_user_id=actor_user_id,
            portal_account_id=portal_account_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            metadata_json=metadata or {},
            created_at=datetime.utcnow(),
        )
    )


def log_portal_audit_sync(
    db: Session,
    *,
    actor_user_id: int | None,
    portal_account_id: int | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    commit: bool = False,
) -> None:
    db.add(
        PortalAuditEvent(
            actor_user_id=actor_user_id,
            portal_account_id=portal_account_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            metadata_json=metadata or {},
            created_at=datetime.utcnow(),
        )
    )
    if commit:
        db.commit()
