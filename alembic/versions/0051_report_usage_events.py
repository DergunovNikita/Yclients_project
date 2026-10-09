"""add the report usage log

One row per opening of a report by a signed-in tenant user, so the owner can see which reports are
used and which can be retired. Append-only analytics: no foreign keys, so a deleted user or account
cannot erase or block the history.

Revision ID: 0051_report_usage_events
Revises: 0050_payment_data_through
"""

import sqlalchemy as sa
from alembic import op


revision = '0051_report_usage_events'
down_revision = '0050_payment_data_through'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'report_usage_events',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('portal_account_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=32), nullable=False),
        sa.Column('report_id', sa.String(length=64), nullable=False),
        sa.Column('requested_report_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('usage_day', sa.Date(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=False),
        sa.Column('status_code', sa.SmallInteger(), nullable=False),
        sa.Column('source_status', sa.String(length=16), nullable=True),
        sa.Column('compare_used', sa.Boolean(), nullable=False),
        sa.Column('staff_filter', sa.Boolean(), nullable=False),
        sa.Column('company_filter', sa.Boolean(), nullable=False),
        sa.Column('granularity', sa.String(length=8), nullable=True),
        sa.Column('period_days', sa.Integer(), nullable=True),
        sa.Column('period_preset', sa.String(length=16), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        schema='system',
    )
    op.create_index(
        'ix_report_usage_events_account_created',
        'report_usage_events',
        ['portal_account_id', 'created_at'],
        schema='system',
    )
    op.create_index('ix_report_usage_events_created', 'report_usage_events', ['created_at'], schema='system')


def downgrade() -> None:
    op.drop_index('ix_report_usage_events_created', table_name='report_usage_events', schema='system')
    op.drop_index('ix_report_usage_events_account_created', table_name='report_usage_events', schema='system')
    op.drop_table('report_usage_events', schema='system')
