import pytest
from sqlalchemy.engine import make_url
from sqlalchemy import create_engine, text

import database
from database import build_async_database_url, build_database_url
import migrate


def test_database_urls_encode_special_characters_in_credentials():
    password = 'p@ss/word:100%'

    for builder in (build_database_url, build_async_database_url):
        url = make_url(builder('db.example', 5432, 'yclients_db', 'portal_user', password))

        assert url.host == 'db.example'
        assert url.port == 5432
        assert url.database == 'yclients_db'
        assert url.username == 'portal_user'
        assert url.password == password


def test_database_urls_keep_password_optional():
    url = make_url(build_database_url('localhost', 5432, 'yclients_db', 'postgres'))

    assert url.host == 'localhost'
    assert url.username == 'postgres'
    assert url.password is None


def test_run_migrations_accepts_percent_encoded_database_url(monkeypatch):
    captured = {}
    url = build_database_url('postgres', 5432, 'yclients_db', 'postgres', 'secret!')

    def fake_upgrade(config, revision):
        captured['url'] = config.get_main_option('sqlalchemy.url')
        captured['revision'] = revision

    monkeypatch.setattr(database.command, 'upgrade', fake_upgrade)

    database.run_migrations(url, revision='head')

    assert '%21' in captured['url']
    assert captured['revision'] == 'head'


class FakeDatabase:
    def __init__(self, engine):
        self.engine = engine

    def test_connection(self):
        return True


def test_migrate_bootstraps_empty_database(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    called = {'bootstrap': False, 'upgrade': False}

    try:
        monkeypatch.setattr(migrate, 'init_database', lambda *_args: FakeDatabase(engine))
        monkeypatch.setattr(migrate, 'bootstrap_database', lambda _database: called.__setitem__('bootstrap', True))
        monkeypatch.setattr(migrate, 'run_migrations', lambda *_args: called.__setitem__('upgrade', True))

        assert migrate.main() == 0
        assert called == {'bootstrap': True, 'upgrade': False}
    finally:
        engine.dispose()


def test_migrate_refuses_nonempty_unstamped_database(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE companies (id INTEGER PRIMARY KEY)'))
    called = {'bootstrap': False, 'upgrade': False}

    try:
        monkeypatch.setattr(migrate, 'init_database', lambda *_args: FakeDatabase(engine))
        monkeypatch.setattr(migrate, 'bootstrap_database', lambda _database: called.__setitem__('bootstrap', True))
        monkeypatch.setattr(migrate, 'run_migrations', lambda *_args: called.__setitem__('upgrade', True))

        assert migrate.main() == 1
        assert called == {'bootstrap': False, 'upgrade': False}
    finally:
        engine.dispose()


def test_migrate_upgrades_alembic_stamped_database(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)'))
    called = {'bootstrap': False, 'upgrade': False}

    try:
        monkeypatch.setattr(migrate, 'init_database', lambda *_args: FakeDatabase(engine))
        monkeypatch.setattr(migrate, 'bootstrap_database', lambda _database: called.__setitem__('bootstrap', True))
        monkeypatch.setattr(migrate, 'run_migrations', lambda *_args: called.__setitem__('upgrade', True))

        assert migrate.main() == 0
        assert called == {'bootstrap': False, 'upgrade': True}
    finally:
        engine.dispose()


def test_migration_engine_bounds_lock_waits_on_postgres(monkeypatch):
    """The Alembic engine must carry `lock_timeout`, or a deploy can wait on a sync's locks for 30 minutes."""
    import logging.config

    import sqlalchemy

    from database import build_database_url, run_migrations

    captured = {}

    def fake_engine_from_config(*_args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError('stop before connecting')

    monkeypatch.setattr(sqlalchemy, 'engine_from_config', fake_engine_from_config)
    # env.py applies alembic.ini's logging config, which would reset the logging of the whole test session.
    monkeypatch.setattr(logging.config, 'fileConfig', lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match='stop before connecting'):
        run_migrations(build_database_url('127.0.0.1', 5433, 'db', 'user', 'p%ss'))

    prefix = '-c lock_timeout='
    options = captured['connect_args']['options']
    assert options.startswith(prefix)
    # Bounded, but not forever: the deploy timer retries every 5 minutes.
    assert 0 < int(options.removeprefix(prefix)) < 5 * 60 * 1000
