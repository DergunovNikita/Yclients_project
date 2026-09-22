"""
Database helpers for PostgreSQL connectivity and migrations.

Sync engine (psycopg2) is used by ETL pipeline / worker.
Async engine (asyncpg) is used by FastAPI endpoints.
"""
from collections.abc import AsyncGenerator, Generator
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker


def _build_url(drivername: str, host: str, port: int, name: str, user: str, password: str = '') -> str:
    return URL.create(
        drivername,
        username=user,
        password=password or None,
        host=host,
        port=int(port),
        database=name,
    ).render_as_string(hide_password=False)


def build_database_url(host: str, port: int, name: str, user: str, password: str = '') -> str:
    return _build_url('postgresql+psycopg2', host, port, name, user, password)


def build_async_database_url(host: str, port: int, name: str, user: str, password: str = '') -> str:
    return _build_url('postgresql+asyncpg', host, port, name, user, password)


def alembic_config_value(value: str) -> str:
    """Escape percent signs for Alembic's ConfigParser-backed settings."""
    return value.replace('%', '%%')


def quote_identifier(dialect, value: str) -> str:
    """Validate and quote an identifier for raw DDL (views, TRUNCATE, CREATE SCHEMA).

    Raises:
        ValueError: if the identifier is not a plain alphanumeric/underscore name.
    """
    if not value.replace('_', '').isalnum() or not value[0].isalpha():
        raise ValueError(f'Unsafe SQL identifier: {value!r}')
    return dialect.identifier_preparer.quote(value)


class Database:
    """Sync connection wrapper — used by ETL pipeline and worker."""

    def __init__(self, host: str, port: int, name: str, user: str, password: str = ''):
        self.database_url = build_database_url(host, port, name, user, password)
        self.engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        self.SessionLocal = sessionmaker(bind=self.engine)

    def get_session(self) -> Generator[Session, None, None]:
        db = self.SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def get_db(self) -> Session:
        return self.SessionLocal()

    def test_connection(self) -> bool:
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            print("Connection to database successful")
            return True
        except SQLAlchemyError as exc:
            print(f"Database connection error: {exc}")
            return False


# --- Sync singleton (ETL / worker) ---

db_instance: Database | None = None


def init_database(host: str, port: int, name: str, user: str, password: str = '') -> Database:
    global db_instance
    db_instance = Database(host, port, name, user, password)
    return db_instance


def get_db() -> Generator[Session, None, None]:
    if db_instance is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    yield from db_instance.get_session()


# --- Async singleton (FastAPI) ---

_async_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_async_database(host: str, port: int, name: str, user: str, password: str = '') -> None:
    global _async_session_factory
    url = build_async_database_url(host, port, name, user, password)
    # Dashboard plans reach cost estimates in the millions because the fact filters nest
    # correlated EXISTS, so PostgreSQL JIT-compiles them with inlining and optimisation:
    # measured on production, one such query spends 2203 ms, of which 2157 ms is LLVM, to
    # then execute in 13 ms. Only the API sees these plans, so the ETL engine keeps JIT.
    #
    # Pool sized against Postgres max_connections=100, shared with the sync worker and any
    # ad hoc psql/migration session (uvicorn runs this API as a single process with no
    # --workers, so this engine's pool is the whole app's ceiling, not a per-process slice):
    #   - sync worker: one long-lived engine at rest (Database sets no pool_size/max_overflow,
    #     so it gets SQLAlchemy's default 5 + 10 overflow = 15) plus, while a job is actively
    #     running, one more engine opened by sync_orchestrator.run_sync_job for the advisory
    #     lock and run tracking (another 15 ceiling) -> ~30 worst case.
    #   - ad hoc access: alembic migrations, one-off scripts, a human psql session -> budget 10.
    #   - remainder for this engine: 100 - 30 - 10 = 60. Split as pool_size=15 (always-open
    #     baseline) + max_overflow=15 (burst room for concurrent heavy dashboard reports) = 30,
    #     so API (30) + worker (30) = 60 -- the low end of the ~60-70 ceiling for the two
    #     together, leaving margin for migrations/psql and Postgres's own reserved connections.
    # pool_timeout=10 fails a queued request in 10s instead of SQLAlchemy's 30s default, so a
    # cheap call like /auth/me errors quickly rather than stalling behind a few 6s dashboard
    # reports holding every pooled connection.
    #
    # plan_cache_mode=force_custom_plan is load-bearing, not tuning. asyncpg prepares and caches
    # a statement per connection, and PostgreSQL's default `auto` mode plans a prepared statement
    # custom for its first 5 executions, then may switch to a GENERIC plan on the 6th. These fact
    # queries are dominated by the parameter values (branch scope, date window), which a generic
    # plan cannot see, so the switch is catastrophic. Measured on a copy of production, from a
    # cold process, /dashboard/widget/plan_fact over full history:
    #   run 1-5: 5.6s 5.9s 7.0s 7.1s 7.2s   ->   run 6+: 19.6s 20.7s 20.6s, and it stays there
    # Because the API holds connections open, every connection falls off that cliff and never
    # recovers. With force_custom_plan the same endpoint is a flat 4.6-4.9s across all 8 runs.
    # This predates the pool/query work in this file; it is not caused by it. Same family as the
    # jit: off note above, and for the same underlying reason. Replanning costs a few ms against
    # multi-second queries. Do not remove without re-measuring the 6th consecutive request.
    engine = create_async_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=15,
        max_overflow=15,
        pool_timeout=10,
        connect_args={'server_settings': {'jit': 'off', 'plan_cache_mode': 'force_custom_plan'}},
    )
    _async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    if _async_session_factory is None:
        raise RuntimeError("Async database not initialized. Call init_async_database() first.")
    async with _async_session_factory() as session:
        yield session


# --- Migrations ---

def run_migrations(database_url: str, revision: str = 'head') -> None:
    config = Config(str(Path(__file__).resolve().parent / 'alembic.ini'))
    config.set_main_option('script_location', str(Path(__file__).resolve().parent / 'alembic'))
    config.set_main_option('sqlalchemy.url', alembic_config_value(database_url))
    command.upgrade(config, revision)
