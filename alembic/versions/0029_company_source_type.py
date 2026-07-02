"""add source_type to companies

Revision ID: 0029_company_source_type
Revises: 0028_platform_admin_no_tenant
Create Date: 2026-07-02 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = '0029_company_source_type'
down_revision = '0028_platform_admin_no_tenant'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'companies',
        sa.Column('source_type', sa.String(), nullable=False, server_default='yclients'),
    )


def downgrade() -> None:
    op.drop_column('companies', 'source_type')
