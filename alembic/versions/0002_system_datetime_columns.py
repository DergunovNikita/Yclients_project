"""normalize system datetime columns"""

import re

from alembic import op
import sqlalchemy as sa

revision = '0002_system_datetime_columns'
down_revision = '0001_hardening_baseline'
branch_labels = None
depends_on = None


SYSTEM_TIMESTAMP_COLUMNS = {
    'sync_state': ['updated_at'],
    'sync_runs': ['started_at', 'finished_at'],
    'sync_step_runs': ['created_at'],
    'sync_jobs': ['requested_at', 'started_at', 'finished_at'],
}

_IDENTIFIER_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def _quote_identifier(bind, value: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f'Unsafe SQL identifier: {value!r}')
    return bind.dialect.identifier_preparer.quote(value)


def _system_table(bind, table_name: str) -> str:
    return f'{_quote_identifier(bind, "system")}.{_quote_identifier(bind, table_name)}'


def _column_type(bind, table_name: str, column_name: str) -> str | None:
    return bind.execute(sa.text("""
        SELECT data_type
        FROM information_schema.columns
        WHERE table_schema = 'system'
          AND table_name = :table_name
          AND column_name = :column_name
    """), {'table_name': table_name, 'column_name': column_name}).scalar()


def upgrade() -> None:
    bind = op.get_bind()
    for table_name, columns in SYSTEM_TIMESTAMP_COLUMNS.items():
        for column_name in columns:
            if _column_type(bind, table_name, column_name) == 'text':
                table_sql = _system_table(bind, table_name)
                column_sql = _quote_identifier(bind, column_name)
                bind.exec_driver_sql(f"""
                    ALTER TABLE {table_sql}
                    ALTER COLUMN {column_sql}
                    TYPE timestamp without time zone
                    USING NULLIF({column_sql}, '')::timestamp
                """)


def downgrade() -> None:
    bind = op.get_bind()
    for table_name, columns in SYSTEM_TIMESTAMP_COLUMNS.items():
        for column_name in columns:
            if _column_type(bind, table_name, column_name) == 'timestamp without time zone':
                table_sql = _system_table(bind, table_name)
                column_sql = _quote_identifier(bind, column_name)
                bind.exec_driver_sql(f"""
                    ALTER TABLE {table_sql}
                    ALTER COLUMN {column_sql}
                    TYPE text
                    USING CASE
                        WHEN {column_sql} IS NULL THEN NULL
                        ELSE to_char({column_sql}, 'YYYY-MM-DD\"T\"HH24:MI:SS.US')
                    END
                """)
