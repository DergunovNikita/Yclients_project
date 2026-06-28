"""portal credentials lifecycle, scoped sync jobs, audit events

Revision ID: 0025_portal_sync_observability
Revises: 0024_auth_security_hardening
"""

from alembic import op
import sqlalchemy as sa


revision = '0025_portal_sync_observability'
down_revision = '0024_auth_security_hardening'
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str, schema: str | None = None) -> bool:
    return any(item['name'] == column for item in inspector.get_columns(table, schema=schema))


def _has_table(inspector, table: str, schema: str | None = None) -> bool:
    return inspector.has_table(table, schema=schema)


def _index_names(inspector, table: str, schema: str | None = None) -> set[str]:
    return {index['name'] for index in inspector.get_indexes(table, schema=schema)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    credential_columns = {
        'last_used_at': sa.Column('last_used_at', sa.DateTime(), nullable=True),
        'last_error_at': sa.Column('last_error_at', sa.DateTime(), nullable=True),
        'last_error': sa.Column('last_error', sa.Text(), nullable=True),
        'needs_reauth': sa.Column('needs_reauth', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        'credential_fingerprint': sa.Column('credential_fingerprint', sa.String(length=64), nullable=True),
    }
    for name, column in credential_columns.items():
        if not _has_column(inspector, 'yclients_credentials', name, schema='system'):
            op.add_column('yclients_credentials', column, schema='system')

    credential_indexes = _index_names(inspector, 'yclients_credentials', schema='system')
    if 'ix_yclients_credentials_credential_fingerprint' not in credential_indexes:
        op.create_index(
            'ix_yclients_credentials_credential_fingerprint',
            'yclients_credentials',
            ['credential_fingerprint'],
            schema='system',
        )

    if not _has_table(inspector, 'sync_jobs', schema='system'):
        op.create_table(
            'sync_jobs',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('mode', sa.String(), nullable=False),
            sa.Column('initiator', sa.String(), nullable=True),
            sa.Column('status', sa.String(), nullable=False),
            sa.Column('portal_account_id', sa.Integer(), nullable=True),
            sa.Column('credential_id', sa.Integer(), nullable=True),
            sa.Column('company_ids', sa.JSON(), nullable=True),
            sa.Column('progress_pct', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('current_stage', sa.String(), nullable=True),
            sa.Column('step_results', sa.JSON(), nullable=True),
            sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.text('false')),
            sa.Column('requested_at', sa.DateTime(), nullable=False),
            sa.Column('started_at', sa.DateTime(), nullable=True),
            sa.Column('finished_at', sa.DateTime(), nullable=True),
            sa.Column('run_id', sa.Integer(), nullable=True),
            sa.Column('error_message', sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(['portal_account_id'], ['system.portal_accounts.id']),
            sa.ForeignKeyConstraint(['credential_id'], ['system.yclients_credentials.id']),
            sa.ForeignKeyConstraint(['run_id'], ['system.sync_runs.id']),
            schema='system',
        )
        op.create_index('ix_system_sync_jobs_requested_at', 'sync_jobs', ['requested_at'], schema='system')
        op.create_index('ix_system_sync_jobs_started_at', 'sync_jobs', ['started_at'], schema='system')
        op.create_index('ix_system_sync_jobs_finished_at', 'sync_jobs', ['finished_at'], schema='system')
        op.create_index('ix_system_sync_jobs_run_id', 'sync_jobs', ['run_id'], schema='system')
    else:
        sync_job_columns = {
            'portal_account_id': sa.Column('portal_account_id', sa.Integer(), nullable=True),
            'credential_id': sa.Column('credential_id', sa.Integer(), nullable=True),
            'company_ids': sa.Column('company_ids', sa.JSON(), nullable=True),
            'progress_pct': sa.Column('progress_pct', sa.Integer(), nullable=False, server_default='0'),
            'current_stage': sa.Column('current_stage', sa.String(), nullable=True),
            'step_results': sa.Column('step_results', sa.JSON(), nullable=True),
            'cancel_requested': sa.Column('cancel_requested', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        }
        for name, column in sync_job_columns.items():
            if not _has_column(inspector, 'sync_jobs', name, schema='system'):
                op.add_column('sync_jobs', column, schema='system')

    sync_job_indexes = _index_names(inspector, 'sync_jobs', schema='system')
    if 'ix_sync_jobs_portal_account_id' not in sync_job_indexes:
        op.create_index('ix_sync_jobs_portal_account_id', 'sync_jobs', ['portal_account_id'], schema='system')
    if 'ix_sync_jobs_credential_id' not in sync_job_indexes:
        op.create_index('ix_sync_jobs_credential_id', 'sync_jobs', ['credential_id'], schema='system')

    # Foreign keys may fail in old SQLite test DBs; production Postgres gets them.
    try:
        op.create_foreign_key(
            'fk_sync_jobs_portal_account_id',
            'sync_jobs',
            'portal_accounts',
            ['portal_account_id'],
            ['id'],
            source_schema='system',
            referent_schema='system',
        )
    except Exception:
        pass
    try:
        op.create_foreign_key(
            'fk_sync_jobs_credential_id',
            'sync_jobs',
            'yclients_credentials',
            ['credential_id'],
            ['id'],
            source_schema='system',
            referent_schema='system',
        )
    except Exception:
        pass

    if not _has_table(inspector, 'sync_job_events', schema='system'):
        op.create_table(
            'sync_job_events',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('job_id', sa.Integer(), nullable=False),
            sa.Column('portal_account_id', sa.Integer(), nullable=True),
            sa.Column('credential_id', sa.Integer(), nullable=True),
            sa.Column('company_id', sa.Integer(), nullable=True),
            sa.Column('stage_key', sa.String(), nullable=True),
            sa.Column('status', sa.String(), nullable=False),
            sa.Column('elapsed_seconds', sa.Float(), nullable=True),
            sa.Column('message', sa.Text(), nullable=True),
            sa.Column('payload', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.ForeignKeyConstraint(['job_id'], ['system.sync_jobs.id'], ondelete='CASCADE'),
            sa.ForeignKeyConstraint(['portal_account_id'], ['system.portal_accounts.id']),
            sa.ForeignKeyConstraint(['credential_id'], ['system.yclients_credentials.id']),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id']),
            schema='system',
        )
        op.create_index('ix_sync_job_events_job_id', 'sync_job_events', ['job_id'], schema='system')
        op.create_index('ix_sync_job_events_portal_account_id', 'sync_job_events', ['portal_account_id'], schema='system')
        op.create_index('ix_sync_job_events_credential_id', 'sync_job_events', ['credential_id'], schema='system')
        op.create_index('ix_sync_job_events_company_id', 'sync_job_events', ['company_id'], schema='system')
        op.create_index('ix_sync_job_events_stage_key', 'sync_job_events', ['stage_key'], schema='system')
        op.create_index('ix_sync_job_events_status', 'sync_job_events', ['status'], schema='system')
        op.create_index('ix_sync_job_events_created_at', 'sync_job_events', ['created_at'], schema='system')
        op.create_index(
            'ix_sync_job_events_job_id_created_at',
            'sync_job_events',
            ['job_id', 'created_at'],
            schema='system',
        )

    if not _has_table(inspector, 'portal_audit_events', schema='system'):
        op.create_table(
            'portal_audit_events',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('actor_user_id', sa.Integer(), nullable=True),
            sa.Column('portal_account_id', sa.Integer(), nullable=True),
            sa.Column('action', sa.String(), nullable=False),
            sa.Column('target_type', sa.String(), nullable=True),
            sa.Column('target_id', sa.String(), nullable=True),
            sa.Column('metadata_json', sa.JSON(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.ForeignKeyConstraint(['actor_user_id'], ['system.portal_users.id']),
            sa.ForeignKeyConstraint(['portal_account_id'], ['system.portal_accounts.id']),
            schema='system',
        )
        op.create_index('ix_portal_audit_events_actor_user_id', 'portal_audit_events', ['actor_user_id'], schema='system')
        op.create_index('ix_portal_audit_events_portal_account_id', 'portal_audit_events', ['portal_account_id'], schema='system')
        op.create_index('ix_portal_audit_events_action', 'portal_audit_events', ['action'], schema='system')
        op.create_index('ix_portal_audit_events_target_type', 'portal_audit_events', ['target_type'], schema='system')
        op.create_index('ix_portal_audit_events_target_id', 'portal_audit_events', ['target_id'], schema='system')
        op.create_index('ix_portal_audit_events_created_at', 'portal_audit_events', ['created_at'], schema='system')
        op.create_index(
            'ix_portal_audit_events_account_created',
            'portal_audit_events',
            ['portal_account_id', 'created_at'],
            schema='system',
        )
        op.create_index(
            'ix_portal_audit_events_actor_created',
            'portal_audit_events',
            ['actor_user_id', 'created_at'],
            schema='system',
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if _has_table(inspector, 'portal_audit_events', schema='system'):
        op.drop_table('portal_audit_events', schema='system')
    if _has_table(inspector, 'sync_job_events', schema='system'):
        op.drop_table('sync_job_events', schema='system')

    for index_name in ('ix_sync_jobs_credential_id', 'ix_sync_jobs_portal_account_id'):
        try:
            op.drop_index(index_name, table_name='sync_jobs', schema='system')
        except Exception:
            pass
    for column in ('cancel_requested', 'step_results', 'current_stage', 'progress_pct', 'company_ids', 'credential_id', 'portal_account_id'):
        if _has_column(inspector, 'sync_jobs', column, schema='system'):
            op.drop_column('sync_jobs', column, schema='system')

    try:
        op.drop_index(
            'ix_yclients_credentials_credential_fingerprint',
            table_name='yclients_credentials',
            schema='system',
        )
    except Exception:
        pass
    for column in ('credential_fingerprint', 'needs_reauth', 'last_error', 'last_error_at', 'last_used_at'):
        if _has_column(inspector, 'yclients_credentials', column, schema='system'):
            op.drop_column('yclients_credentials', column, schema='system')
