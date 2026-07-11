from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_database_url, init_database, run_migrations
from models import SYSTEM_SCHEMA
from scripts.bootstrap_db import bootstrap_database
from sqlalchemy import inspect
from sqlalchemy.exc import SQLAlchemyError


def _table_names(database) -> set[str]:
    inspector = inspect(database.engine)
    names = set(inspector.get_table_names())
    try:
        names.update(f'{SYSTEM_SCHEMA}.{name}' for name in inspector.get_table_names(schema=SYSTEM_SCHEMA))
    except SQLAlchemyError:
        pass
    return names


def main():
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1
    tables = _table_names(database)
    if 'alembic_version' not in tables:
        if tables:
            print(
                'Database has application tables but no alembic_version; refusing automatic migration. '
                'Run the documented bootstrap/stamp procedure manually after verifying the schema.'
            )
            return 1
        bootstrap_database(database)
        print("Fresh database bootstrapped and stamped at Alembic head")
        return 0
    run_migrations(build_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    print("Migrations applied OK")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
