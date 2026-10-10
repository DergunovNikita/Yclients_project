"""drop the analytics and Z-report tables

The sync wrote six tables (YClients analytics overall/charts/sources/statuses and the Z-report) that no
dashboard, report or export ever read, at the cost of several throttled requests per branch per run.
The writers are gone, so the tables go too. Downgrade recreates the empty tables exactly as 0051 left
them; the rows are not restored.

Revision ID: 0052_drop_analytics_tables
Revises: 0051_report_usage_events
"""

import sqlalchemy as sa
from alembic import op


revision = '0052_drop_analytics_tables'
down_revision = '0051_report_usage_events'
branch_labels = None
depends_on = None


# No foreign keys between these tables: each one only points at companies.
TABLES = (
    'analytics_overall',
    'analytics_daily_metrics',
    'analytics_record_sources',
    'analytics_record_statuses',
    'z_reports',
    'z_report_payments',
)


def upgrade() -> None:
    for table in TABLES:
        op.execute(sa.text(f'DROP TABLE IF EXISTS {table}'))


def _company_id() -> list:
    return [
        sa.Column('company_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
    ]


def _index(table: str, columns: list[str]) -> None:
    for column in columns:
        op.create_index(f'ix_{table}_{column}', table, [column])


def downgrade() -> None:
    op.create_table(
        'analytics_overall',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('date_from', sa.Date(), nullable=False),
        sa.Column('date_to', sa.Date(), nullable=False),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.Column('income_total', sa.Float(), nullable=True),
        sa.Column('income_total_prev', sa.Float(), nullable=True),
        sa.Column('income_total_change', sa.Float(), nullable=True),
        sa.Column('income_services', sa.Float(), nullable=True),
        sa.Column('income_services_prev', sa.Float(), nullable=True),
        sa.Column('income_goods', sa.Float(), nullable=True),
        sa.Column('income_goods_prev', sa.Float(), nullable=True),
        sa.Column('income_average', sa.Float(), nullable=True),
        sa.Column('income_average_prev', sa.Float(), nullable=True),
        sa.Column('income_average_services', sa.Float(), nullable=True),
        sa.Column('income_average_services_prev', sa.Float(), nullable=True),
        sa.Column('fullness_current', sa.Float(), nullable=True),
        sa.Column('fullness_previous', sa.Float(), nullable=True),
        sa.Column('fullness_change', sa.Float(), nullable=True),
        sa.Column('records_completed', sa.Integer(), nullable=True),
        sa.Column('records_pending', sa.Integer(), nullable=True),
        sa.Column('records_canceled', sa.Integer(), nullable=True),
        sa.Column('records_total', sa.Integer(), nullable=True),
        sa.Column('records_total_prev', sa.Integer(), nullable=True),
        sa.Column('records_change', sa.Float(), nullable=True),
        sa.Column('clients_total', sa.Integer(), nullable=True),
        sa.Column('clients_new', sa.Integer(), nullable=True),
        sa.Column('clients_new_percent', sa.Float(), nullable=True),
        sa.Column('clients_return', sa.Integer(), nullable=True),
        sa.Column('clients_return_percent', sa.Float(), nullable=True),
        sa.Column('clients_active', sa.Integer(), nullable=True),
        sa.Column('clients_lost', sa.Integer(), nullable=True),
        sa.Column('clients_lost_percent', sa.Float(), nullable=True),
        *_company_id(),
        sa.PrimaryKeyConstraint('id'),
    )
    _index('analytics_overall', ['company_id', 'date_from', 'date_to'])

    op.create_table(
        'analytics_daily_metrics',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('date', sa.Date(), nullable=False),
        sa.Column('metric_type', sa.String(), nullable=False),
        sa.Column('label', sa.String(), nullable=True),
        sa.Column('value', sa.Float(), nullable=True),
        *_company_id(),
        sa.PrimaryKeyConstraint('id'),
    )
    _index('analytics_daily_metrics', ['company_id', 'date', 'metric_type'])

    for table in ('analytics_record_sources', 'analytics_record_statuses'):
        op.create_table(
            table,
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('date_from', sa.Date(), nullable=True),
            sa.Column('date_to', sa.Date(), nullable=True),
            sa.Column('label', sa.String(), nullable=False),
            sa.Column('value', sa.Integer(), nullable=True),
            *_company_id(),
            sa.PrimaryKeyConstraint('id'),
        )
        _index(table, ['company_id', 'date_from', 'date_to'])

    op.create_table(
        'z_reports',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('clients', sa.Integer(), nullable=True),
        sa.Column('clients_average', sa.Float(), nullable=True),
        sa.Column('records', sa.Integer(), nullable=True),
        sa.Column('records_average', sa.Float(), nullable=True),
        sa.Column('visit_records', sa.Integer(), nullable=True),
        sa.Column('visit_records_average', sa.Float(), nullable=True),
        sa.Column('non_visit_records', sa.Integer(), nullable=True),
        sa.Column('non_visit_records_average', sa.Float(), nullable=True),
        sa.Column('targets', sa.Integer(), nullable=True),
        sa.Column('targets_paid', sa.Float(), nullable=True),
        sa.Column('goods_count', sa.Integer(), nullable=True),
        sa.Column('goods_paid', sa.Float(), nullable=True),
        sa.Column('certificates_count', sa.Integer(), nullable=True),
        sa.Column('certificates_paid', sa.Float(), nullable=True),
        sa.Column('abonement_count', sa.Integer(), nullable=True),
        sa.Column('abonement_paid', sa.Float(), nullable=True),
        sa.Column('total_accounts', sa.Float(), nullable=True),
        sa.Column('total_discount', sa.Float(), nullable=True),
        sa.Column('currency', sa.String(), nullable=True),
        *_company_id(),
        sa.PrimaryKeyConstraint('id'),
    )
    _index('z_reports', ['company_id', 'report_date'])

    op.create_table(
        'z_report_payments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('report_date', sa.Date(), nullable=False),
        sa.Column('payment_group', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=True),
        sa.Column('amount', sa.Float(), nullable=True),
        *_company_id(),
        sa.PrimaryKeyConstraint('id'),
    )
    _index('z_report_payments', ['company_id', 'payment_group', 'report_date'])
