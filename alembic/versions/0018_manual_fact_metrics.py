"""add manually entered fact metrics"""

from alembic import op
import sqlalchemy as sa


revision = '0018_manual_fact_metrics'
down_revision = '0017_portal_initial_password'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if inspector.has_table('manual_fact_metrics', schema='public'):
        return

    op.create_table(
        'manual_fact_metrics',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('staff_id', sa.Integer(), nullable=False),
        sa.Column('metric_code', sa.String(), nullable=False),
        sa.Column('value', sa.Float(), nullable=False),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['staff_id'], ['public.staff.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='public',
    )
    op.create_index('ix_manual_fact_metrics_period_start', 'manual_fact_metrics', ['period_start'], schema='public')
    op.create_index('ix_manual_fact_metrics_period_end', 'manual_fact_metrics', ['period_end'], schema='public')
    op.create_index('ix_manual_fact_metrics_company_id', 'manual_fact_metrics', ['company_id'], schema='public')
    op.create_index('ix_manual_fact_metrics_staff_id', 'manual_fact_metrics', ['staff_id'], schema='public')
    op.create_index('ix_manual_fact_metrics_metric_code', 'manual_fact_metrics', ['metric_code'], schema='public')
    op.create_index(
        'uq_manual_fact_metric_period_company_staff_metric',
        'manual_fact_metrics',
        ['period_start', 'period_end', 'company_id', 'staff_id', 'metric_code'],
        unique=True,
        schema='public',
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table('manual_fact_metrics', schema='public'):
        return

    indexes = {index['name'] for index in inspector.get_indexes('manual_fact_metrics', schema='public')}
    for index_name in (
        'uq_manual_fact_metric_period_company_staff_metric',
        'ix_manual_fact_metrics_metric_code',
        'ix_manual_fact_metrics_staff_id',
        'ix_manual_fact_metrics_company_id',
        'ix_manual_fact_metrics_period_end',
        'ix_manual_fact_metrics_period_start',
    ):
        if index_name in indexes:
            op.drop_index(index_name, table_name='manual_fact_metrics', schema='public')
    op.drop_table('manual_fact_metrics', schema='public')
