"""Migration 0057 drops six fact-table indexes production never scans, and downgrade recreates them."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from helpers import load_migration

DROPPED = {
    'ix_financial_transactions_client_id': ('financial_transactions', 'client_id'),
    'ix_clients_phone': ('clients', 'phone'),
    'ix_comments_record_id': ('comments', 'record_id'),
    'ix_goods_transactions_client_id': ('goods_transactions', 'client_id'),
    'ix_goods_transactions_storage_id': ('goods_transactions', 'storage_id'),
    'ix_clients_last_visit_date': ('clients', 'last_visit_date'),
}


@pytest.fixture
def migration():
    return load_migration('0057_drop_unused_fact_indexes.py', 'migration_0057')


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _indexes(engine) -> dict[str, tuple[str, str]]:
    """{index name: (table, column)} of every single-column index in the database."""
    inspector = sa.inspect(engine)
    return {
        ix['name']: (table, ix['column_names'][0])
        for table in inspector.get_table_names()
        for ix in inspector.get_indexes(table)
    }


def test_downgrade_recreates_the_indexes_and_upgrade_drops_them(migration):
    engine = sa.create_engine('sqlite:///:memory:', poolclass=sa.pool.StaticPool)
    columns = {}
    for table, column in DROPPED.values():
        columns.setdefault(table, []).append(column)
    with engine.begin() as conn:
        for table, table_columns in columns.items():
            conn.execute(sa.text(
                f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, {", ".join(f"{c} INTEGER" for c in table_columns)})'
            ))

    _run(engine, migration.downgrade)
    assert _indexes(engine) == DROPPED

    _run(engine, migration.upgrade)
    assert _indexes(engine) == {}

    # A database bootstrapped from the models never had them: dropping what is gone must not fail.
    _run(engine, migration.upgrade)


def test_models_no_longer_declare_the_dropped_indexes():
    declared = {
        index.name for table in models.Base.metadata.tables.values() for index in table.indexes
    }
    assert not declared & set(DROPPED)
