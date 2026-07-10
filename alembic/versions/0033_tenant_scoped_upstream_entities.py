"""add tenant-scoped external ids for upstream detail entities

Revision ID: 0033_tenant_upstream_entities
Revises: 0032_tenant_company_identity
Create Date: 2026-07-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0033_tenant_upstream_entities'
down_revision = '0032_tenant_company_identity'
branch_labels = None
depends_on = None


ENTITY_TABLES = (
    ('staff', 'uq_staff_company_source_external'),
    ('financial_transactions', 'uq_financial_transactions_company_source_external'),
    ('goods_transactions', 'uq_goods_transactions_company_source_external'),
    ('comments', 'uq_comments_company_source_external'),
)


def _has_column(inspector, table: str, column: str, schema: str | None = None) -> bool:
    return any(item['name'] == column for item in inspector.get_columns(table, schema=schema))


def _index_names(inspector, table: str, schema: str | None = None) -> set[str]:
    return {item['name'] for item in inspector.get_indexes(table, schema=schema)}


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    for table, unique_index in ENTITY_TABLES:
        if not _has_column(inspector, table, 'external_id'):
            op.add_column(table, sa.Column('external_id', sa.Integer(), nullable=True))
        if not _has_column(inspector, table, 'source_type'):
            op.add_column(
                table,
                sa.Column('source_type', sa.String(), server_default='yclients', nullable=False),
            )
        op.execute(sa.text(f'UPDATE {table} SET external_id = id WHERE external_id IS NULL'))

        indexes = _index_names(inspector, table)
        external_index = f'ix_{table}_external_id'
        if external_index not in indexes:
            op.create_index(external_index, table, ['external_id'])
        if unique_index not in indexes:
            op.create_index(
                unique_index,
                table,
                ['company_id', 'source_type', 'external_id'],
                unique=True,
            )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())

    for table, unique_index in reversed(ENTITY_TABLES):
        indexes = _index_names(inspector, table)
        if unique_index in indexes:
            op.drop_index(unique_index, table_name=table)
        external_index = f'ix_{table}_external_id'
        if external_index in indexes:
            op.drop_index(external_index, table_name=table)
        if _has_column(inspector, table, 'source_type'):
            op.drop_column(table, 'source_type')
        if _has_column(inspector, table, 'external_id'):
            op.drop_column(table, 'external_id')
