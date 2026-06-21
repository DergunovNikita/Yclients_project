"""multi-tenant portal accounts

Revision ID: 0023_multi_tenant_portal
Revises: 0022_yclients_credentials
"""

from alembic import op
import sqlalchemy as sa


revision = '0023_multi_tenant_portal'
down_revision = '0022_yclients_credentials'
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str, schema: str | None = None) -> bool:
    return any(item['name'] == column for item in inspector.get_columns(table, schema=schema))


def _has_table(inspector, table: str, schema: str | None = None) -> bool:
    return inspector.has_table(table, schema=schema)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    op.execute(sa.text("""
        INSERT INTO system.portal_accounts (label)
        SELECT 'default'
        WHERE NOT EXISTS (SELECT 1 FROM system.portal_accounts)
    """))

    default_account_id = bind.execute(
        sa.text("SELECT id FROM system.portal_accounts ORDER BY id LIMIT 1")
    ).scalar_one()

    if not _has_table(inspector, 'portal_users', schema='system'):
        op.create_table(
            'portal_users',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('portal_account_id', sa.Integer(), nullable=True),
            sa.Column('email', sa.String(length=255), nullable=False),
            sa.Column('password_hash', sa.String(length=255), nullable=False),
            sa.Column('full_name', sa.String(length=255), nullable=True),
            sa.Column('role', sa.String(length=32), nullable=False, server_default='viewer'),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('email_verified_at', sa.DateTime(), nullable=True),
            sa.Column('initial_password', sa.String(length=128), nullable=True),
            sa.Column('password_changed_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('last_login_at', sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(['portal_account_id'], ['system.portal_accounts.id']),
            sa.UniqueConstraint('email', name='uq_portal_users_email'),
            schema='system',
        )
        op.create_index('ix_portal_users_role', 'portal_users', ['role'], schema='system')
        op.create_index(
            'ix_portal_users_portal_account_id',
            'portal_users',
            ['portal_account_id'],
            schema='system',
        )
    elif not _has_column(inspector, 'portal_users', 'portal_account_id', schema='system'):
        op.add_column(
            'portal_users',
            sa.Column('portal_account_id', sa.Integer(), nullable=True),
            schema='system',
        )
        op.create_index(
            'ix_portal_users_portal_account_id',
            'portal_users',
            ['portal_account_id'],
            schema='system',
        )
        op.create_foreign_key(
            'fk_portal_users_portal_account_id',
            'portal_users',
            'portal_accounts',
            ['portal_account_id'],
            ['id'],
            source_schema='system',
            referent_schema='system',
        )

    if not _has_table(inspector, 'portal_user_branches', schema='system'):
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

    if not _has_table(inspector, 'portal_email_tokens', schema='system'):
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

    bind.execute(
        sa.text("""
            UPDATE system.portal_users
            SET portal_account_id = :account_id
            WHERE portal_account_id IS NULL AND role <> 'super_admin'
        """),
        {'account_id': default_account_id},
    )
    op.execute(sa.text("UPDATE system.portal_users SET role = 'platform_admin' WHERE role = 'super_admin'"))

    bind.execute(
        sa.text("""
            INSERT INTO system.portal_branches (portal_account_id, company_id)
            SELECT :account_id, c.id
            FROM companies c
            WHERE NOT EXISTS (
                SELECT 1 FROM system.portal_branches pb WHERE pb.company_id = c.id
            )
        """),
        {'account_id': default_account_id},
    )

    portal_branch_indexes = {
        index['name']
        for index in inspector.get_indexes('portal_branches', schema='system')
    }
    if 'ix_portal_branches_portal_account_id' not in portal_branch_indexes:
        op.create_index(
            'ix_portal_branches_portal_account_id',
            'portal_branches',
            ['portal_account_id'],
            schema='system',
        )
    if 'ix_portal_branches_company_id' not in portal_branch_indexes:
        op.create_index(
            'ix_portal_branches_company_id',
            'portal_branches',
            ['company_id'],
            unique=True,
            schema='system',
        )

    if not _has_column(inspector, 'yclients_credentials', 'portal_account_id', schema='system'):
        op.add_column(
            'yclients_credentials',
            sa.Column('portal_account_id', sa.Integer(), nullable=True),
            schema='system',
        )
        op.create_index(
            'ix_yclients_credentials_portal_account_id',
            'yclients_credentials',
            ['portal_account_id'],
            schema='system',
        )
        op.create_foreign_key(
            'fk_yclients_credentials_portal_account_id',
            'yclients_credentials',
            'portal_accounts',
            ['portal_account_id'],
            ['id'],
            source_schema='system',
            referent_schema='system',
        )
        bind.execute(
            sa.text("""
                UPDATE system.yclients_credentials
                SET portal_account_id = :account_id
                WHERE portal_account_id IS NULL
            """),
            {'account_id': default_account_id},
        )
        op.alter_column('yclients_credentials', 'portal_account_id', nullable=False, schema='system')

    if not _has_column(inspector, 'service_kpi_groups', 'portal_account_id'):
        op.add_column(
            'service_kpi_groups',
            sa.Column('portal_account_id', sa.Integer(), nullable=True),
        )
        op.create_index(
            'ix_service_kpi_groups_portal_account_id',
            'service_kpi_groups',
            ['portal_account_id'],
        )
        op.create_foreign_key(
            'fk_service_kpi_groups_portal_account_id',
            'service_kpi_groups',
            'portal_accounts',
            ['portal_account_id'],
            ['id'],
            referent_schema='system',
        )
        bind.execute(
            sa.text("""
                UPDATE service_kpi_groups
                SET portal_account_id = :account_id
                WHERE portal_account_id IS NULL
            """),
            {'account_id': default_account_id},
        )
        op.alter_column('service_kpi_groups', 'portal_account_id', nullable=False)

    service_group_uniques = {
        constraint['name']
        for constraint in inspector.get_unique_constraints('service_kpi_groups')
    }
    if 'uq_service_kpi_groups_code' in service_group_uniques:
        op.drop_constraint('uq_service_kpi_groups_code', 'service_kpi_groups', type_='unique')
    if 'uq_service_kpi_groups_account_code' not in service_group_uniques:
        op.create_unique_constraint(
            'uq_service_kpi_groups_account_code',
            'service_kpi_groups',
            ['portal_account_id', 'code'],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_column(inspector, 'service_kpi_groups', 'portal_account_id'):
        service_group_uniques = {
            constraint['name']
            for constraint in inspector.get_unique_constraints('service_kpi_groups')
        }
        if 'uq_service_kpi_groups_account_code' in service_group_uniques:
            op.drop_constraint('uq_service_kpi_groups_account_code', 'service_kpi_groups', type_='unique')
        if 'uq_service_kpi_groups_code' not in service_group_uniques:
            op.create_unique_constraint('uq_service_kpi_groups_code', 'service_kpi_groups', ['code'])
        op.drop_constraint('fk_service_kpi_groups_portal_account_id', 'service_kpi_groups', type_='foreignkey')
        op.drop_index('ix_service_kpi_groups_portal_account_id', table_name='service_kpi_groups')
        op.drop_column('service_kpi_groups', 'portal_account_id')

    if _has_column(inspector, 'yclients_credentials', 'portal_account_id', schema='system'):
        op.drop_constraint(
            'fk_yclients_credentials_portal_account_id',
            'yclients_credentials',
            schema='system',
            type_='foreignkey',
        )
        op.drop_index(
            'ix_yclients_credentials_portal_account_id',
            table_name='yclients_credentials',
            schema='system',
        )
        op.drop_column('yclients_credentials', 'portal_account_id', schema='system')

    portal_branch_indexes = {
        index['name']
        for index in inspector.get_indexes('portal_branches', schema='system')
    }
    if 'ix_portal_branches_company_id' in portal_branch_indexes:
        op.drop_index('ix_portal_branches_company_id', table_name='portal_branches', schema='system')
    if 'ix_portal_branches_portal_account_id' in portal_branch_indexes:
        op.drop_index('ix_portal_branches_portal_account_id', table_name='portal_branches', schema='system')

    if _has_column(inspector, 'portal_users', 'portal_account_id', schema='system'):
        op.execute(sa.text("UPDATE system.portal_users SET role = 'super_admin' WHERE role = 'platform_admin'"))
        op.drop_constraint('fk_portal_users_portal_account_id', 'portal_users', schema='system', type_='foreignkey')
        op.drop_index('ix_portal_users_portal_account_id', table_name='portal_users', schema='system')
        op.drop_column('portal_users', 'portal_account_id', schema='system')
