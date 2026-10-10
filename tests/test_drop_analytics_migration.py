"""Migration 0052 drops the write-only analytics / Z-report tables and can put them back."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION = Path(__file__).resolve().parents[1] / 'alembic' / 'versions' / '0052_drop_analytics_tables.py'


@pytest.fixture
def migration():
    spec = importlib.util.spec_from_file_location('migration_0052', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, fn):
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            fn()


def test_upgrade_drops_and_downgrade_recreates_the_tables(migration):
    engine = sa.create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(sa.text('CREATE TABLE companies (id INTEGER PRIMARY KEY)'))

    _run(engine, migration.downgrade)
    inspector = sa.inspect(engine)
    assert set(migration.TABLES) <= set(inspector.get_table_names())
    columns = {column['name'] for column in inspector.get_columns('z_reports')}
    assert {'report_date', 'clients_average', 'total_discount', 'company_id'} <= columns
    assert {ix['name'] for ix in inspector.get_indexes('z_report_payments')} == {
        'ix_z_report_payments_company_id',
        'ix_z_report_payments_payment_group',
        'ix_z_report_payments_report_date',
    }
    assert inspector.get_foreign_keys('analytics_overall')[0]['referred_table'] == 'companies'

    _run(engine, migration.upgrade)
    assert not set(migration.TABLES) & set(sa.inspect(engine).get_table_names())

    # Dropping what is already gone must not fail: a fresh database never creates the tables.
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    assert set(migration.TABLES) <= set(sa.inspect(engine).get_table_names())
