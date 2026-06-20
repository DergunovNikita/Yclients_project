"""Provision portal accounts for all staff without login."""
from __future__ import annotations

import asyncio

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_async_database_url, init_database
from portal_account_provision import provision_all_unlinked_staff


async def main() -> int:
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    engine = create_async_engine(build_async_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with session_factory() as db:
        created, errors = await provision_all_unlinked_staff(db, allowed_company_ids=None)
        await db.commit()
        for item in created:
            print(
                f'created staff_id={item.staff_id} user_id={item.user_id} '
                f'email={item.email} password={item.initial_password}'
            )
        for error in errors:
            print(f'error: {error}')
        print(f'Done. Created {len(created)} account(s), {len(errors)} error(s).')

    await engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
