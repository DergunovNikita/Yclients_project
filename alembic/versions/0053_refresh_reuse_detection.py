"""remember the previous refresh token to detect a replayed one

Rotation replaces a session's token hash, so a client whose response was lost, and a thief
replaying a stolen token, both just saw "invalid token". The previous hash and the rotation
time let the server accept the former within a short grace window and revoke the session on
the latter. Downgrade drops the columns; nothing else reads them.

Revision ID: 0053_refresh_reuse_detection
Revises: 0052_drop_analytics_tables
"""

import sqlalchemy as sa
from alembic import op


revision = '0053_refresh_reuse_detection'
down_revision = '0052_drop_analytics_tables'
branch_labels = None
depends_on = None

INDEX_NAME = 'ix_portal_refresh_tokens_previous_hash'


def upgrade() -> None:
    op.add_column(
        'portal_refresh_tokens', sa.Column('previous_token_hash', sa.String(length=64), nullable=True), schema='system'
    )
    op.add_column('portal_refresh_tokens', sa.Column('rotated_at', sa.DateTime(), nullable=True), schema='system')
    op.create_index(INDEX_NAME, 'portal_refresh_tokens', ['previous_token_hash'], schema='system')


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name='portal_refresh_tokens', schema='system')
    op.drop_column('portal_refresh_tokens', 'rotated_at', schema='system')
    op.drop_column('portal_refresh_tokens', 'previous_token_hash', schema='system')
