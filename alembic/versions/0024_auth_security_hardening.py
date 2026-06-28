"""auth security hardening: token_version, refresh tokens, email cooldowns, onboarding

Revision ID: 0024_auth_security_hardening
Revises: 0023_multi_tenant_portal
"""

from alembic import op
import sqlalchemy as sa


revision = '0024_auth_security_hardening'
down_revision = '0023_multi_tenant_portal'
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str, schema: str | None = None) -> bool:
    return any(item['name'] == column for item in inspector.get_columns(table, schema=schema))


def _has_table(inspector, table: str, schema: str | None = None) -> bool:
    return inspector.has_table(table, schema=schema)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, 'portal_users', 'token_version', schema='system'):
        op.add_column(
            'portal_users',
            sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'),
            schema='system',
        )

    if not _has_column(inspector, 'portal_users', 'email_verification_sent_at', schema='system'):
        op.add_column(
            'portal_users',
            sa.Column('email_verification_sent_at', sa.DateTime(), nullable=True),
            schema='system',
        )

    if not _has_column(inspector, 'portal_users', 'password_reset_sent_at', schema='system'):
        op.add_column(
            'portal_users',
            sa.Column('password_reset_sent_at', sa.DateTime(), nullable=True),
            schema='system',
        )

    if not _has_column(inspector, 'portal_users', 'onboarding_completed_at', schema='system'):
        op.add_column(
            'portal_users',
            sa.Column('onboarding_completed_at', sa.DateTime(), nullable=True),
            schema='system',
        )
        # Backfill: existing users (incl. platform_admin and pre-existing owners) are
        # treated as already onboarded so we don't lock anyone out of the dashboard.
        bind.execute(sa.text(
            "UPDATE system.portal_users "
            "SET onboarding_completed_at = COALESCE(created_at, CURRENT_TIMESTAMP)"
        ))

    if not _has_table(inspector, 'portal_refresh_tokens', schema='system'):
        op.create_table(
            'portal_refresh_tokens',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('user_id', sa.Integer(), nullable=False),
            sa.Column('token_hash', sa.String(length=64), nullable=False),
            sa.Column('expires_at', sa.DateTime(), nullable=False),
            sa.Column('revoked_at', sa.DateTime(), nullable=True),
            sa.Column('last_used_at', sa.DateTime(), nullable=True),
            sa.Column('user_agent', sa.String(length=500), nullable=True),
            sa.Column('device_label', sa.String(length=100), nullable=True),
            sa.Column('ip_hash', sa.String(length=64), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.ForeignKeyConstraint(['user_id'], ['system.portal_users.id'], ondelete='CASCADE'),
            sa.UniqueConstraint('token_hash', name='uq_portal_refresh_tokens_hash'),
            schema='system',
        )
        op.create_index(
            'ix_portal_refresh_tokens_user_id',
            'portal_refresh_tokens',
            ['user_id'],
            schema='system',
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, 'portal_refresh_tokens', schema='system'):
        op.drop_index(
            'ix_portal_refresh_tokens_user_id',
            table_name='portal_refresh_tokens',
            schema='system',
        )
        op.drop_table('portal_refresh_tokens', schema='system')

    for column in ('onboarding_completed_at', 'password_reset_sent_at', 'email_verification_sent_at', 'token_version'):
        if _has_column(inspector, 'portal_users', column, schema='system'):
            op.drop_column('portal_users', column, schema='system')
