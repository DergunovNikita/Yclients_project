"""add service KPI groups"""

from alembic import op
import sqlalchemy as sa


revision = '0021_service_kpi_groups'
down_revision = '0020_average_check_sources'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('service_kpi_groups', schema='public'):
        op.create_table(
            'service_kpi_groups',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('code', sa.String(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('description', sa.Text(), nullable=True),
            sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('code', name='uq_service_kpi_groups_code'),
            schema='public',
        )
        op.create_index('ix_service_kpi_groups_code', 'service_kpi_groups', ['code'], schema='public')
        op.create_index('ix_service_kpi_groups_is_active', 'service_kpi_groups', ['is_active'], schema='public')

    if not inspector.has_table('service_kpi_assignments', schema='public'):
        op.create_table(
            'service_kpi_assignments',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('service_id', sa.Integer(), nullable=False),
            sa.Column('group_id', sa.Integer(), nullable=False),
            sa.Column('source', sa.String(), nullable=True, server_default='dashboard'),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'service_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['group_id'], ['public.service_kpi_groups.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(
                ['company_id', 'service_id'],
                ['public.service_catalog.company_id', 'public.service_catalog.service_id'],
                ondelete='CASCADE',
            ),
            schema='public',
        )
        op.create_index('ix_service_kpi_assignments_group_id', 'service_kpi_assignments', ['group_id'], schema='public')
        op.create_index('ix_service_kpi_assignments_service_id', 'service_kpi_assignments', ['service_id'], schema='public')


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('service_kpi_assignments', schema='public'):
        indexes = {index['name'] for index in inspector.get_indexes('service_kpi_assignments', schema='public')}
        if 'ix_service_kpi_assignments_service_id' in indexes:
            op.drop_index('ix_service_kpi_assignments_service_id', table_name='service_kpi_assignments', schema='public')
        if 'ix_service_kpi_assignments_group_id' in indexes:
            op.drop_index('ix_service_kpi_assignments_group_id', table_name='service_kpi_assignments', schema='public')
        op.drop_table('service_kpi_assignments', schema='public')

    if inspector.has_table('service_kpi_groups', schema='public'):
        indexes = {index['name'] for index in inspector.get_indexes('service_kpi_groups', schema='public')}
        if 'ix_service_kpi_groups_is_active' in indexes:
            op.drop_index('ix_service_kpi_groups_is_active', table_name='service_kpi_groups', schema='public')
        if 'ix_service_kpi_groups_code' in indexes:
            op.drop_index('ix_service_kpi_groups_code', table_name='service_kpi_groups', schema='public')
        op.drop_table('service_kpi_groups', schema='public')
