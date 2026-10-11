"""Migration 0056 drops the KPI groups, the `services` stub and two write-only sync objects, and can recreate them."""

import io

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

import models
from helpers import load_migration


@pytest.fixture
def migration():
    return load_migration('0056_drop_unused_service_sync.py', 'migration_0056')


def _postgres_script(fn) -> str:
    """The SQL a PostgreSQL run would emit: SQLite cannot alter foreign keys."""
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': buffer})
    with Operations.context(context):
        fn()
    return buffer.getvalue()


def _statements(script: str) -> list[str]:
    return [' '.join(part.split()) for part in script.split(';') if part.strip()]


def test_upgrade_repoints_the_label_key_before_the_stub_goes(migration):
    statements = _statements(_postgres_script(migration.upgrade))

    assert 'DROP TABLE service_kpi_assignments' in statements
    assert 'DROP TABLE service_kpi_groups' in statements
    assert 'ALTER TABLE service_labels DROP CONSTRAINT service_labels_service_id_fkey' in statements
    assert (
        'ALTER TABLE service_labels ADD CONSTRAINT service_labels_company_id_service_id_fkey '
        'FOREIGN KEY(company_id, service_id) REFERENCES service_catalog (company_id, service_id) ON DELETE CASCADE'
    ) in statements
    assert statements.index('DROP TABLE services') > statements.index(
        'ALTER TABLE service_labels DROP CONSTRAINT service_labels_service_id_fkey'
    )
    assert statements.index('DROP TABLE service_kpi_assignments') < statements.index('DROP TABLE service_kpi_groups')
    assert 'DROP TABLE system.sync_step_runs' in statements
    assert 'ALTER TABLE sync_source_states DROP COLUMN synced_at' in statements


def test_downgrade_recreates_the_objects_and_backfills_the_stubs_the_label_key_needs(migration):
    statements = _statements(_postgres_script(migration.downgrade))

    for table in ('services', 'service_kpi_groups', 'service_kpi_assignments', 'system.sync_step_runs'):
        assert any(statement.startswith(f'CREATE TABLE {table} ') for statement in statements), table
    add_column = next(s for s in statements if s.startswith('ALTER TABLE sync_source_states ADD COLUMN synced_at'))
    assert 'NOT NULL' in add_column and 'DEFAULT CURRENT_TIMESTAMP' in add_column  # existing rows need a value
    assert 'ALTER TABLE sync_source_states ALTER COLUMN synced_at DROP DEFAULT' in statements
    insert = next(index for index, statement in enumerate(statements) if statement.startswith('INSERT INTO services'))
    restored_key = (
        'ALTER TABLE service_labels ADD CONSTRAINT service_labels_service_id_fkey '
        'FOREIGN KEY(service_id) REFERENCES services (id) ON DELETE CASCADE'
    )
    assert statements.index(restored_key) > insert  # the key must find a stub for every label already stored


def test_the_label_key_guard_refuses_a_label_without_a_catalog_row(migration):
    engine = sa.create_engine('sqlite:///:memory:', poolclass=sa.pool.StaticPool)
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE service_catalog (company_id INTEGER, service_id INTEGER)'))
        conn.execute(sa.text('CREATE TABLE service_labels (company_id INTEGER, service_id INTEGER)'))
        conn.execute(sa.text('INSERT INTO service_catalog VALUES (1, 10)'))
        conn.execute(sa.text('INSERT INTO service_labels VALUES (1, 10)'))
        with Operations.context(MigrationContext.configure(conn)):
            migration._assert_every_label_has_a_catalog_row()
            conn.execute(sa.text('INSERT INTO service_labels VALUES (1, 11)'))
            with pytest.raises(RuntimeError, match='1 service_labels'):
                migration._assert_every_label_has_a_catalog_row()


def test_models_no_longer_carry_what_the_migration_drops():
    tables = set(models.Base.metadata.tables)
    assert not tables & {'services', 'service_kpi_groups', 'service_kpi_assignments', 'system.sync_step_runs'}
    assert 'synced_at' not in models.SyncSourceState.__table__.c
    (foreign_key,) = models.ServiceLabel.__table__.foreign_key_constraints - {
        constraint for constraint in models.ServiceLabel.__table__.foreign_key_constraints
        if constraint.referred_table.name == 'companies'
    }
    assert foreign_key.referred_table.name == 'service_catalog'
    assert foreign_key.ondelete == 'CASCADE'
    assert [column.name for column in foreign_key.columns] == ['company_id', 'service_id']


def test_migration_chains_from_0055(migration):
    assert migration.down_revision == '0055_auth_lifecycle_cleanup'
    assert len(migration.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
