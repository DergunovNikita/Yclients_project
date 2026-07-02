"""enforce platform admins without tenant accounts

Revision ID: 0028_platform_admin_no_tenant
Revises: 0027_detach_platform_admins
Create Date: 2026-07-01 00:00:00.000000
"""

from alembic import op


revision = '0028_platform_admin_no_tenant'
down_revision = '0027_detach_platform_admins'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE system.portal_users
        SET portal_account_id = NULL
        WHERE role = 'platform_admin'
        """
    )
    op.create_check_constraint(
        'ck_portal_users_platform_admin_no_tenant',
        'portal_users',
        "role <> 'platform_admin' OR portal_account_id IS NULL",
        schema='system',
    )


def downgrade() -> None:
    op.drop_constraint(
        'ck_portal_users_platform_admin_no_tenant',
        'portal_users',
        schema='system',
        type_='check',
    )
