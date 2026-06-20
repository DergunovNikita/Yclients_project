"""portal user initial password storage and password change tracking"""

from alembic import op
import sqlalchemy as sa

revision = '0017_portal_initial_password'
down_revision = '0016_staff_portal_user_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'portal_users',
        sa.Column('initial_password', sa.String(length=128), nullable=True),
        schema='system',
    )
    op.add_column(
        'portal_users',
        sa.Column('password_changed_at', sa.DateTime(), nullable=True),
        schema='system',
    )


def downgrade() -> None:
    op.drop_column('portal_users', 'password_changed_at', schema='system')
    op.drop_column('portal_users', 'initial_password', schema='system')
