"""Delete @portal.local portal accounts, clear initial passwords, reset id sequence."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select, text, update

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_async_database_url
from models import PortalUser, Staff


async def main() -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(build_async_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        pwd_count = (
            await db.execute(
                select(func.count()).select_from(PortalUser).where(PortalUser.initial_password.is_not(None))
            )
        ).scalar_one()
        users = (
            await db.execute(select(PortalUser).where(PortalUser.email.ilike('%@portal.local')))
        ).scalars().all()
        user_ids = [user.id for user in users]

        if user_ids:
            await db.execute(
                update(Staff).where(Staff.portal_user_id.in_(user_ids)).values(portal_user_id=None)
            )
            for user in users:
                await db.delete(user)

        await db.execute(
            update(PortalUser).where(PortalUser.initial_password.is_not(None)).values(initial_password=None)
        )
        await db.execute(
            text(
                "SELECT setval("
                "pg_get_serial_sequence('system.portal_users', 'id'), "
                "COALESCE((SELECT MAX(id) FROM system.portal_users), 1), "
                "true)"
            )
        )
        await db.commit()
        print(f'Cleared initial_password for {pwd_count} user(s).')
        print(f'Deleted {len(users)} @portal.local account(s).')

    await engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
