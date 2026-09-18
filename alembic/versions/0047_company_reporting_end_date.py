"""add reporting_end_date to companies

A branch can stop belonging to the tenant that reports on it. Ownership was a set
(`portal_branches`), so the only levers were "hers forever" and "delete the row and lose
the history with it". The reporting window gets an upper bound to say "hers until this
day", mirroring `reporting_start_date`.

Revision ID: 0047_company_reporting_end
Revises: 0046_admin_barber_roles
"""

import sqlalchemy as sa
from alembic import op


revision = '0047_company_reporting_end'
down_revision = '0046_admin_barber_roles'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('reporting_end_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('companies', 'reporting_end_date')
