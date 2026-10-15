"""owner-chosen branch order

`system.portal_branches.sort_order` lets the tenant owner reorder the branch lists. NULL means "no choice":
those branches fall back to the default order (reporting start date, then id), so the column is nullable and
existing rows keep today's behaviour without a backfill.

Revision ID: 0058_portal_branch_sort_order
Revises: 0057_drop_unused_fact_indexes
"""

import sqlalchemy as sa
from alembic import op


revision = '0058_portal_branch_sort_order'
down_revision = '0057_drop_unused_fact_indexes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('portal_branches', sa.Column('sort_order', sa.Integer(), nullable=True), schema='system')


def downgrade() -> None:
    op.drop_column('portal_branches', 'sort_order', schema='system')
