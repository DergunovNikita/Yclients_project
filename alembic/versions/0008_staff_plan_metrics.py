"""support staff-level plan metrics"""

from alembic import op
import sqlalchemy as sa


revision = '0008_staff_plan_metrics'
down_revision = '0007_plan_metrics'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('plan_metrics', schema='public'):
        return

    columns = {column['name'] for column in inspector.get_columns('plan_metrics', schema='public')}
    if 'staff_id' not in columns:
        op.add_column('plan_metrics', sa.Column('staff_id', sa.Integer(), nullable=True), schema='public')
        op.create_foreign_key(
            'fk_plan_metrics_staff_id',
            'plan_metrics',
            'staff',
            ['staff_id'],
            ['id'],
            source_schema='public',
            referent_schema='public',
            ondelete='CASCADE',
        )
        op.create_index('ix_plan_metrics_staff_id', 'plan_metrics', ['staff_id'], schema='public')
    if 'staff_category' not in columns:
        op.add_column('plan_metrics', sa.Column('staff_category', sa.String(), nullable=True), schema='public')
        op.create_index('ix_plan_metrics_staff_category', 'plan_metrics', ['staff_category'], schema='public')

    constraints = {
        constraint['name'] for constraint in inspector.get_unique_constraints('plan_metrics', schema='public')
    }
    if 'uq_plan_metric_period_company_metric' in constraints:
        op.drop_constraint(
            'uq_plan_metric_period_company_metric',
            'plan_metrics',
            schema='public',
            type_='unique',
        )

    indexes = {index['name'] for index in inspector.get_indexes('plan_metrics', schema='public')}
    if 'uq_plan_metric_period_company_metric_branch' not in indexes:
        op.create_index(
            'uq_plan_metric_period_company_metric_branch',
            'plan_metrics',
            ['period_start', 'period_end', 'company_id', 'metric_code'],
            unique=True,
            schema='public',
            postgresql_where=sa.text('staff_id IS NULL'),
        )
    if 'uq_plan_metric_period_company_staff_metric' not in indexes:
        op.create_index(
            'uq_plan_metric_period_company_staff_metric',
            'plan_metrics',
            ['period_start', 'period_end', 'company_id', 'staff_id', 'metric_code'],
            unique=True,
            schema='public',
            postgresql_where=sa.text('staff_id IS NOT NULL'),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('plan_metrics', schema='public'):
        return

    indexes = {index['name'] for index in inspector.get_indexes('plan_metrics', schema='public')}
    if 'uq_plan_metric_period_company_staff_metric' in indexes:
        op.drop_index('uq_plan_metric_period_company_staff_metric', table_name='plan_metrics', schema='public')
    if 'uq_plan_metric_period_company_metric_branch' in indexes:
        op.drop_index('uq_plan_metric_period_company_metric_branch', table_name='plan_metrics', schema='public')
    if 'ix_plan_metrics_staff_category' in indexes:
        op.drop_index('ix_plan_metrics_staff_category', table_name='plan_metrics', schema='public')
    if 'ix_plan_metrics_staff_id' in indexes:
        op.drop_index('ix_plan_metrics_staff_id', table_name='plan_metrics', schema='public')

    constraints = {constraint['name'] for constraint in inspector.get_foreign_keys('plan_metrics', schema='public')}
    if 'fk_plan_metrics_staff_id' in constraints:
        op.drop_constraint('fk_plan_metrics_staff_id', 'plan_metrics', schema='public', type_='foreignkey')

    columns = {column['name'] for column in inspector.get_columns('plan_metrics', schema='public')}
    if 'staff_category' in columns:
        op.drop_column('plan_metrics', 'staff_category', schema='public')
    if 'staff_id' in columns:
        op.drop_column('plan_metrics', 'staff_id', schema='public')

    constraints = {
        constraint['name'] for constraint in inspector.get_unique_constraints('plan_metrics', schema='public')
    }
    if 'uq_plan_metric_period_company_metric' not in constraints:
        op.create_unique_constraint(
            'uq_plan_metric_period_company_metric',
            'plan_metrics',
            ['period_start', 'period_end', 'company_id', 'metric_code'],
            schema='public',
        )
