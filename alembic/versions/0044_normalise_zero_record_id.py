"""normalise the zero record_id sentinel on financial transactions

YClients sends `record_id: 0` for a sale with no visit behind it — a counter purchase —
and never sends null. Stored verbatim it reads as "there is a visit" to every IS NULL
check, the appointment is never found, and the dashboard drops the sale from revenue.

Revision ID: 0044_zero_record_id
Revises: 0043_dashboard_fact_indexes
"""
from alembic import op

revision = '0044_zero_record_id'
down_revision = '0043_dashboard_fact_indexes'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE financial_transactions SET record_id = NULL WHERE record_id = 0")


def downgrade():
    # The sentinel and a genuine absence are indistinguishable once normalised; every
    # NULL here came from a zero, because YClients never sends null for this field.
    op.execute("UPDATE financial_transactions SET record_id = 0 WHERE record_id IS NULL")
