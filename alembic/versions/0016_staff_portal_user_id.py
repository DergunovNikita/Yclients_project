"""link portal users to staff rows for dashboard worker filters"""

from alembic import op
import sqlalchemy as sa

revision = '0016_staff_portal_user_id'
down_revision = '0015_portal_users'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('staff')}
    indexes = {index['name'] for index in inspector.get_indexes('staff')}
    if 'portal_user_id' not in columns:
        op.add_column('staff', sa.Column('portal_user_id', sa.Integer(), nullable=True))
    if 'ix_staff_portal_user_id' not in indexes:
        op.create_index('ix_staff_portal_user_id', 'staff', ['portal_user_id'], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index['name'] for index in inspector.get_indexes('staff')}
    columns = {column['name'] for column in inspector.get_columns('staff')}
    if 'ix_staff_portal_user_id' in indexes:
        op.drop_index('ix_staff_portal_user_id', table_name='staff')
    if 'portal_user_id' in columns:
        op.drop_column('staff', 'portal_user_id')
