"""portal users, branch assignments, and email tokens for personal cabinets"""

from alembic import op
import sqlalchemy as sa

revision = '0015_portal_users'
down_revision = '0014_branch_scoped_catalogs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'portal_users',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('email', sa.String(length=255), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('full_name', sa.String(length=255), nullable=True),
        sa.Column('role', sa.String(length=32), nullable=False, server_default='viewer'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('email_verified_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('last_login_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('email', name='uq_portal_users_email'),
        schema='system',
    )
    op.create_index('ix_portal_users_role', 'portal_users', ['role'], schema='system')

    op.create_table(
        'portal_user_branches',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['system.portal_users.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('user_id', 'company_id', name='uq_portal_user_branch'),
        schema='system',
    )
    op.create_index('ix_portal_user_branches_user_id', 'portal_user_branches', ['user_id'], schema='system')
    op.create_index('ix_portal_user_branches_company_id', 'portal_user_branches', ['company_id'], schema='system')

    op.create_table(
        'portal_email_tokens',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('token_hash', sa.String(length=64), nullable=False),
        sa.Column('purpose', sa.String(length=16), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.ForeignKeyConstraint(['user_id'], ['system.portal_users.id'], ondelete='CASCADE'),
        sa.UniqueConstraint('token_hash', name='uq_portal_email_tokens_hash'),
        schema='system',
    )
    op.create_index('ix_portal_email_tokens_user_id', 'portal_email_tokens', ['user_id'], schema='system')


def downgrade() -> None:
    op.drop_table('portal_email_tokens', schema='system')
    op.drop_table('portal_user_branches', schema='system')
    op.drop_index('ix_portal_users_role', table_name='portal_users', schema='system')
    op.drop_table('portal_users', schema='system')
