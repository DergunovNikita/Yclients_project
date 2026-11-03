"""mark the portal's synthetic staff rows: `staff.source_type = 'portal'`

A portal account gets a mirror `staff` row per branch, and until now the only thing that told it from a row of
YClients linked to the account (the CRM row) was `id = portal_user_id AND external_id IS NOT NULL`. An admin can now
link an account to any YClients row, whose id is whatever YClients gave it, so the mirror is marked instead:
rows with a `portal_user_id` that are not CRM rows by that old test get `source_type = 'portal'`.

Mirror rows keep `external_id` as it was (the five legacy mirrors of one account carry `external_id = id`); the unique
index on `(company_id, source_type, external_id)` cannot collide, because no row had 'portal' before.

Downgrade restores 'yclients' on every portal row. A YClients row linked after this migration to an account under a
different id would read as a mirror to the old code, which renames and fires mirrors, so those links are released
first (`portal_user_id = NULL`); that is one-way, the next upgrade does not bring them back.

Revision ID: 0062_portal_mirror_source
Revises: 0061_drop_write_only_catalogs
"""

from alembic import op


revision = '0062_portal_mirror_source'
down_revision = '0061_drop_write_only_catalogs'
branch_labels = None
depends_on = None

OLD_CRM_ROW = 'id = portal_user_id AND external_id IS NOT NULL'


def upgrade() -> None:
    op.execute(
        "UPDATE staff SET source_type = 'portal' "
        f"WHERE portal_user_id IS NOT NULL AND source_type = 'yclients' AND NOT ({OLD_CRM_ROW})"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE staff SET portal_user_id = NULL "
        f"WHERE portal_user_id IS NOT NULL AND source_type <> 'portal' AND NOT ({OLD_CRM_ROW})"
    )
    op.execute("UPDATE staff SET source_type = 'yclients' WHERE source_type = 'portal'")
