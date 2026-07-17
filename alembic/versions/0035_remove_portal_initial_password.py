"""remove plaintext portal initial passwords

Revision ID: 0035_remove_initial_password
Revises: 0034_metric_visibility
Create Date: 2026-07-17 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0035_remove_initial_password'
down_revision = '0034_metric_visibility'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column('portal_users', 'initial_password', schema='system')


def downgrade() -> None:
    op.add_column(
        'portal_users',
        sa.Column('initial_password', sa.String(length=128), nullable=True),
        schema='system',
    )
