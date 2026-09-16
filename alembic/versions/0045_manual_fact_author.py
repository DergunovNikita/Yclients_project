"""record who entered a manual fact

The editors become per-employee: a staff member enters their own value and the branch
manager corrects it. Without an author the two are indistinguishable after the fact.

Revision ID: 0045_manual_fact_author
Revises: 0044_zero_record_id
"""
from alembic import op
import sqlalchemy as sa

revision = '0045_manual_fact_author'
down_revision = '0044_zero_record_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('manual_fact_metrics')}
    if 'updated_by_user_id' not in columns:
        op.add_column(
            'manual_fact_metrics',
            sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('manual_fact_metrics')}
    if 'updated_by_user_id' in columns:
        op.drop_column('manual_fact_metrics', 'updated_by_user_id')
