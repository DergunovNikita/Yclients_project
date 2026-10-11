"""Fakes and loaders shared by several test modules."""

import importlib.util
from contextlib import contextmanager
from functools import cache
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from auth_service import hash_password
from models import Base

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / 'alembic' / 'versions'


@cache
def cached_password_hash(password: str) -> str:
    """bcrypt hash of `password`, computed once per process.

    A real hash costs ~0.2 s, and fixtures that only need a stored hash (not a salted fresh one)
    would pay it on every test. Tests that check hashing itself call `hash_password` directly.
    """
    return hash_password(password)


@contextmanager
def sqlite_session_with_system(tables):
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=tables)
    session_local = sessionmaker(bind=engine)
    db = session_local()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def load_migration(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, MIGRATIONS_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeConnection:
    def __init__(self):
        self.closed = False

    def execution_options(self, **_kwargs):
        return self

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self):
        self.closed = False

    def commit(self):
        pass

    def close(self):
        self.closed = True


class FakeEngine:
    def connect(self):
        return FakeConnection()


class FakeDatabase:
    """Engine plus a single control session, as `sync_orchestrator.run_sync_job` expects."""

    def __init__(self):
        self.engine = FakeEngine()
        self.control_db = FakeSession()
        self.lock_conn = None

    def get_db(self):
        return self.control_db
