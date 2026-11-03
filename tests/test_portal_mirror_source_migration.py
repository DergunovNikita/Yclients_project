"""Migration 0062: the portal's mirror rows are marked `source_type = 'portal'`, and a rollback does not leave a link
that the old rule would read as a mirror."""

from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy import create_engine, select

from helpers import load_migration
from models import Staff

CRM, MIRROR, LEGACY_MIRROR, FREE, LINKED, DEMO = 1, 2, 3, 4, 5, 6


def _row(row_id, *, portal_user_id=None, external_id=None, source_type='yclients'):
    return {
        'id': row_id, 'name': f'Staff {row_id}', 'company_id': None, 'fired': 0, 'portal_user_id': portal_user_id,
        'external_id': external_id, 'source_type': source_type,
    }


def _migrate(direction, rows, times=1):
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        Staff.__table__.create(connection)
        connection.execute(Staff.__table__.insert(), rows)
    migration = load_migration('0062_portal_mirror_source.py', 'migration_0062')
    with engine.begin() as connection:
        migration.op = SimpleNamespace(execute=lambda statement: connection.execute(sa.text(statement)))
        for _ in range(times):
            getattr(migration, direction)()
        return {
            row.id: (row.source_type, row.portal_user_id)
            for row in connection.execute(select(Staff.id, Staff.source_type, Staff.portal_user_id))
        }


BEFORE = [
    # Provisioned from the employee: id is the account's id, YClients gave it an external id.
    _row(CRM, portal_user_id=CRM, external_id=CRM),
    _row(MIRROR, portal_user_id=50),
    # Legacy mirror: migration 0033 gave it `external_id = id`, but its id is not its user's.
    _row(LEGACY_MIRROR, portal_user_id=51, external_id=LEGACY_MIRROR),
    _row(FREE, external_id=FREE),
    _row(DEMO, external_id=DEMO, source_type='demo'),
]


def test_only_the_portals_own_rows_are_marked_and_a_second_run_changes_nothing():
    expected = {
        CRM: ('yclients', CRM), MIRROR: ('portal', 50), LEGACY_MIRROR: ('portal', 51),
        FREE: ('yclients', None), DEMO: ('demo', None),
    }
    assert _migrate('upgrade', BEFORE) == expected
    assert _migrate('upgrade', BEFORE, times=2) == expected


def test_a_rollback_unmarks_the_mirrors_and_releases_links_the_old_rule_would_take_for_mirrors():
    after = BEFORE[:3] + [_row(LINKED, portal_user_id=60, external_id=LINKED)]
    after[1]['source_type'] = after[2]['source_type'] = 'portal'

    assert _migrate('downgrade', after) == {
        CRM: ('yclients', CRM), MIRROR: ('yclients', 50), LEGACY_MIRROR: ('yclients', 51),
        # Linked by an admin under another id: the old code would rename and fire it as a mirror.
        LINKED: ('yclients', None),
    }
