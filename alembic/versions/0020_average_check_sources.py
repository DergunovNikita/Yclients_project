"""average check source diagnostics

Revision ID: 0020_average_check_sources
Revises: 0019_plan_settings
"""

from alembic import op
import sqlalchemy as sa


revision = '0020_average_check_sources'
down_revision = '0019_plan_settings'
branch_labels = None
depends_on = None


def upgrade():
    inspector = sa.inspect(op.get_bind())
    columns = {column['name'] for column in inspector.get_columns('financial_transactions')}
    if 'expense_title' not in columns:
        op.add_column('financial_transactions', sa.Column('expense_title', sa.String(), nullable=True))
    if not inspector.has_table('sync_source_states'):
        op.create_table(
            'sync_source_states',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('source', sa.String(), nullable=False),
            sa.Column('period_start', sa.Date(), nullable=False),
            sa.Column('period_end', sa.Date(), nullable=False),
            sa.Column('synced_at', sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
            sa.PrimaryKeyConstraint('company_id', 'source'),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())
    if inspector.has_table('sync_source_states'):
        op.drop_table('sync_source_states')
    columns = {column['name'] for column in inspector.get_columns('financial_transactions')}
    if 'expense_title' in columns:
        op.drop_column('financial_transactions', 'expense_title')
