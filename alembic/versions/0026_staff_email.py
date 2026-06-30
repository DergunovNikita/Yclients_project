"""store staff email from YClients

Revision ID: 0026_staff_email
Revises: 0025_portal_sync_observability
"""

from alembic import op
import sqlalchemy as sa


revision = '0026_staff_email'
down_revision = '0025_portal_sync_observability'
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str, schema: str | None = None) -> bool:
    return any(item['name'] == column for item in inspector.get_columns(table, schema=schema))


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not _has_column(inspector, 'staff', 'email'):
        op.add_column('staff', sa.Column('email', sa.String(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_column(inspector, 'staff', 'email'):
        op.drop_column('staff', 'email')
