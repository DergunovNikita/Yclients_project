"""bound active sessions and remove obsolete token rows

Revision ID: 0036_session_lifecycle
Revises: 0035_remove_initial_password
Create Date: 2026-08-01 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0036_session_lifecycle'
down_revision = '0035_remove_initial_password'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Revoked and expired tokens cannot authenticate. Removing them once here
    # clears the history produced by the old insert-on-every-refresh behavior.
    op.execute(
        sa.text(
            "DELETE FROM system.portal_refresh_tokens "
            "WHERE revoked_at IS NOT NULL OR expires_at <= CURRENT_TIMESTAMP"
        )
    )
    op.drop_index(
        'ix_portal_refresh_tokens_user_id',
        table_name='portal_refresh_tokens',
        schema='system',
    )
    op.create_index(
        'ix_portal_refresh_tokens_user_state',
        'portal_refresh_tokens',
        ['user_id', 'revoked_at', 'last_used_at'],
        schema='system',
    )


def downgrade() -> None:
    op.drop_index(
        'ix_portal_refresh_tokens_user_state',
        table_name='portal_refresh_tokens',
        schema='system',
    )
    op.create_index(
        'ix_portal_refresh_tokens_user_id',
        'portal_refresh_tokens',
        ['user_id'],
        schema='system',
    )
