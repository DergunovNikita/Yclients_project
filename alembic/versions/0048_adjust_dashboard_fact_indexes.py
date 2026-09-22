"""extend company+date indexes to comments/goods_transactions, drop a superseded index

comments and goods_transactions are filtered by exactly `company_id = X AND date BETWEEN
a, b` (dashboard_reports._nps_payload, dashboard_service._goods_revenue_filters) — the
same shape migration 0043 fixed for appointments and financial_transactions. Both tables
still carry only separate single-column ix_*_company_id / ix_*_date indexes, so Postgres
falls back to a BitmapAnd of the two instead of one composite scan.

ix_staff_schedules_company_id shows 0 idx_scan since the last VACUUM ANALYZE (verified
against the local copy), while the 0042 composite that leads with the same column
(company_id, staff_id, date, slot_from, slot_to) absorbs every staff_schedules lookup
(1M+ idx_scan) — the single-column index is now dead weight kept up to date on every
write for no read that uses it.

All three tables here are small (comments ~77k rows, goods_transactions ~29k rows,
staff_schedules ~79k rows) and the index build/drop is well under a second, so this uses
plain (non-CONCURRENTLY) DDL rather than op.get_context().autocommit_block(). Entering an
autocommit block unconditionally commits whatever the enclosing alembic transaction has
done so far (see MigrationContext.autocommit_block docs); since env.py runs the whole
pending batch in one transaction, that would split the "batch is one all-or-nothing unit"
guarantee the deploy pipeline relies on if this migration ever lands in the same batch as
another pending one. A brief lock on three small tables is the better trade. SET LOCAL
still bounds the wait so a lock held by ETL writes fails fast instead of hanging the
deploy behind it (same reasoning as 0043 — see its own note on what LOCAL does and does
not scope to within a batch).

Revision ID: 0048_adjust_fact_indexes
Revises: 0047_company_reporting_end
Create Date: 2026-09-21 00:00:00.000000
"""

from alembic import op


revision = '0048_adjust_fact_indexes'
down_revision = '0047_company_reporting_end'
branch_labels = None
depends_on = None

NEW_INDEXES = (
    ('ix_comments_company_date', 'comments', ['company_id', 'date']),
    ('ix_goods_transactions_company_date', 'goods_transactions', ['company_id', 'date']),
)

# Only one single-column company index is dropped, and that asymmetry is measured, not an
# oversight. ix_staff_schedules_company_id had idx_scan = 0 against 1,120,684 on the 0042
# composite that leads with the same column, so nothing read it. The equivalents on the two
# tables above are demonstrably live (pg_stat_user_indexes on a copy of production:
# ix_comments_company_id 162 scans, ix_goods_transactions_company_id 24,642), so they stay.
# Their counters predate the composites added here and cannot be attributed cleanly until the
# stats are reset, which is the evidence a later migration would need before dropping them.
DROPPED_INDEX = ('ix_staff_schedules_company_id', 'staff_schedules', ['company_id'])


# The DDL below is unguarded (no IF NOT EXISTS / IF EXISTS) on purpose. A from-zero
# database never runs it: migrate.py hands a fresh DB to scripts.bootstrap_db, which
# builds the schema straight from Base.metadata and stamps it at head — verified on a
# scratch database, which came out with both composites present, ix_staff_schedules_
# company_id correctly absent, and alembic_version at 0048 without this file executing.
# So the only path that runs this is an existing database at 0047, where the indexes
# are in exactly the state these statements expect.
def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for name, table, columns in NEW_INDEXES:
        op.create_index(name, table, columns, schema='public')
    name, table, _columns = DROPPED_INDEX
    op.drop_index(name, table_name=table, schema='public')


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    name, table, columns = DROPPED_INDEX
    op.create_index(name, table, columns, schema='public')
    for name, table, _columns in reversed(NEW_INDEXES):
        op.drop_index(name, table_name=table, schema='public')
