"""record the last day a hand-entered payment total covers

A Yandex Pay sum is typed in for a month, but it may be entered before the month is over. The
date says where the figure stops, so the report can compare it with YClients for the same
stretch instead of treating a half-month sum as a whole month.

Revision ID: 0050_payment_data_through
Revises: 0049_manual_payment_amounts
"""

import sqlalchemy as sa
from alembic import op


revision = '0050_payment_data_through'
down_revision = '0049_manual_payment_amounts'
branch_labels = None
depends_on = None

CHECK_NAME = 'ck_manual_payment_amounts_data_through_in_period'


def upgrade() -> None:
    op.add_column('manual_payment_amounts', sa.Column('data_through', sa.Date(), nullable=True), schema='public')
    # A total typed on day X cannot cover days after X; for a closed month that is its last day.
    # `updated_at` is naive UTC, so its date can trail the branch day by one: GREATEST keeps a
    # value entered just after local midnight of the 1st inside the month.
    op.execute(
        """
        UPDATE public.manual_payment_amounts
        SET data_through = GREATEST(period_start, LEAST(period_end, updated_at::date))
        """
    )
    op.alter_column('manual_payment_amounts', 'data_through', nullable=False, schema='public')
    op.create_check_constraint(
        CHECK_NAME,
        'manual_payment_amounts',
        'data_through BETWEEN period_start AND period_end',
        schema='public',
    )


def downgrade() -> None:
    op.drop_constraint(CHECK_NAME, 'manual_payment_amounts', schema='public', type_='check')
    op.drop_column('manual_payment_amounts', 'data_through', schema='public')
