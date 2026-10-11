"""Migration 0057 drops six fact-table indexes production never scans, and downgrade recreates them."""

import io

import pytest
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


def _statements(fn) -> list[str]:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': buffer})
    with Operations.context(context):
        fn()
    return [' '.join(part.split()) for part in buffer.getvalue().split(';') if part.strip()]


def test_upgrade_drops_exactly_the_six_indexes_if_present(migration):
    assert _statements(migration.upgrade) == [f'DROP INDEX IF EXISTS {name}' for name in DROPPED]


def test_downgrade_recreates_each_index_as_it_was(migration):
    expected = [
        f'CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})' for name, (table, column) in DROPPED.items()
    ]
    assert _statements(migration.downgrade) == expected


def test_models_no_longer_declare_the_dropped_indexes():
    declared = {
        index.name for table in models.Base.metadata.tables.values() for index in table.indexes
    }
    assert not declared & set(DROPPED)


def test_the_heavily_used_neighbours_keep_their_indexes():
    def single_column_indexed(table: str, column: str) -> bool:
        return models.Base.metadata.tables[table].c[column].index is True

    assert single_column_indexed('appointments', 'client_id')
    assert single_column_indexed('financial_transactions', 'record_id')


def test_migration_chains_from_0056(migration):
    assert migration.down_revision == '0056_drop_unused_service_sync'
    assert len(migration.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
