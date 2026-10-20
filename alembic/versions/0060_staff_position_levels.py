"""barber levels: staff position id and its monthly history

Barber levels are YClients positions («ТОП-барбер», «ПРЕМИУМ-барбер», ...). The sync used to keep only the position
title, and YClients only knows the current position, so a promotion would rewrite the history of every earlier month.
`staff.position_id` stores the id, `staff_position_months` the position the sync saw in each month. Ids are unknown
until the first sync after this migration, so nothing is backfilled.

Revision ID: 0060_staff_position_levels
Revises: 0059_admin_opz_plan
"""

import sqlalchemy as sa
from alembic import op


revision = '0060_staff_position_levels'
down_revision = '0059_admin_opz_plan'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('staff', sa.Column('position_id', sa.Integer(), nullable=True))
    op.create_table(
        'staff_position_months',
        sa.Column('staff_id', sa.Integer(), nullable=False),
        sa.Column('month', sa.Date(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('position_id', sa.Integer(), nullable=True),
        sa.Column('position_title', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['staff_id'], ['staff.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('staff_id', 'month'),
    )


def downgrade() -> None:
    op.drop_table('staff_position_months')
    op.drop_column('staff', 'position_id')
