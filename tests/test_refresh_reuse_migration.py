"""Migration 0053 adds the refresh-token reuse-detection columns and removes them again."""

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from helpers import load_migration
from models import PortalRefreshToken


@pytest.fixture
def migration():
    return load_migration('0053_refresh_reuse_detection.py', 'migration_0053')


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def _legacy_table(engine):
    """portal_refresh_tokens as 0052 left it: the model minus the two new columns and their index."""
    with engine.begin() as conn:
        conn.execute(sa.text("ATTACH DATABASE ':memory:' AS system"))
        conn.execute(sa.text(
            'CREATE TABLE system.portal_refresh_tokens (id INTEGER PRIMARY KEY, token_hash VARCHAR(64) NOT NULL)'
        ))


def test_upgrade_adds_the_columns_and_index_and_downgrade_removes_them(migration):
    engine = sa.create_engine('sqlite:///:memory:', poolclass=sa.pool.StaticPool)
    _legacy_table(engine)

    _run(engine, migration.upgrade)
    inspector = sa.inspect(engine)
    assert {c['name'] for c in inspector.get_columns('portal_refresh_tokens', schema='system')} >= {
        'previous_token_hash', 'rotated_at',
    }
    assert migration.INDEX_NAME in {
        ix['name'] for ix in inspector.get_indexes('portal_refresh_tokens', schema='system')
    }

    _run(engine, migration.downgrade)
    inspector = sa.inspect(engine)
    assert {c['name'] for c in inspector.get_columns('portal_refresh_tokens', schema='system')} == {'id', 'token_hash'}
    assert migration.INDEX_NAME not in {
        ix['name'] for ix in inspector.get_indexes('portal_refresh_tokens', schema='system')
    }


def test_the_model_matches_the_migration(migration):
    index_names = {index.name for index in PortalRefreshToken.__table__.indexes}
    assert migration.INDEX_NAME in index_names
    assert {'previous_token_hash', 'rotated_at'} <= set(PortalRefreshToken.__table__.columns.keys())
