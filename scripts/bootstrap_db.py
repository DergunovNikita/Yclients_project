"""Bootstrap a brand-new database to the current schema and stamp it at Alembic head.

Running ``alembic upgrade head`` from scratch is unusable in this project: the
``0001`` baseline builds the public tables from the *current* models, so later
add-column migrations (``source_type``, ``is_demo``, …) collide with columns that
already exist. For a fresh database (e.g. the isolated demo instance) create the
full current schema from metadata and stamp head, so future migrations still
apply incrementally.

Idempotent: safe to re-run (schema/tables are created if missing, version is set
to head). Run: ``python -m scripts.bootstrap_db`` / ``python scripts/bootstrap_db.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from sqlalchemy import text  # noqa: E402

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER  # noqa: E402
from database import alembic_config_value, build_database_url, init_database  # noqa: E402
from models import SYSTEM_SCHEMA, Base  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent


def bootstrap_database(database) -> None:
    with database.engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS {SYSTEM_SCHEMA}'))
    Base.metadata.create_all(database.engine)

    cfg = Config(str(REPO_ROOT / 'alembic.ini'))
    cfg.set_main_option('script_location', str(REPO_ROOT / 'alembic'))
    cfg.set_main_option(
        'sqlalchemy.url',
        alembic_config_value(build_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)),
    )
    command.stamp(cfg, 'head')


def main() -> int:
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    bootstrap_database(database)
    print(f'Schema created and stamped at Alembic head for database "{DB_NAME}"')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
