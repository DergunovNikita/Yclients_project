"""let users and credentials be deleted, and drop two write-only columns

`portal_audit_events.actor_user_id`, `sync_jobs.credential_id` and `sync_job_events.credential_id` pointed at their
parent without an ON DELETE rule, so PostgreSQL refused to delete a portal user who had ever written an audit event,
or a YClients credential that any sync job still referenced (a 500). The columns are nullable and the rows are
history, so the link now goes to NULL.

`portal_refresh_tokens.revoked_at` was never written (sessions are deleted, not revoked) and
`yclients_credentials.credential_fingerprint` was written but never read; both go, and the refresh-token
index loses its dead column.

Downgrade restores the plain foreign keys and recreates the two columns empty: a NULLed link is not restored.

Revision ID: 0055_auth_lifecycle_cleanup
Revises: 0054_drop_legacy_catalogs
"""

import sqlalchemy as sa
from alembic import op


revision = '0055_auth_lifecycle_cleanup'
down_revision = '0054_drop_legacy_catalogs'
branch_labels = None
depends_on = None

# (table, column, constraint name on a database that went through the chain, referenced table)
SET_NULL_FOREIGN_KEYS = (
    ('portal_audit_events', 'actor_user_id', 'portal_audit_events_actor_user_id_fkey', 'portal_users'),
    ('sync_jobs', 'credential_id', 'fk_sync_jobs_credential_id', 'yclients_credentials'),
    ('sync_job_events', 'credential_id', 'sync_job_events_credential_id_fkey', 'yclients_credentials'),
)
REFRESH_INDEX = 'ix_portal_refresh_tokens_user_state'
FINGERPRINT_INDEX = 'ix_yclients_credentials_credential_fingerprint'


def _recreate_foreign_keys(ondelete: str | None) -> None:
    for table, column, name, referent in SET_NULL_FOREIGN_KEYS:
        op.drop_constraint(name, table, schema='system', type_='foreignkey')
        op.create_foreign_key(
            name, table, referent, [column], ['id'],
            source_schema='system', referent_schema='system', ondelete=ondelete,
        )


def upgrade() -> None:
    _recreate_foreign_keys('SET NULL')

    op.drop_index(REFRESH_INDEX, table_name='portal_refresh_tokens', schema='system')
    op.drop_column('portal_refresh_tokens', 'revoked_at', schema='system')
    op.create_index(REFRESH_INDEX, 'portal_refresh_tokens', ['user_id', 'last_used_at'], schema='system')

    op.drop_index(FINGERPRINT_INDEX, table_name='yclients_credentials', schema='system')
    op.drop_column('yclients_credentials', 'credential_fingerprint', schema='system')


def downgrade() -> None:
    op.add_column(
        'yclients_credentials',
        sa.Column('credential_fingerprint', sa.String(length=64), nullable=True),
        schema='system',
    )
    op.create_index(FINGERPRINT_INDEX, 'yclients_credentials', ['credential_fingerprint'], schema='system')

    op.drop_index(REFRESH_INDEX, table_name='portal_refresh_tokens', schema='system')
    op.add_column('portal_refresh_tokens', sa.Column('revoked_at', sa.DateTime(), nullable=True), schema='system')
    op.create_index(
        REFRESH_INDEX, 'portal_refresh_tokens', ['user_id', 'revoked_at', 'last_used_at'], schema='system'
    )

    _recreate_foreign_keys(None)
