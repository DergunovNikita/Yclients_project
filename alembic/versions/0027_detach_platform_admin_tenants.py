"""detach platform admins from tenant accounts

Revision ID: 0027_detach_platform_admins
Revises: 0026_staff_email
Create Date: 2026-06-30 00:00:00.000000
"""

from alembic import op


revision = '0027_detach_platform_admins'
down_revision = '0026_staff_email'
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


def downgrade() -> None:
    pass
