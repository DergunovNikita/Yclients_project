"""Migration 0061 drops two write-only catalogs and the orphan sync_state keys, and can recreate the tables."""

import io

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from helpers import load_migration


@pytest.fixture
def migration():
    return load_migration('0061_drop_write_only_catalogs.py', 'migration_0061')


def _postgres_script(fn) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': buffer})
    with Operations.context(context):
        fn()
    return buffer.getvalue()


def _statements(script: str) -> list[str]:
    return [' '.join(part.split()) for part in script.split(';') if part.strip()]


def test_upgrade_drops_the_tables_and_only_the_orphan_state_keys(migration):
    statements = _statements(_postgres_script(migration.upgrade))

    assert 'DROP TABLE public.staff_position_catalog' in statements
    assert 'DROP TABLE public.storage_catalog' in statements
    delete = next(statement for statement in statements if statement.startswith('DELETE FROM system.sync_state'))
    for key in migration.ORPHAN_STATE_KEYS:
        assert f"'{key}'" in delete
    assert "starts_with(key, 'services_labels_last_')" in delete
    assert "'transactions_last_success_date'" in delete
    # Neighbours that are still read: the retention throttle and the checkpoints.
    for kept in ('last_run_retention_at', 'last_successful_sync_at', 'last_unscoped_sync_at'):
        assert kept not in delete
    # The scoped checkpoints share the global key's prefix: it must be matched by exact name only.
    assert 'transactions_last_success_date:company' not in delete
    assert "starts_with(key, 'transactions" not in delete


def test_downgrade_recreates_both_tables_with_their_original_key_index_and_foreign_key(migration):
    statements = _statements(_postgres_script(migration.downgrade))

    for table, key in (('staff_position_catalog', 'position_id'), ('storage_catalog', 'storage_id')):
        create = next(s for s in statements if s.startswith(f'CREATE TABLE public.{table} '))
        assert f'PRIMARY KEY (company_id, {key})' in create
        assert 'FOREIGN KEY(company_id) REFERENCES public.companies (id) ON DELETE CASCADE' in create
        assert f'CREATE INDEX ix_{table}_{key} ON public.{table} ({key})' in statements


def test_models_no_longer_carry_the_dropped_tables():
    assert not set(models.Base.metadata.tables) & {'staff_position_catalog', 'storage_catalog'}


def test_migration_chains_from_0060(migration):
    assert migration.down_revision == '0060_staff_position_levels'
    assert len(migration.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
