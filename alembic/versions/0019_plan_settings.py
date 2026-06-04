"""add monthly plan settings"""

from alembic import op
import sqlalchemy as sa


revision = '0019_plan_settings'
down_revision = '0018_manual_fact_metrics'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table('plan_branch_settings', schema='public'):
        op.create_table(
            'plan_branch_settings',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('period_start', sa.Date(), nullable=False),
            sa.Column('period_end', sa.Date(), nullable=False),
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('wax_pct', sa.Float(), nullable=True),
            sa.Column('head_care_pct', sa.Float(), nullable=True),
            sa.Column('face_care_pct', sa.Float(), nullable=True),
            sa.Column('camouflage_pct', sa.Float(), nullable=True),
            sa.Column('cosmo_pct', sa.Float(), nullable=True),
            sa.Column('opz_pct', sa.Float(), nullable=True),
            sa.Column('cosmo_price', sa.Float(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            schema='public',
        )
        op.create_index('ix_plan_branch_settings_period_start', 'plan_branch_settings', ['period_start'], schema='public')
        op.create_index('ix_plan_branch_settings_period_end', 'plan_branch_settings', ['period_end'], schema='public')
        op.create_index('ix_plan_branch_settings_company_id', 'plan_branch_settings', ['company_id'], schema='public')
        op.create_index(
            'uq_plan_branch_setting_period_company',
            'plan_branch_settings',
            ['period_start', 'period_end', 'company_id'],
            unique=True,
            schema='public',
        )

    if not inspector.has_table('plan_staff_inputs', schema='public'):
        op.create_table(
            'plan_staff_inputs',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('period_start', sa.Date(), nullable=False),
            sa.Column('period_end', sa.Date(), nullable=False),
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('staff_id', sa.Integer(), nullable=False),
            sa.Column('staff_category', sa.String(), nullable=False),
            sa.Column('clients', sa.Float(), nullable=True),
            sa.Column('avg_check_total', sa.Float(), nullable=True),
            sa.Column('reviews_qty', sa.Float(), nullable=True),
            sa.Column('cosmo_qty', sa.Float(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['staff_id'], ['public.staff.id'], ondelete='CASCADE'),
            sa.PrimaryKeyConstraint('id'),
            schema='public',
        )
        op.create_index('ix_plan_staff_inputs_period_start', 'plan_staff_inputs', ['period_start'], schema='public')
        op.create_index('ix_plan_staff_inputs_period_end', 'plan_staff_inputs', ['period_end'], schema='public')
        op.create_index('ix_plan_staff_inputs_company_id', 'plan_staff_inputs', ['company_id'], schema='public')
        op.create_index('ix_plan_staff_inputs_staff_id', 'plan_staff_inputs', ['staff_id'], schema='public')
        op.create_index('ix_plan_staff_inputs_staff_category', 'plan_staff_inputs', ['staff_category'], schema='public')
        op.create_index(
            'uq_plan_staff_input_period_company_staff',
            'plan_staff_inputs',
            ['period_start', 'period_end', 'company_id', 'staff_id'],
            unique=True,
            schema='public',
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if inspector.has_table('plan_staff_inputs', schema='public'):
        indexes = {index['name'] for index in inspector.get_indexes('plan_staff_inputs', schema='public')}
        for index_name in (
            'uq_plan_staff_input_period_company_staff',
            'ix_plan_staff_inputs_staff_category',
            'ix_plan_staff_inputs_staff_id',
            'ix_plan_staff_inputs_company_id',
            'ix_plan_staff_inputs_period_end',
            'ix_plan_staff_inputs_period_start',
        ):
            if index_name in indexes:
                op.drop_index(index_name, table_name='plan_staff_inputs', schema='public')
        op.drop_table('plan_staff_inputs', schema='public')

    if inspector.has_table('plan_branch_settings', schema='public'):
        indexes = {index['name'] for index in inspector.get_indexes('plan_branch_settings', schema='public')}
        for index_name in (
            'uq_plan_branch_setting_period_company',
            'ix_plan_branch_settings_company_id',
            'ix_plan_branch_settings_period_end',
            'ix_plan_branch_settings_period_start',
        ):
            if index_name in indexes:
                op.drop_index(index_name, table_name='plan_branch_settings', schema='public')
        op.drop_table('plan_branch_settings', schema='public')
