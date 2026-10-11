"""drop six single-column fact indexes that production never scans

`pg_stat_user_indexes` on production (read 2026-10-11) shows idx_scan = 0 for these, while the neighbouring
indexes of the same tables have millions of scans. Every `refresh` / `full` reload deletes and reinserts whole
windows of these tables, and each index is maintained on every one of those rows.

* `ix_financial_transactions_client_id`, `ix_goods_transactions_client_id`: the dashboard joins visits by
  `appointments.client_id`, never payments or goods sales by client.
* `ix_goods_transactions_storage_id`, `ix_clients_phone`, `ix_clients_last_visit_date`: no query filters
  or sorts by these columns.
* `ix_comments_record_id`: the only reader is the review twin lookup, which is also keyed by
  `company_id` + `date` and runs on `ix_comments_company_date`.

None of the columns carries a foreign key. Downgrade recreates every index with its original definition
(single-column btree, `public` schema); nothing is lost but the index build time.

Revision ID: 0057_drop_unused_fact_indexes
Revises: 0056_drop_unused_service_sync
"""

from alembic import op


revision = '0057_drop_unused_fact_indexes'
down_revision = '0056_drop_unused_service_sync'
branch_labels = None
depends_on = None

INDEXES = (
    ('ix_financial_transactions_client_id', 'financial_transactions', 'client_id'),
    ('ix_clients_phone', 'clients', 'phone'),
    ('ix_comments_record_id', 'comments', 'record_id'),
    ('ix_goods_transactions_client_id', 'goods_transactions', 'client_id'),
    ('ix_goods_transactions_storage_id', 'goods_transactions', 'storage_id'),
    ('ix_clients_last_visit_date', 'clients', 'last_visit_date'),
)


def upgrade() -> None:
    for name, table, _column in INDEXES:
        op.drop_index(name, table_name=table, if_exists=True)


def downgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column], if_not_exists=True)
