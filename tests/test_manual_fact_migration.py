"""Data migration 0040: manual fact rows are collapsed into calendar months."""

import importlib.util
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import create_engine, select

from models import ManualFactMetric


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / 'alembic' / 'versions' / '0040_month_anchored_manual_facts.py'
)


def _load_migration():
    spec = importlib.util.spec_from_file_location('migration_0040', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _row(period_start, period_end, staff_id, value, updated_at, source='dashboard'):
    return {
        'period_start': period_start,
        'period_end': period_end,
        'company_id': 1,
        'staff_id': staff_id,
        'metric_code': 'reviews_qty',
        'value': value,
        'source': source,
        'updated_at': updated_at,
    }


def _upgrade(monkeypatch, seed_rows):
    engine = create_engine('sqlite://')
    ManualFactMetric.__table__.create(engine)
    with engine.begin() as connection:
        connection.execute(ManualFactMetric.__table__.insert(), seed_rows)

    migration = _load_migration()
    with engine.begin() as connection:
        monkeypatch.setattr(migration, 'op', SimpleNamespace(get_bind=lambda: connection))
        migration.upgrade()
    return engine


def test_upgrade_collapses_overlapping_rows_into_one_row_per_month(monkeypatch):
    engine = _upgrade(monkeypatch, [
        _row(date(2025, 6, 1), date(2025, 6, 30), 2, 20.0, datetime(2025, 6, 30)),
        _row(date(2025, 6, 1), date(2025, 6, 7), 2, 5.0, datetime(2025, 7, 1), 'legacy'),
        _row(date(2025, 7, 1), date(2025, 7, 31), 2, 4.0, datetime(2025, 7, 31)),
        _row(date(2025, 6, 10), date(2025, 6, 10), 3, 2.0, datetime(2025, 6, 10)),
    ])

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                ManualFactMetric.period_start,
                ManualFactMetric.period_end,
                ManualFactMetric.staff_id,
                ManualFactMetric.value,
                ManualFactMetric.source,
            ).order_by(ManualFactMetric.staff_id, ManualFactMetric.period_start)
        ).all()
    engine.dispose()

    assert [tuple(row) for row in rows] == [
        # The month and the week saved separately were double counted before, so they are
        # summed into the single value the full-month view already showed.
        (date(2025, 6, 1), date(2025, 6, 30), 2, 25.0, 'legacy'),
        (date(2025, 7, 1), date(2025, 7, 31), 2, 4.0, 'dashboard'),
        (date(2025, 6, 1), date(2025, 6, 30), 3, 2.0, 'dashboard'),
    ]


def test_upgrade_widens_a_partial_row_stored_before_the_month_row(monkeypatch):
    """The row being widened may collide with a month row that is still in the table."""
    engine = _upgrade(monkeypatch, [
        _row(date(2025, 8, 1), date(2025, 8, 13), 2, 5.0, datetime(2025, 8, 13)),
        _row(date(2025, 8, 1), date(2025, 8, 31), 2, 20.0, datetime(2025, 8, 31)),
    ])

    with engine.connect() as connection:
        rows = connection.execute(
            select(
                ManualFactMetric.period_start,
                ManualFactMetric.period_end,
                ManualFactMetric.value,
            )
        ).all()
    engine.dispose()

    assert [tuple(row) for row in rows] == [(date(2025, 8, 1), date(2025, 8, 31), 25.0)]
