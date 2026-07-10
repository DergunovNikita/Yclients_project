"""add tenant-scoped external ids for core YClients entities

Revision ID: 0032_tenant_company_identity
Revises: 0031_demo_flag
Create Date: 2026-07-10 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0032_tenant_company_identity'
down_revision = '0031_demo_flag'
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str, schema: str | None = None) -> bool:
    return any(item['name'] == column for item in inspector.get_columns(table, schema=schema))


def _index_names(inspector, table: str, schema: str | None = None) -> set[str]:
    return {item['name'] for item in inspector.get_indexes(table, schema=schema)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not _has_column(inspector, 'groups', 'portal_account_id'):
        op.add_column('groups', sa.Column('portal_account_id', sa.Integer(), nullable=True))
        op.create_foreign_key(
            'fk_groups_portal_account_id',
            'groups',
            'portal_accounts',
            ['portal_account_id'],
            ['id'],
            referent_schema='system',
        )
    if not _has_column(inspector, 'groups', 'external_id'):
        op.add_column('groups', sa.Column('external_id', sa.Integer(), nullable=True))

    if not _has_column(inspector, 'companies', 'portal_account_id'):
        op.add_column('companies', sa.Column('portal_account_id', sa.Integer(), nullable=True))
        op.create_foreign_key(
            'fk_companies_portal_account_id',
            'companies',
            'portal_accounts',
            ['portal_account_id'],
            ['id'],
            referent_schema='system',
        )
    if not _has_column(inspector, 'companies', 'external_id'):
        op.add_column('companies', sa.Column('external_id', sa.Integer(), nullable=True))

    if not _has_column(inspector, 'clients', 'external_id'):
        op.add_column('clients', sa.Column('external_id', sa.Integer(), nullable=True))
    if not _has_column(inspector, 'clients', 'source_type'):
        op.add_column(
            'clients',
            sa.Column('source_type', sa.String(), server_default='yclients', nullable=False),
        )

    if not _has_column(inspector, 'appointments', 'external_id'):
        op.add_column('appointments', sa.Column('external_id', sa.Integer(), nullable=True))
    if not _has_column(inspector, 'appointments', 'source_type'):
        op.add_column(
            'appointments',
            sa.Column('source_type', sa.String(), server_default='yclients', nullable=False),
        )

    op.execute(sa.text('UPDATE groups SET external_id = id WHERE external_id IS NULL'))
    op.execute(sa.text('UPDATE companies SET external_id = id WHERE external_id IS NULL'))
    op.execute(sa.text('UPDATE clients SET external_id = id WHERE external_id IS NULL'))
    op.execute(sa.text('UPDATE appointments SET external_id = id WHERE external_id IS NULL'))
    op.execute(sa.text("""
        UPDATE companies c
        SET portal_account_id = pb.portal_account_id
        FROM system.portal_branches pb
        WHERE pb.company_id = c.id
          AND c.portal_account_id IS NULL
    """))
    op.execute(sa.text("""
        UPDATE groups g
        SET portal_account_id = tenant.portal_account_id
        FROM (
            SELECT c.group_id, MIN(c.portal_account_id) AS portal_account_id
            FROM companies c
            WHERE c.portal_account_id IS NOT NULL
            GROUP BY c.group_id
        ) tenant
        WHERE tenant.group_id = g.id
          AND g.portal_account_id IS NULL
    """))

    group_indexes = _index_names(inspector, 'groups')
    if 'ix_groups_portal_account_id' not in group_indexes:
        op.create_index('ix_groups_portal_account_id', 'groups', ['portal_account_id'])
    if 'ix_groups_external_id' not in group_indexes:
        op.create_index('ix_groups_external_id', 'groups', ['external_id'])
    if 'uq_groups_account_external' not in group_indexes:
        op.create_index(
            'uq_groups_account_external',
            'groups',
            ['portal_account_id', 'external_id'],
            unique=True,
        )

    company_indexes = _index_names(inspector, 'companies')
    if 'ix_companies_portal_account_id' not in company_indexes:
        op.create_index('ix_companies_portal_account_id', 'companies', ['portal_account_id'])
    if 'ix_companies_external_id' not in company_indexes:
        op.create_index('ix_companies_external_id', 'companies', ['external_id'])
    if 'uq_companies_account_source_external' not in company_indexes:
        op.create_index(
            'uq_companies_account_source_external',
            'companies',
            ['portal_account_id', 'source_type', 'external_id'],
            unique=True,
        )

    client_indexes = _index_names(inspector, 'clients')
    if 'ix_clients_external_id' not in client_indexes:
        op.create_index('ix_clients_external_id', 'clients', ['external_id'])
    if 'uq_clients_company_source_external' not in client_indexes:
        op.create_index(
            'uq_clients_company_source_external',
            'clients',
            ['company_id', 'source_type', 'external_id'],
            unique=True,
        )

    appointment_indexes = _index_names(inspector, 'appointments')
    if 'ix_appointments_external_id' not in appointment_indexes:
        op.create_index('ix_appointments_external_id', 'appointments', ['external_id'])
    if 'uq_appointments_company_source_external' not in appointment_indexes:
        op.create_index(
            'uq_appointments_company_source_external',
            'appointments',
            ['company_id', 'source_type', 'external_id'],
            unique=True,
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    group_indexes = _index_names(inspector, 'groups')
    company_indexes = _index_names(inspector, 'companies')
    client_indexes = _index_names(inspector, 'clients')
    appointment_indexes = _index_names(inspector, 'appointments')

    if 'uq_appointments_company_source_external' in appointment_indexes:
        op.drop_index('uq_appointments_company_source_external', table_name='appointments')
    if 'ix_appointments_external_id' in appointment_indexes:
        op.drop_index('ix_appointments_external_id', table_name='appointments')
    if _has_column(inspector, 'appointments', 'source_type'):
        op.drop_column('appointments', 'source_type')
    if _has_column(inspector, 'appointments', 'external_id'):
        op.drop_column('appointments', 'external_id')

    if 'uq_clients_company_source_external' in client_indexes:
        op.drop_index('uq_clients_company_source_external', table_name='clients')
    if 'ix_clients_external_id' in client_indexes:
        op.drop_index('ix_clients_external_id', table_name='clients')
    if _has_column(inspector, 'clients', 'source_type'):
        op.drop_column('clients', 'source_type')
    if _has_column(inspector, 'clients', 'external_id'):
        op.drop_column('clients', 'external_id')

    if 'uq_companies_account_source_external' in company_indexes:
        op.drop_index('uq_companies_account_source_external', table_name='companies')
    if 'ix_companies_external_id' in company_indexes:
        op.drop_index('ix_companies_external_id', table_name='companies')
    if 'ix_companies_portal_account_id' in company_indexes:
        op.drop_index('ix_companies_portal_account_id', table_name='companies')
    if _has_column(inspector, 'companies', 'portal_account_id'):
        op.drop_constraint('fk_companies_portal_account_id', 'companies', type_='foreignkey')
    if _has_column(inspector, 'companies', 'external_id'):
        op.drop_column('companies', 'external_id')
    if _has_column(inspector, 'companies', 'portal_account_id'):
        op.drop_column('companies', 'portal_account_id')

    if 'uq_groups_account_external' in group_indexes:
        op.drop_index('uq_groups_account_external', table_name='groups')
    if 'ix_groups_external_id' in group_indexes:
        op.drop_index('ix_groups_external_id', table_name='groups')
    if 'ix_groups_portal_account_id' in group_indexes:
        op.drop_index('ix_groups_portal_account_id', table_name='groups')
    if _has_column(inspector, 'groups', 'portal_account_id'):
        op.drop_constraint('fk_groups_portal_account_id', 'groups', type_='foreignkey')
    if _has_column(inspector, 'groups', 'external_id'):
        op.drop_column('groups', 'external_id')
    if _has_column(inspector, 'groups', 'portal_account_id'):
        op.drop_column('groups', 'portal_account_id')
