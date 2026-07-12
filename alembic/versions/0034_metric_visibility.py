"""per-tenant, per-role money metric visibility overrides

Revision ID: 0034_metric_visibility
Revises: 0033_tenant_upstream_entities
Create Date: 2026-07-11 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0034_metric_visibility'
down_revision = '0033_tenant_upstream_entities'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'portal_metric_visibility',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('portal_account_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.Column('visible_codes', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(
            ['portal_account_id'],
            ['system.portal_accounts.id'],
            ondelete='CASCADE',
        ),
        schema='system',
    )
    op.create_index(
        'uq_portal_metric_visibility_account_role',
        'portal_metric_visibility',
        ['portal_account_id', 'role'],
        unique=True,
        schema='system',
    )


def downgrade() -> None:
    op.drop_index(
        'uq_portal_metric_visibility_account_role',
        table_name='portal_metric_visibility',
        schema='system',
    )
    op.drop_table('portal_metric_visibility', schema='system')
