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
    engine = create_async_engine(url, pool_pre_ping=True, pool_recycle=300)
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
