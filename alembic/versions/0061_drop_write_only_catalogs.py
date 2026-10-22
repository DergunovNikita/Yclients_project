"""drop two write-only catalogs and the sync_state keys nobody reads

* `staff_position_catalog`, `storage_catalog`: filled by the sync, read by nothing. Barber levels come from
  `staff.position_id` and `staff_position_months`; warehouses are only copied onto goods transactions as an id and
  a title. The sync steps that fed them are gone too.
* `system.sync_state` rows written by `create_run` / `finish_run` before the run bookkeeping moved to `sync_runs`
  (`last_run_*`, `last_successful_run_id`) and the `services_labels_last_*` keys of a sync step that no longer exists.
* the global `transactions_last_success_date` checkpoint: every branch has its own `...:company:<id>` one, nothing
  has written the global key since, and the code that fell back to it is gone. Only the exact name goes; the scoped
  keys share its prefix and are the live checkpoints.

Downgrade recreates both tables empty, with the columns, key, index and foreign key `0014` gave them; the data is
refilled by the first sync of a build that still has the steps. The deleted `sync_state` rows are not restored:
nothing read them, and the next run of the old code writes them again.

Revision ID: 0061_drop_write_only_catalogs
Revises: 0060_staff_position_levels
"""

import sqlalchemy as sa
from alembic import op


revision = '0061_drop_write_only_catalogs'
down_revision = '0060_staff_position_levels'
branch_labels = None
depends_on = None

ORPHAN_STATE_KEYS = (
    'last_run_id',
    'last_run_status',
    'last_run_mode',
    'last_run_trigger_type',
    'last_run_started_at',
    'last_run_finished_at',
    'last_successful_run_id',
    'transactions_last_success_date',
)
ORPHAN_STATE_KEY_PREFIX = 'services_labels_last_'


def upgrade() -> None:
    op.drop_table('staff_position_catalog', schema='public')
    op.drop_table('storage_catalog', schema='public')
    keys = ', '.join(f"'{key}'" for key in ORPHAN_STATE_KEYS)
    op.execute(
        f"DELETE FROM system.sync_state WHERE key IN ({keys}) "
        f"OR starts_with(key, '{ORPHAN_STATE_KEY_PREFIX}')"
    )


def downgrade() -> None:
    op.create_table(
        'staff_position_catalog',
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('position_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('company_id', 'position_id'),
        sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
        schema='public',
    )
    op.create_index(
        'ix_staff_position_catalog_position_id', 'staff_position_catalog', ['position_id'], schema='public'
    )
    op.create_table(
        'storage_catalog',
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('storage_id', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('for_services', sa.Boolean(), nullable=True),
        sa.Column('for_sale', sa.Boolean(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('company_id', 'storage_id'),
        sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
        schema='public',
    )
    op.create_index('ix_storage_catalog_storage_id', 'storage_catalog', ['storage_id'], schema='public')
