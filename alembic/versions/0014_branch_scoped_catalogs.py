"""add branch-scoped catalogs"""

from alembic import op
import sqlalchemy as sa


revision = '0014_branch_scoped_catalogs'
down_revision = '0013_service_labels_scope'
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    if not inspector.has_table(table_name, schema='public'):
        return False
    return any(
        column.get('name') == column_name
        for column in inspector.get_columns(table_name, schema='public')
    )


def _create_catalog_tables(inspector) -> None:
    if not inspector.has_table('service_category_catalog', schema='public'):
        op.create_table(
            'service_category_catalog',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('category_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('weight', sa.Integer(), nullable=True),
            sa.Column('api_id', sa.String(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'category_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='public',
        )
        op.create_index(
            'ix_service_category_catalog_category_id', 'service_category_catalog', ['category_id'], schema='public'
        )

    if not inspector.has_table('service_catalog', schema='public'):
        op.create_table(
            'service_catalog',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('service_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('price_min', sa.Float(), nullable=True),
            sa.Column('duration', sa.Integer(), nullable=True),
            sa.Column('category_id', sa.Integer(), nullable=True),
            sa.Column('category_title', sa.String(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'service_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='public',
        )
        op.create_index('ix_service_catalog_service_id', 'service_catalog', ['service_id'], schema='public')
        op.create_index(
            'ix_service_catalog_company_category', 'service_catalog', ['company_id', 'category_title'], schema='public'
        )

    if not inspector.has_table('staff_position_catalog', schema='public'):
        op.create_table(
            'staff_position_catalog',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('position_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'position_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='public',
        )
        op.create_index(
            'ix_staff_position_catalog_position_id', 'staff_position_catalog', ['position_id'], schema='public'
        )

    if not inspector.has_table('account_catalog', schema='public'):
        op.create_table(
            'account_catalog',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('account_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('type', sa.Integer(), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'account_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='public',
        )
        op.create_index('ix_account_catalog_account_id', 'account_catalog', ['account_id'], schema='public')

    if not inspector.has_table('storage_catalog', schema='public'):
        op.create_table(
            'storage_catalog',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('storage_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('for_services', sa.Boolean(), nullable=True),
            sa.Column('for_sale', sa.Boolean(), nullable=True),
            sa.Column('comment', sa.Text(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'storage_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='public',
        )
        op.create_index('ix_storage_catalog_storage_id', 'storage_catalog', ['storage_id'], schema='public')

    if not inspector.has_table('good_category_catalog', schema='public'):
        op.create_table(
            'good_category_catalog',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('category_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('parent_category_id', sa.Integer(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'category_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='public',
        )
        op.create_index(
            'ix_good_category_catalog_category_id', 'good_category_catalog', ['category_id'], schema='public'
        )

    if not inspector.has_table('good_catalog', schema='public'):
        op.create_table(
            'good_catalog',
            sa.Column('company_id', sa.Integer(), nullable=False),
            sa.Column('good_id', sa.Integer(), nullable=False),
            sa.Column('title', sa.String(), nullable=False),
            sa.Column('cost', sa.Float(), nullable=True),
            sa.Column('actual_cost', sa.Float(), nullable=True),
            sa.Column('barcode', sa.String(), nullable=True),
            sa.Column('unit_short_title', sa.String(), nullable=True),
            sa.Column('category_id', sa.Integer(), nullable=True),
            sa.Column('last_change_date', sa.DateTime(), nullable=True),
            sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text('CURRENT_TIMESTAMP')),
            sa.PrimaryKeyConstraint('company_id', 'good_id'),
            sa.ForeignKeyConstraint(['company_id'], ['public.companies.id'], ondelete='CASCADE'),
            schema='public',
        )
        op.create_index('ix_good_catalog_good_id', 'good_catalog', ['good_id'], schema='public')
        op.create_index(
            'ix_good_catalog_company_category', 'good_catalog', ['company_id', 'category_id'], schema='public'
        )


def _backfill_catalogs() -> None:
    op.execute("""
        INSERT INTO public.service_category_catalog
            (company_id, category_id, title, weight, api_id, updated_at)
        SELECT company_id, id, title, weight, api_id, CURRENT_TIMESTAMP
        FROM public.service_categories
        WHERE company_id IS NOT NULL
        ON CONFLICT (company_id, category_id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.service_catalog
            (company_id, service_id, title, price_min, duration, category_title, updated_at)
        SELECT company_id, id, title, price_min, duration, category_title, CURRENT_TIMESTAMP
        FROM public.services
        WHERE company_id IS NOT NULL
        ON CONFLICT (company_id, service_id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.service_catalog
            (company_id, service_id, title, price_min, duration, category_title, updated_at)
        SELECT
            t.company_id,
            t.service_id,
            COALESCE(MIN(NULLIF(t.service_title, '')), MAX(s.title), t.service_id::text) AS title,
            MAX(s.price_min) AS price_min,
            MAX(s.duration) AS duration,
            MAX(s.category_title) AS category_title,
            CURRENT_TIMESTAMP
        FROM public.transactions t
        LEFT JOIN public.services s ON s.id = t.service_id
        WHERE t.company_id IS NOT NULL
          AND t.service_id IS NOT NULL
        GROUP BY t.company_id, t.service_id
        ON CONFLICT (company_id, service_id) DO UPDATE SET
            title = COALESCE(NULLIF(EXCLUDED.title, ''), public.service_catalog.title),
            price_min = COALESCE(public.service_catalog.price_min, EXCLUDED.price_min),
            duration = COALESCE(public.service_catalog.duration, EXCLUDED.duration),
            category_title = COALESCE(public.service_catalog.category_title, EXCLUDED.category_title)
    """)
    op.execute("""
        INSERT INTO public.staff_position_catalog
            (company_id, position_id, title, updated_at)
        SELECT company_id, id, title, CURRENT_TIMESTAMP
        FROM public.staff_positions
        WHERE company_id IS NOT NULL
        ON CONFLICT (company_id, position_id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.account_catalog
            (company_id, account_id, title, type, comment, updated_at)
        SELECT company_id, id, title, type, comment, CURRENT_TIMESTAMP
        FROM public.accounts
        WHERE company_id IS NOT NULL
        ON CONFLICT (company_id, account_id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.storage_catalog
            (company_id, storage_id, title, for_services, for_sale, comment, updated_at)
        SELECT company_id, id, title, for_services, for_sale, comment, CURRENT_TIMESTAMP
        FROM public.storages
        WHERE company_id IS NOT NULL
        ON CONFLICT (company_id, storage_id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.good_category_catalog
            (company_id, category_id, title, parent_category_id, updated_at)
        SELECT company_id, id, title, parent_category_id, CURRENT_TIMESTAMP
        FROM public.good_categories
        WHERE company_id IS NOT NULL
        ON CONFLICT (company_id, category_id) DO NOTHING
    """)
    op.execute("""
        INSERT INTO public.good_catalog
            (company_id, good_id, title, cost, actual_cost, barcode, unit_short_title, category_id,
             last_change_date, updated_at)
        SELECT company_id, good_id, title, cost, actual_cost, barcode, unit_short_title, category_id,
               last_change_date, CURRENT_TIMESTAMP
        FROM public.goods
        WHERE company_id IS NOT NULL
        ON CONFLICT (company_id, good_id) DO NOTHING
    """)
    op.execute("""
        UPDATE public.goods_transactions gt
        SET good_title = g.title
        FROM public.good_catalog g
        WHERE gt.good_title IS NULL
          AND gt.company_id = g.company_id
          AND gt.good_id = g.good_id
    """)
    op.execute("""
        UPDATE public.goods_transactions gt
        SET storage_title = s.title
        FROM public.storage_catalog s
        WHERE gt.storage_title IS NULL
          AND gt.company_id = s.company_id
          AND gt.storage_id = s.storage_id
    """)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    _create_catalog_tables(inspector)
    if not _has_column(inspector, 'goods_transactions', 'good_title'):
        op.add_column('goods_transactions', sa.Column('good_title', sa.String(), nullable=True), schema='public')
    if not _has_column(inspector, 'goods_transactions', 'storage_title'):
        op.add_column('goods_transactions', sa.Column('storage_title', sa.String(), nullable=True), schema='public')
    _backfill_catalogs()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _has_column(inspector, 'goods_transactions', 'storage_title'):
        op.drop_column('goods_transactions', 'storage_title', schema='public')
    if _has_column(inspector, 'goods_transactions', 'good_title'):
        op.drop_column('goods_transactions', 'good_title', schema='public')
    for table_name in (
        'good_catalog',
        'good_category_catalog',
        'storage_catalog',
        'account_catalog',
        'staff_position_catalog',
        'service_catalog',
        'service_category_catalog',
    ):
        if inspector.has_table(table_name, schema='public'):
            op.drop_table(table_name, schema='public')
