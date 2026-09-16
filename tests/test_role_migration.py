"""Migration 0046: the viewer role becomes the pair admin / barber.

Both directions are run against SQLite, because both rewrite rows. The downgrade is the
destructive one — it collapses two role names into one and drops the per-label money
overrides — and the rule it has to honour is that a rollback can lose configuration but must
never widen anybody's access. The upgrade is checked for the one thing that is easy to forget:
it has to rename `staff.position` on the mirror rows too, or the label the portal wrote stops
matching the category the dashboard reads back.
"""

import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy import create_engine, select

from models import PortalMetricVisibility, PortalUser, Staff


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / 'alembic' / 'versions' / '0046_split_viewer_into_admin_barber.py'
)


def _load_migration():
    spec = importlib.util.spec_from_file_location('migration_0046', MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _user(user_id, role):
    return {
        'id': user_id,
        'portal_account_id': 1,
        'email': f'user{user_id}@example.com',
        'password_hash': 'x',
        'role': role,
        'is_active': True,
        'is_demo': False,
        'token_version': 0,
        'created_at': datetime(2026, 1, 1),
    }


def _visibility(role, codes):
    return {
        'portal_account_id': 1,
        'role': role,
        'visible_codes': codes,
        'updated_at': datetime(2026, 1, 1),
    }


def _migrate(direction, users=(), visibility=(), staff=()):
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.exec_driver_sql("ATTACH DATABASE ':memory:' AS system")
        for table in (PortalUser.__table__, PortalMetricVisibility.__table__, Staff.__table__):
            table.create(connection)
        for table, rows in (
            (PortalUser.__table__, users),
            (PortalMetricVisibility.__table__, visibility),
            (Staff.__table__, staff),
        ):
            if rows:
                connection.execute(table.insert(), list(rows))

    migration = _load_migration()
    with engine.begin() as connection:
        migration.op = SimpleNamespace(
            get_bind=lambda: connection,
            execute=lambda statement: connection.execute(sa.text(statement)),
            # Server defaults are a no-op on SQLite; the data rewrite is what is under test.
            alter_column=lambda *args, **kwargs: None,
        )
        getattr(migration, direction)()
        return {
            'roles': sorted(connection.execute(select(PortalUser.id, PortalUser.role)).all()),
            'visibility': sorted(
                connection.execute(
                    select(PortalMetricVisibility.role, PortalMetricVisibility.visible_codes)
                ).all()
            ),
            'positions': sorted(connection.execute(select(Staff.id, Staff.position)).all()),
        }


def _downgrade(**kwargs):
    return _migrate('downgrade', **kwargs)


def _upgrade(**kwargs):
    return _migrate('upgrade', **kwargs)


def test_every_viewer_lands_on_the_half_that_opens_nothing():
    result = _upgrade(users=[_user(1, 'viewer'), _user(2, 'manager')])
    assert result['roles'] == [(1, 'barber'), (2, 'manager')]


def test_the_upgrade_renames_the_position_it_will_rename_back():
    """A mirror row left at 'viewer' reads as category `unknown` while a re-synced one reads
    as `barber` — two accounts of one role on different sides of the leaderboard split."""
    result = _upgrade(
        users=[_user(50, 'viewer')],
        staff=[
            {'id': 900, 'name': 'Mirror', 'position': 'viewer', 'company_id': 1,
             'fired': 0, 'portal_user_id': 50, 'source_type': 'portal'},
            {'id': 50, 'name': 'From CRM', 'position': 'Администратор', 'company_id': 1,
             'fired': 0, 'portal_user_id': 50, 'source_type': 'yclients'},
            {'id': 901, 'name': 'Plain', 'position': 'Барбер', 'company_id': 1,
             'fired': 0, 'portal_user_id': None, 'source_type': 'yclients'},
        ],
    )
    assert result['positions'] == [
        (50, 'Администратор'),
        (900, 'barber'),
        (901, 'Барбер'),
    ]


def test_both_names_collapse_back_into_one_role():
    result = _downgrade(users=[_user(1, 'admin'), _user(2, 'barber'), _user(3, 'manager')])
    assert result['roles'] == [(1, 'viewer'), (2, 'viewer'), (3, 'manager')]


def test_money_overrides_for_the_pair_do_not_survive():
    """Carrying either label's codes over would grant the other something it was refused."""
    result = _downgrade(
        visibility=[
            _visibility('barber', ['avg_check']),
            _visibility('admin', ['revenue']),
            _visibility('manager', ['avg_check']),
        ]
    )
    assert result['visibility'] == [('manager', ['avg_check'])]


def test_a_lone_override_is_dropped_too():
    result = _downgrade(visibility=[_visibility('barber', ['avg_check'])])
    assert result['visibility'] == []


def test_portal_created_staff_rows_lose_the_role_they_carried_as_a_position():
    """`admin` in `position` would read as an administrator to the pre-0046 code."""
    result = _downgrade(
        users=[_user(50, 'admin')],
        staff=[
            # Portal-created mirror row: its id differs from the portal user id.
            {'id': 900, 'name': 'Mirror', 'position': 'admin', 'company_id': 1,
             'fired': 0, 'portal_user_id': 50, 'source_type': 'portal'},
            # Provisioned from the CRM: YClients owns this position, the portal must not touch it.
            {'id': 50, 'name': 'From CRM', 'position': 'Администратор', 'company_id': 1,
             'fired': 0, 'portal_user_id': 50, 'source_type': 'yclients'},
            # Nothing to do with the portal at all.
            {'id': 901, 'name': 'Plain', 'position': 'barber', 'company_id': 1,
             'fired': 0, 'portal_user_id': None, 'source_type': 'yclients'},
        ],
    )
    assert result['positions'] == [
        (50, 'Администратор'),
        (900, 'viewer'),
        (901, 'barber'),
    ]
