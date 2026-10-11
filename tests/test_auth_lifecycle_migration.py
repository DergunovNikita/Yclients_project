"""Migration 0055 makes users and credentials deletable and drops two write-only columns."""

import io

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from helpers import load_migration
from models import PortalAuditEvent, PortalRefreshToken, SyncJob, SyncJobEvent, YClientsCredential


@pytest.fixture
def migration():
    return load_migration('0055_auth_lifecycle_cleanup.py', 'migration_0055')


def _postgres_script(fn) -> str:
    """The SQL a PostgreSQL run would emit: SQLite cannot alter foreign keys."""
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': buffer})
    with Operations.context(context):
        fn()
    return buffer.getvalue()


def _statements(script: str) -> list[str]:
    return [' '.join(part.split()) for part in script.split(';') if part.strip()]


def test_upgrade_nulls_the_links_and_drops_the_dead_columns(migration):
    statements = _statements(_postgres_script(migration.upgrade))

    for table, column, name, referent in migration.SET_NULL_FOREIGN_KEYS:
        assert f'ALTER TABLE system.{table} DROP CONSTRAINT {name}' in statements
        assert (
            f'ALTER TABLE system.{table} ADD CONSTRAINT {name} FOREIGN KEY({column}) '
            f'REFERENCES system.{referent} (id) ON DELETE SET NULL'
        ) in statements
    assert 'ALTER TABLE system.portal_refresh_tokens DROP COLUMN revoked_at' in statements
    assert (
        'CREATE INDEX ix_portal_refresh_tokens_user_state ON system.portal_refresh_tokens (user_id, last_used_at)'
    ) in statements
    assert 'ALTER TABLE system.yclients_credentials DROP COLUMN credential_fingerprint' in statements
    # The index goes before the column that holds it, and the rebuilt one comes after the column is gone.
    index_drop = statements.index('DROP INDEX system.ix_portal_refresh_tokens_user_state')
    assert index_drop < statements.index('ALTER TABLE system.portal_refresh_tokens DROP COLUMN revoked_at')


def test_downgrade_restores_plain_foreign_keys_and_the_columns(migration):
    statements = _statements(_postgres_script(migration.downgrade))

    for table, column, name, referent in migration.SET_NULL_FOREIGN_KEYS:
        assert (
            f'ALTER TABLE system.{table} ADD CONSTRAINT {name} FOREIGN KEY({column}) '
            f'REFERENCES system.{referent} (id)'
        ) in statements
    assert not any('ON DELETE' in statement for statement in statements)
    assert 'ALTER TABLE system.portal_refresh_tokens ADD COLUMN revoked_at TIMESTAMP WITHOUT TIME ZONE' in statements
    assert (
        'CREATE INDEX ix_portal_refresh_tokens_user_state ON system.portal_refresh_tokens '
        '(user_id, revoked_at, last_used_at)'
    ) in statements
    assert 'ALTER TABLE system.yclients_credentials ADD COLUMN credential_fingerprint VARCHAR(64)' in statements


def test_migration_matches_the_models_and_chains_from_0054(migration):
    assert migration.down_revision == '0054_drop_legacy_catalogs'
    assert len(migration.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
    for model, column in (
        (PortalAuditEvent, 'actor_user_id'),
        (SyncJob, 'credential_id'),
        (SyncJobEvent, 'credential_id'),
    ):
        (foreign_key,) = model.__table__.c[column].foreign_keys
        assert foreign_key.ondelete == 'SET NULL'
    assert 'revoked_at' not in PortalRefreshToken.__table__.c
    assert 'credential_fingerprint' not in YClientsCredential.__table__.c
    index = next(ix for ix in PortalRefreshToken.__table__.indexes if ix.name == 'ix_portal_refresh_tokens_user_state')
    assert [column.name for column in index.columns] == ['user_id', 'last_used_at']
