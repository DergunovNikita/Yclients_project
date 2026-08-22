"""index schedule attribution lookups

Revision ID: 0042_staff_schedule_index
Revises: 0041_service_admin_extras
Create Date: 2026-08-22 00:00:00.000000
"""

from alembic import op


revision = '0042_staff_schedule_index'
down_revision = '0041_service_admin_extras'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        'ix_staff_schedules_company_staff_date_slots',
        'staff_schedules',
        ['company_id', 'staff_id', 'date', 'slot_from', 'slot_to'],
        schema='public',
    )


def downgrade() -> None:
    op.drop_index(
        'ix_staff_schedules_company_staff_date_slots',
        table_name='staff_schedules',
        schema='public',
    )
