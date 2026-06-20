"""link portal users to staff rows for dashboard worker filters"""

from alembic import op
import sqlalchemy as sa

revision = '0016_staff_portal_user_id'
down_revision = '0015_portal_users'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('staff', sa.Column('portal_user_id', sa.Integer(), nullable=True))
    op.create_index('ix_staff_portal_user_id', 'staff', ['portal_user_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_staff_portal_user_id', table_name='staff')
    op.drop_column('staff', 'portal_user_id')
