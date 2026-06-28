"""hardening baseline"""

from alembic import op
import sqlalchemy as sa

from models import Base

revision = '0001_hardening_baseline'
down_revision = None
branch_labels = None
depends_on = None


PUBLIC_TABLES = [
    'plan_metrics',
    'z_report_payments',
    'z_reports',
    'analytics_record_statuses',
    'analytics_record_sources',
    'analytics_daily_metrics',
    'analytics_overall',
    'staff_schedules',
    'comments',
    'goods_transactions',
    'financial_transactions',
    'transactions',
    'appointments',
    'goods',
    'good_categories',
    'storages',
    'accounts',
    'clients',
    'staff',
    'staff_positions',
    'services',
    'service_categories',
    'companies',
    'groups',
]

LEGACY_VIEWS = [
    'v_loyalty_summary',
    'v_certificates_stats',
    'v_revenue_daily',
    'v_revenue_by_staff',
    'v_popular_services',
    'v_staff_workload',
    'v_client_analytics',
    'v_revenue_monthly',
    'v_attendance_stats',
    'v_finance_daily',
    'v_finance_by_account',
    'v_finance_monthly',
    'v_goods_sales',
    'v_goods_movement',
    'v_staff_reviews',
    'v_reviews_monthly',
    'v_schedule_utilization',
    'v_companies_lookup',
    'v_services_lookup',
    'v_calendar',
    'v_appointments_enriched',
    'v_financial_transactions_enriched',
    'v_goods_transactions_enriched',
]

SYSTEM_BASE_TABLES = {
    'sync_state',
    'sync_runs',
    'sync_step_runs',
}


def _rebuild_public_schema(bind) -> None:
    for view_name in LEGACY_VIEWS:
        op.execute(sa.text(f'DROP VIEW IF EXISTS {view_name} CASCADE'))
    for table_name in PUBLIC_TABLES:
        op.execute(sa.text(f'DROP TABLE IF EXISTS public.{table_name} CASCADE'))
    Base.metadata.create_all(
        bind=bind,
        tables=[
            table for table in Base.metadata.sorted_tables
            if table.schema != 'system' and table.name in PUBLIC_TABLES
        ],
        checkfirst=False,
    )


def _ensure_system_schema(bind) -> None:
    op.execute(sa.text('CREATE SCHEMA IF NOT EXISTS system'))
    Base.metadata.create_all(
        bind=bind,
        tables=[
            table for table in Base.metadata.sorted_tables
            if table.schema == 'system' and table.name in SYSTEM_BASE_TABLES
        ],
        checkfirst=True,
    )
    has_legacy_state = bind.execute(sa.text("""
        SELECT EXISTS (
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'sync_state'
        )
    """)).scalar()
    if has_legacy_state:
        op.execute(sa.text("""
            INSERT INTO system.sync_state (key, value, updated_at)
            SELECT key, value, updated_at
            FROM public.sync_state
            ON CONFLICT (key) DO NOTHING
        """))


def upgrade() -> None:
    bind = op.get_bind()
    _ensure_system_schema(bind)
    _rebuild_public_schema(bind)


def downgrade() -> None:
    for view_name in LEGACY_VIEWS:
        op.execute(sa.text(f'DROP VIEW IF EXISTS {view_name} CASCADE'))
    for table_name in PUBLIC_TABLES:
        op.execute(sa.text(f'DROP TABLE IF EXISTS public.{table_name} CASCADE'))
    op.execute(sa.text('DROP TABLE IF EXISTS system.sync_jobs CASCADE'))
