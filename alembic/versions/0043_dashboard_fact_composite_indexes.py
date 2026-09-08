"""index dashboard fact lookups by branch and date

Every dashboard query filters facts by one branch and a date window, but the tables
carried only single-column indexes, so each read walked the whole history of the branch.

Revision ID: 0043_dashboard_fact_indexes
Revises: 0042_staff_schedule_index
Create Date: 2026-09-07 00:00:00.000000
"""

from alembic import op


revision = '0043_dashboard_fact_indexes'
down_revision = '0042_staff_schedule_index'
branch_labels = None
depends_on = None

INDEXES = (
    ('ix_appointments_company_date', 'appointments', ['company_id', 'date']),
    ('ix_appointments_company_create_date', 'appointments', ['company_id', 'create_date']),
    (
        'ix_financial_transactions_company_date',
        'financial_transactions',
        ['company_id', 'date'],
    ),
)


def upgrade() -> None:
    # CREATE INDEX takes a SHARE lock, which the sync's bulk writes conflict with. Without
    # a bound the migration would queue behind a running sync for as long as it takes, and
    # every write would queue behind the migration. Failing fast is the recoverable outcome:
    # the deploy stops before restarting the containers and the next tick retries.
    op.execute("SET lock_timeout = '5s'")
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns, schema='public')


def downgrade() -> None:
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table, schema='public')
