"""add reporting_start_date to companies

Revision ID: 0037_company_reporting_start
Revises: 0036_session_lifecycle
Create Date: 2026-08-04 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = '0037_company_reporting_start'
down_revision = '0036_session_lifecycle'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('reporting_start_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('companies', 'reporting_start_date')
