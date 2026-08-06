"""widen yclients-sourced identifiers to bigint

YClients ids are global counters that outgrew int4: goods_transactions.document_id
reached 2146545972 against a 2147483647 ceiling and incoming rows already exceed it,
which aborted every financial and goods load with NumericValueOutOfRange.

Revision ID: 0038_widen_yclients_ids
Revises: 0037_company_reporting_start
Create Date: 2026-08-06 00:00:00.000000
"""

import sqlalchemy as sa
from alembic import op


revision = '0038_widen_yclients_ids'
down_revision = '0037_company_reporting_start'
branch_labels = None
depends_on = None


# Only ids mirrored from YClients. Internal keys (company_id, staff_id, client_id)
# and small enumerations (expense_id, type_id, storage_id) stay int4.
WIDENED_COLUMNS = (
    ('appointments', 'id'),
    ('appointments', 'external_id'),
    ('transactions', 'id'),
    ('transactions', 'appointment_id'),
    ('financial_transactions', 'id'),
    ('financial_transactions', 'external_id'),
    ('financial_transactions', 'document_id'),
    ('financial_transactions', 'record_id'),
    ('financial_transactions', 'visit_id'),
    ('financial_transactions', 'sold_item_id'),
    ('goods_transactions', 'id'),
    ('goods_transactions', 'external_id'),
    ('goods_transactions', 'document_id'),
    ('goods_transactions', 'good_id'),
    ('comments', 'id'),
    ('comments', 'external_id'),
    ('comments', 'record_id'),
)

# Widening a serial column leaves its sequence on the old type, which would keep
# handing out int4 values and reintroduce the overflow on the next insert.
WIDENED_SEQUENCES = (
    'appointments_id_seq',
    'transactions_id_seq',
    'financial_transactions_id_seq',
    'goods_transactions_id_seq',
    'comments_id_seq',
)

_CAPTURE_VIEWS = """
SELECT c.relname, pg_get_viewdef(c.oid, true)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'v' AND n.nspname = 'public'
ORDER BY c.oid
"""


def _retype(target_type: str) -> None:
    """Retype the columns, carrying the analytics views over the change.

    PostgreSQL refuses to alter a column a view selects from, and setup_analytics
    builds 22 of them on these tables. Definitions are read back from the catalog
    rather than imported from the application, so this migration stays correct even
    if the view SQL changes later. Ordering by oid preserves creation order, which
    matters because v_appointments_enriched and v_financial_transactions_enriched
    both select from v_calendar.

    Statements go through the raw DBAPI cursor: v_average_check_components contains a
    literal percent sign, which psycopg2 would otherwise read as a parameter marker.
    """
    conn = op.get_bind()
    views = conn.execute(sa.text(_CAPTURE_VIEWS)).fetchall()
    cursor = conn.connection.cursor()

    for name, _ in reversed(views):
        cursor.execute(f'DROP VIEW IF EXISTS public."{name}" CASCADE')

    # transactions.appointment_id carries a foreign key to appointments.id; both are
    # altered in this one transaction so the constraint never sees mismatched types.
    for table, column in WIDENED_COLUMNS:
        cursor.execute(f'ALTER TABLE {table} ALTER COLUMN {column} TYPE {target_type}')

    for name, definition in views:
        cursor.execute(f'CREATE VIEW public."{name}" AS {definition}')


def upgrade() -> None:
    _retype('BIGINT')
    for sequence in WIDENED_SEQUENCES:
        op.execute(sa.text(f'ALTER SEQUENCE {sequence} AS BIGINT'))


def downgrade() -> None:
    # Fails loudly if any stored id already exceeds int4 rather than truncating it.
    for sequence in WIDENED_SEQUENCES:
        op.execute(sa.text(f'ALTER SEQUENCE {sequence} AS INTEGER'))
    _retype('INTEGER')
