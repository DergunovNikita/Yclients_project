"""add is_demo flag to portal users and accounts

Revision ID: 0031_demo_flag
Revises: 0030_company_localization
Create Date: 2026-07-07 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = '0031_demo_flag'
down_revision = '0030_company_localization'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'portal_users',
        sa.Column('is_demo', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        schema='system',
    )
    op.add_column(
        'portal_accounts',
        sa.Column('is_demo', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        schema='system',
    )


def downgrade() -> None:
    op.drop_column('portal_accounts', 'is_demo', schema='system')
    op.drop_column('portal_users', 'is_demo', schema='system')
