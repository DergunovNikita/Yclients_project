"""Migration 0061 drops two write-only catalogs and the orphan sync_state keys, and can recreate the tables."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from helpers import load_migration

LIVE_STATE_KEYS = {
    # The scoped checkpoints share the global orphan key's prefix and must survive it.
    'transactions_last_success_date:company:1',
    'last_run_retention_at',
    'last_successful_sync_at',
    'last_unscoped_sync_at',
}


@pytest.fixture
def migration():
    return load_migration('0061_drop_write_only_catalogs.py', 'migration_0061')


@pytest.fixture
def engine():
    engine = sa.create_engine('sqlite:///:memory:', poolclass=sa.pool.StaticPool)

    @sa.event.listens_for(engine, 'connect')
    def _emulate_postgres(dbapi_connection, _record):
        # PostgreSQL's `starts_with` has no SQLite counterpart, and the migration's schemas are attached databases.
        dbapi_connection.create_function('starts_with', 2, lambda value, prefix: value.startswith(prefix))
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS system")

    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE public.companies (id INTEGER PRIMARY KEY)'))
        conn.execute(sa.text('CREATE TABLE system.sync_state (key VARCHAR PRIMARY KEY, value VARCHAR)'))
    yield engine
    engine.dispose()


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _state_keys(engine) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(sa.text('SELECT key FROM system.sync_state')).scalars())


def test_upgrade_drops_the_tables_and_only_the_orphan_state_keys(migration, engine):
    orphan_keys = {*migration.ORPHAN_STATE_KEYS, 'services_labels_last_2026-10-01'}
    with engine.begin() as conn:
        for key in orphan_keys | LIVE_STATE_KEYS:
            conn.execute(sa.text("INSERT INTO system.sync_state VALUES (:key, '1')"), {'key': key})

    _run(engine, migration.downgrade)
    assert {'staff_position_catalog', 'storage_catalog'} <= set(sa.inspect(engine).get_table_names('public'))

    _run(engine, migration.upgrade)
    assert set(sa.inspect(engine).get_table_names('public')) == {'companies'}
    assert _state_keys(engine) == LIVE_STATE_KEYS


def test_downgrade_recreates_both_tables_with_their_original_key_index_and_foreign_key(migration, engine):
    _run(engine, migration.downgrade)

    inspector = sa.inspect(engine)
    for table, key in (('staff_position_catalog', 'position_id'), ('storage_catalog', 'storage_id')):
        assert inspector.get_pk_constraint(table, schema='public')['constrained_columns'] == ['company_id', key]
        assert [ix['column_names'] for ix in inspector.get_indexes(table, schema='public')] == [[key]]
        (foreign_key,) = inspector.get_foreign_keys(table, schema='public')
        assert foreign_key['referred_table'] == 'companies'
        assert foreign_key['constrained_columns'] == ['company_id']
        assert foreign_key['options'] == {'ondelete': 'CASCADE'}


def test_models_no_longer_carry_the_dropped_tables():
    assert not set(models.Base.metadata.tables) & {'staff_position_catalog', 'storage_catalog'}
