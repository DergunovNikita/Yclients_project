"""add encrypted yclients credentials

Revision ID: 0022_yclients_credentials
Revises: 0021_service_kpi_groups
"""

from alembic import op
import sqlalchemy as sa


revision = '0022_yclients_credentials'
down_revision = '0021_service_kpi_groups'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('yclients_credentials', schema='system'):
        op.create_table(
            'yclients_credentials',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('title', sa.String(length=255), nullable=False),
            sa.Column('partner_token_encrypted', sa.Text(), nullable=False),
            sa.Column('login_encrypted', sa.Text(), nullable=False),
            sa.Column('password_encrypted', sa.Text(), nullable=False),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            schema='system',
        )

    if not inspector.has_table('yclients_credential_companies', schema='system'):
        op.create_table(
            'yclients_credential_companies',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('credential_id', sa.Integer(), nullable=False),
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(
                ['credential_id'],
                ['system.yclients_credentials.id'],
                ondelete='CASCADE',
            ),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='system',
        )

    indexes = {
        index['name']
        for index in inspector.get_indexes('yclients_credential_companies', schema='system')
    }
    if 'ix_yclients_credential_companies_credential_id' not in indexes:
        op.create_index(
            'ix_yclients_credential_companies_credential_id',
            'yclients_credential_companies',
            ['credential_id'],
            schema='system',
        )
    if 'ix_yclients_credential_companies_company_id' not in indexes:
        op.create_index(
            'ix_yclients_credential_companies_company_id',
            'yclients_credential_companies',
            ['company_id'],
            unique=True,
            schema='system',
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('yclients_credential_companies', schema='system'):
        indexes = {
            index['name']
            for index in inspector.get_indexes('yclients_credential_companies', schema='system')
        }
        if 'ix_yclients_credential_companies_company_id' in indexes:
            op.drop_index(
                'ix_yclients_credential_companies_company_id',
                table_name='yclients_credential_companies',
                schema='system',
            )
        if 'ix_yclients_credential_companies_credential_id' in indexes:
            op.drop_index(
                'ix_yclients_credential_companies_credential_id',
                table_name='yclients_credential_companies',
                schema='system',
            )
        op.drop_table('yclients_credential_companies', schema='system')

    if inspector.has_table('yclients_credentials', schema='system'):
        op.drop_table('yclients_credentials', schema='system')
