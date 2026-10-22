"""Migration 0054 drops the legacy catalog twins and write-only columns, and can put them back empty."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from helpers import load_migration


@pytest.fixture
def migration():
    return load_migration('0054_drop_legacy_catalogs.py', 'migration_0054')


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _database_without_legacy_objects():
    """Only the tables 0054 alters, with the columns it re-adds left out."""
    engine = sa.create_engine('sqlite:///:memory:', poolclass=sa.pool.StaticPool)
    with engine.begin() as conn:
        conn.execute(sa.text("ATTACH DATABASE ':memory:' AS system"))
        conn.execute(sa.text('CREATE TABLE companies (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL)'))
        conn.execute(sa.text('CREATE TABLE groups (id INTEGER PRIMARY KEY, title VARCHAR NOT NULL)'))
        conn.execute(sa.text('CREATE TABLE staff (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL)'))
        conn.execute(sa.text('CREATE TABLE system.sync_jobs (id INTEGER PRIMARY KEY, mode VARCHAR NOT NULL)'))
    return engine


def _columns(engine, table, schema=None):
    return {column['name'] for column in sa.inspect(engine).get_columns(table, schema=schema)}


def test_downgrade_recreates_and_upgrade_drops_the_legacy_objects(migration):
    engine = _database_without_legacy_objects()

    _run(engine, migration.downgrade)
    assert set(migration.LEGACY_TABLES) <= set(sa.inspect(engine).get_table_names())
    assert {'country', 'locale', 'currency'} <= _columns(engine, 'companies')
    assert 'access' in _columns(engine, 'groups')
    assert 'avatar_url' in _columns(engine, 'staff')
    assert 'cancel_requested' in _columns(engine, 'sync_jobs', 'system')
    assert {ix['name'] for ix in sa.inspect(engine).get_indexes('goods')} == {
        'ix_goods_barcode', 'ix_goods_category_id', 'ix_goods_last_change_date', 'ix_goods_company_id',
    }

    _run(engine, migration.upgrade)
    assert not set(migration.LEGACY_TABLES) & set(sa.inspect(engine).get_table_names())
    assert _columns(engine, 'companies') == {'id', 'title'}
    assert _columns(engine, 'groups') == {'id', 'title'}
    assert _columns(engine, 'staff') == {'id', 'name'}
    assert _columns(engine, 'sync_jobs', 'system') == {'id', 'mode'}

    # A fresh database is bootstrapped from the models and never had any of it.
    _run(engine, migration.upgrade)


def test_models_no_longer_carry_what_the_migration_drops(migration):
    tables = models.Base.metadata.tables
    assert not set(migration.LEGACY_TABLES) & set(tables)
    for table, schema, column, _type, _default in migration.DROPPED_COLUMNS:
        key = f'{schema}.{table}' if schema else table
        assert column not in tables[key].c
