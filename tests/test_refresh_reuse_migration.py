"""Migration 0053 adds the refresh-token reuse-detection columns and removes them again."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from models import PortalRefreshToken

MIGRATION = Path(__file__).resolve().parents[1] / 'alembic' / 'versions' / '0053_refresh_reuse_detection.py'


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location('migration_0053', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert migration.INDEX_NAME in {ix['name'] for ix in inspector.get_indexes('portal_refresh_tokens', schema='system')}

    _run(engine, migration.downgrade)
    inspector = sa.inspect(engine)
    assert {c['name'] for c in inspector.get_columns('portal_refresh_tokens', schema='system')} == {'id', 'token_hash'}
    assert migration.INDEX_NAME not in {
        ix['name'] for ix in inspector.get_indexes('portal_refresh_tokens', schema='system')
    }


def test_migration_matches_the_model_and_chains_from_0052(migration):
    assert migration.down_revision == '0052_drop_analytics_tables'
    assert len(migration.revision) <= 32  # alembic_version.version_num is VARCHAR(32)
    index_names = {index.name for index in PortalRefreshToken.__table__.indexes}
    assert migration.INDEX_NAME in index_names
    assert {'previous_token_hash', 'rotated_at'} <= set(PortalRefreshToken.__table__.columns.keys())
