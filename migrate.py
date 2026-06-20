from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_database_url, init_database, run_migrations


def main():
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1
    run_migrations(build_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    print("Migrations applied OK")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
