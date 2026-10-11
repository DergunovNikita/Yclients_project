"""drop the service KPI groups, the legacy `services` stub and two write-only sync objects

* `service_kpi_groups` / `service_kpi_assignments`: a Settings feature whose assignments no number ever read
  (the KPI service groups of the metrics are resolved by title, not by assignment).
* `services`: a copy of a catalog row that `_apply_service_label` wrote only to satisfy the foreign key of
  `service_labels`. That key now points at the branch-scoped `service_catalog`, like the label itself.
* `system.sync_step_runs`: ~25 rows per run that nothing read (job step results live in `sync_jobs`).
* `sync_source_states.synced_at`: written on every sync, read by nothing.

Upgrade refuses to run while a label has no catalog row (the new foreign key could not hold it, and silently
dropping a label would change the extra-service figures).

Downgrade recreates every object; it cannot restore data. Empty after downgrade: the KPI groups and their
assignments, `sync_step_runs`. `services` gets one row per service that has a label, copied from the catalog
(the stubs of unlabelled services are gone for good), and `synced_at` is the downgrade time for every row.

Revision ID: 0056_drop_unused_service_sync
Revises: 0055_auth_lifecycle_cleanup
"""

import sqlalchemy as sa
from alembic import op


revision = '0056_drop_unused_service_sync'
down_revision = '0055_auth_lifecycle_cleanup'
branch_labels = None
depends_on = None

LEGACY_LABEL_FK = 'service_labels_service_id_fkey'
CATALOG_LABEL_FK = 'service_labels_company_id_service_id_fkey'


def _assert_every_label_has_a_catalog_row() -> None:
    if op.get_context().as_sql:
        return
    orphans = op.get_bind().execute(sa.text(
        'SELECT count(*) FROM service_labels l LEFT JOIN service_catalog c '
        'ON c.company_id = l.company_id AND c.service_id = l.service_id WHERE c.service_id IS NULL'
    )).scalar_one()
    if orphans:
        raise RuntimeError(
            f'{orphans} service_labels row(s) have no (company_id, service_id) row in service_catalog; '
            'resolve them by hand before migrating, the new foreign key cannot hold them'
        )


def upgrade() -> None:
    op.drop_table('service_kpi_assignments')
    op.drop_table('service_kpi_groups')

    _assert_every_label_has_a_catalog_row()
    op.drop_constraint(LEGACY_LABEL_FK, 'service_labels', type_='foreignkey')
    op.create_foreign_key(
        CATALOG_LABEL_FK, 'service_labels', 'service_catalog',
        ['company_id', 'service_id'], ['company_id', 'service_id'], ondelete='CASCADE',
    )
    op.drop_table('services')

    op.drop_table('sync_step_runs', schema='system')
    op.drop_column('sync_source_states', 'synced_at')


def downgrade() -> None:
    op.add_column(
        'sync_source_states',
        sa.Column('synced_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
    )
    op.alter_column('sync_source_states', 'synced_at', server_default=None)

    op.create_table(
        'sync_step_runs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('run_id', sa.Integer(), nullable=False),
        sa.Column('step_name', sa.String(), nullable=False),
        sa.Column('step_key', sa.String(), nullable=True),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('elapsed_seconds', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['run_id'], ['system.sync_runs.id']),
        schema='system',
    )
    for column in ('created_at', 'run_id', 'status', 'step_key', 'step_name'):
        op.create_index(f'ix_system_sync_step_runs_{column}', 'sync_step_runs', [column], schema='system')

    op.create_table(
        'services',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('price_min', sa.Float(), nullable=True),
        sa.Column('duration', sa.Integer(), nullable=True),
        sa.Column('category_title', sa.String(), nullable=True),
        sa.Column('company_id', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id']),
    )
    op.create_index('ix_services_category_title', 'services', ['category_title'])
    op.create_index('ix_services_company_id', 'services', ['company_id'])
    # One stub per labelled service, as the removed code wrote it: the first branch that labelled it owns the copy.
    op.execute(
        'INSERT INTO services (id, title, price_min, duration, category_title, company_id) '
        'SELECT DISTINCT ON (c.service_id) c.service_id, c.title, c.price_min, c.duration, c.category_title, '
        'c.company_id FROM service_labels l JOIN service_catalog c '
        'ON c.company_id = l.company_id AND c.service_id = l.service_id ORDER BY c.service_id, c.company_id'
    )
    op.drop_constraint(CATALOG_LABEL_FK, 'service_labels', type_='foreignkey')
    op.create_foreign_key(
        LEGACY_LABEL_FK, 'service_labels', 'services', ['service_id'], ['id'], ondelete='CASCADE',
    )

    op.create_table(
        'service_kpi_groups',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('code', sa.String(), nullable=False),
        sa.Column('title', sa.String(), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.Column('portal_account_id', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('portal_account_id', 'code', name='uq_service_kpi_groups_account_code'),
        sa.ForeignKeyConstraint(
            ['portal_account_id'], ['system.portal_accounts.id'], name='fk_service_kpi_groups_portal_account_id',
        ),
    )
    for column in ('code', 'is_active', 'portal_account_id'):
        op.create_index(f'ix_service_kpi_groups_{column}', 'service_kpi_groups', [column])

    op.create_table(
        'service_kpi_assignments',
        sa.Column('company_id', sa.Integer(), nullable=False),
        sa.Column('service_id', sa.Integer(), nullable=False),
        sa.Column('group_id', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(), nullable=True, server_default='dashboard'),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
        sa.PrimaryKeyConstraint('company_id', 'service_id'),
        sa.ForeignKeyConstraint(['company_id'], ['companies.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['group_id'], ['service_kpi_groups.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(
            ['company_id', 'service_id'],
            ['service_catalog.company_id', 'service_catalog.service_id'],
            ondelete='CASCADE',
        ),
    )
    op.create_index('ix_service_kpi_assignments_group_id', 'service_kpi_assignments', ['group_id'])
    op.create_index('ix_service_kpi_assignments_service_id', 'service_kpi_assignments', ['service_id'])
