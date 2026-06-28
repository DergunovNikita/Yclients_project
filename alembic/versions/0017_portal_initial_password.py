"""portal user initial password storage and password change tracking"""

from alembic import op
import sqlalchemy as sa

revision = '0017_portal_initial_password'
down_revision = '0016_staff_portal_user_id'
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('portal_users', schema='system')}
    if 'initial_password' not in columns:
        op.add_column(
            'portal_users',
            sa.Column('initial_password', sa.String(length=128), nullable=True),
            schema='system',
        )
    if 'password_changed_at' not in columns:
        op.add_column(
            'portal_users',
            sa.Column('password_changed_at', sa.DateTime(), nullable=True),
            schema='system',
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('portal_users', schema='system')}
    if 'password_changed_at' in columns:
        op.drop_column('portal_users', 'password_changed_at', schema='system')
    if 'initial_password' in columns:
        op.drop_column('portal_users', 'initial_password', schema='system')
