"""drop the legacy catalog tables and the write-only columns

The sync wrote each of seven catalogs twice: a global-id legacy table and the branch-scoped `*_catalog`
twin the dashboards read. The legacy twins of service categories, positions, cash accounts, storages, good
categories and goods are read by nothing any more, so they go, together with columns that were written on
every run but never read (`companies.country/locale/currency`, `groups.access`, `staff.avatar_url`,
`sync_jobs.cancel_requested`). The legacy `services` table stays: `service_labels` still points at it.

Downgrade recreates the empty tables and the columns as 0053 left them; no rows are restored. Re-added
columns land at the end of their table, not at their former position.

Revision ID: 0054_drop_legacy_catalogs
Revises: 0053_refresh_reuse_detection
"""

import sqlalchemy as sa
from alembic import op


revision = '0054_drop_legacy_catalogs'
down_revision = '0053_refresh_reuse_detection'
branch_labels = None
depends_on = None


# Each only points at companies; nothing points at them.
LEGACY_TABLES = (
    'service_categories',
    'staff_positions',
    'accounts',
    'storages',
    'good_categories',
    'goods',
)

# (table, schema, column, type factory, server default)
DROPPED_COLUMNS = (
    ('companies', None, 'country', sa.String, None),
    ('companies', None, 'locale', sa.String, None),
    ('companies', None, 'currency', sa.String, None),
    ('groups', None, 'access', sa.JSON, None),
    ('staff', None, 'avatar_url', sa.String, None),
    ('sync_jobs', 'system', 'cancel_requested', sa.Boolean, sa.text('false')),
)


def _has_column(inspector, table: str, column: str, schema: str | None) -> bool:
    return any(item['name'] == column for item in inspector.get_columns(table, schema=schema))


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for table in LEGACY_TABLES:
        if inspector.has_table(table):
            op.drop_table(table)
    for table, schema, column, _type, _default in DROPPED_COLUMNS:
        if _has_column(inspector, table, column, schema):
            op.drop_column(table, column, schema=schema)


def _company_id() -> list:
    return [
        sa.Column('company_id', sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
    ]


def _index(table: str, *columns: str) -> None:
    for column in columns:
        op.create_index(f'ix_{table}_{column}', table, [column])


def downgrade() -> None:
    op.create_table(
        'service_categories',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('weight', sa.Integer(), nullable=True),
        sa.Column('api_id', sa.String(), nullable=True),
        *_company_id(),
    )
    _index('service_categories', 'company_id')

    op.create_table(
        'staff_positions',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        *_company_id(),
    )
    _index('staff_positions', 'company_id')

    op.create_table(
        'accounts',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('type', sa.Integer(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        *_company_id(),
    )
    _index('accounts', 'type', 'company_id')

    op.create_table(
        'storages',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('for_services', sa.Boolean(), nullable=True),
        sa.Column('for_sale', sa.Boolean(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        *_company_id(),
    )
    _index('storages', 'company_id')

    op.create_table(
        'good_categories',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('parent_category_id', sa.Integer(), nullable=True),
        *_company_id(),
    )
    _index('good_categories', 'parent_category_id', 'company_id')

    op.create_table(
        'goods',
        sa.Column('good_id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('cost', sa.Float(), nullable=True),
        sa.Column('actual_cost', sa.Float(), nullable=True),
        sa.Column('barcode', sa.String(), nullable=True),
        sa.Column('unit_short_title', sa.String(), nullable=True),
        sa.Column('category_id', sa.Integer(), nullable=True),
        sa.Column('last_change_date', sa.DateTime(), nullable=True),
        *_company_id(),
    )
    _index('goods', 'barcode', 'category_id', 'last_change_date', 'company_id')

    for table, schema, column, type_factory, default in DROPPED_COLUMNS:
        op.add_column(
            table,
            sa.Column(column, type_factory(), nullable=default is None, server_default=default),
            schema=schema,
        )
