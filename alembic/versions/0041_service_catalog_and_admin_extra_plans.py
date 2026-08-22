"""track current YClients services and administrator extra-service plans

Revision ID: 0041_service_admin_extras
Revises: 0040_month_anchored_facts
Create Date: 2026-08-22 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = '0041_service_admin_extras'
down_revision = '0040_month_anchored_facts'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'service_catalog',
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index(
        'ix_service_catalog_is_active',
        'service_catalog',
        ['is_active'],
        schema='public',
    )
    op.add_column('plan_staff_inputs', sa.Column('extra_services_qty', sa.Float(), nullable=True))
    op.add_column('plan_staff_inputs', sa.Column('extra_services_pct', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('plan_staff_inputs', 'extra_services_pct')
    op.drop_column('plan_staff_inputs', 'extra_services_qty')
    op.drop_index('ix_service_catalog_is_active', table_name='service_catalog', schema='public')
    op.drop_column('service_catalog', 'is_active')
