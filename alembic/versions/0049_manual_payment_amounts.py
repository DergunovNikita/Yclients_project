"""add manually entered payment-method totals (Yandex Pay)

YClients exposes no list of Yandex Pay payments, so the monthly total per branch is typed
in by hand. It cannot live in `manual_fact_metrics`: that table is keyed by staff row
(`staff_id NOT NULL`) and everything reading it assumes a per-employee value.

Revision ID: 0049_manual_payment_amounts
Revises: 0048_adjust_fact_indexes
"""

import sqlalchemy as sa
from alembic import op


revision = '0049_manual_payment_amounts'
down_revision = '0048_adjust_fact_indexes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'manual_payment_amounts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('period_start', sa.Date(), nullable=False),
        sa.Column('period_end', sa.Date(), nullable=False),
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('method_code', sa.String(length=32), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('source', sa.String(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.Column('updated_by_user_id', sa.Integer(), nullable=True),
        sa.CheckConstraint('amount >= 0', name='ck_manual_payment_amounts_amount_non_negative'),
        sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        schema='public',
    )
    op.create_index('ix_manual_payment_amounts_company_id', 'manual_payment_amounts', ['company_id'], schema='public')
    op.create_index(
        'uq_manual_payment_amounts_period_company_method',
        'manual_payment_amounts',
        ['period_start', 'company_id', 'method_code'],
        unique=True,
        schema='public',
    )


def downgrade() -> None:
    op.drop_index(
        'uq_manual_payment_amounts_period_company_method', table_name='manual_payment_amounts', schema='public'
    )
    op.drop_index('ix_manual_payment_amounts_company_id', table_name='manual_payment_amounts', schema='public')
    op.drop_table('manual_payment_amounts', schema='public')
