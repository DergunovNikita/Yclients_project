"""Backfill staff rows for existing manager/viewer portal users."""
from __future__ import annotations

import asyncio

from sqlalchemy import select

from auth_service import load_user_branch_ids
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_async_database_url, init_database
from models import PortalUser
from portal_staff_sync import portal_user_syncs_to_staff, sync_portal_user_staff


async def main() -> int:
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(build_async_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        users = (await db.execute(select(PortalUser).order_by(PortalUser.id.asc()))).scalars().all()
        synced = 0
        for user in users:
            if not portal_user_syncs_to_staff(user):
                continue
            branch_ids = await load_user_branch_ids(db, user.id)
            if not branch_ids:
                print(f"skip user id={user.id} ({user.full_name}): no branches")
                continue
            await sync_portal_user_staff(db, user, branch_ids)
            synced += 1
            print(f"synced user id={user.id} ({user.full_name}) branches={branch_ids}")
        await db.commit()
        print(f"Done. Synced {synced} portal user(s).")

    await engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
