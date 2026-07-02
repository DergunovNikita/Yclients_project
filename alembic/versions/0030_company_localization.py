"""add localization columns to companies

Revision ID: 0030_company_localization
Revises: 0029_company_source_type
Create Date: 2026-07-02 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = '0030_company_localization'
down_revision = '0029_company_source_type'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('companies', sa.Column('country', sa.String(), nullable=True))
    op.add_column('companies', sa.Column('locale', sa.String(), nullable=True))
    op.add_column('companies', sa.Column('currency', sa.String(), nullable=True))
    op.add_column('companies', sa.Column('timezone', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('companies', 'timezone')
    op.drop_column('companies', 'currency')
    op.drop_column('companies', 'locale')
    op.drop_column('companies', 'country')
