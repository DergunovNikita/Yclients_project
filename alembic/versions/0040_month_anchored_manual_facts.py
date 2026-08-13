"""collapse manual fact metrics into calendar months

Manual review facts used to be stored with whatever range was selected when saving, and
were read back only when that range was fully inside the viewed period. A month-to-date
dashboard therefore showed a zero fact against a full-month plan, and a month plus a week
saved separately were counted twice. Facts are month-anchored from now on, so the existing
rows are collapsed into one row per (month, company, staff, metric).

Revision ID: 0040_month_anchored_facts
Revises: 0039_separate_generated_ids
Create Date: 2026-08-13 00:00:00.000000
"""

from calendar import monthrange
from datetime import date

from alembic import op
from sqlalchemy import bindparam, text


revision = '0040_month_anchored_facts'
down_revision = '0039_separate_generated_ids'
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        text(
            """
            SELECT id, period_start, period_end, company_id, staff_id, metric_code, value,
                   source, updated_at
            FROM manual_fact_metrics
            ORDER BY id
            """
        )
    ).mappings().all()

    # Values of the same month are summed: that is exactly the number the dashboard showed
    # for a full-month view, which summed every row contained in it. A row that straddles a
    # month boundary belongs to the month it starts in.
    collapsed: dict[tuple, dict] = {}
    for row in rows:
        period_start = row['period_start']
        if isinstance(period_start, str):
            period_start = date.fromisoformat(period_start)
        key = (
            row['company_id'],
            row['staff_id'],
            row['metric_code'],
            period_start.year,
            period_start.month,
        )
        item = collapsed.setdefault(
            key,
            {'value': 0.0, 'source': row['source'], 'updated_at': row['updated_at'], 'ids': []},
        )
        item['value'] += float(row['value'] or 0.0)
        item['ids'].append(row['id'])
        if row['updated_at'] is not None and (
            item['updated_at'] is None or row['updated_at'] >= item['updated_at']
        ):
            item['updated_at'] = row['updated_at']
            item['source'] = row['source']

    # The merged rows go first: widening a partial row to the month boundaries while a row
    # already anchored to that month is still present would hit the unique index.
    merged_ids = [
        row_id
        for item in collapsed.values()
        for row_id in item['ids']
        if row_id != min(item['ids'])
    ]
    if merged_ids:
        connection.execute(
            text('DELETE FROM manual_fact_metrics WHERE id IN :ids').bindparams(
                bindparam('ids', expanding=True)
            ),
            {'ids': merged_ids},
        )

    for (_, _, _, year, month), item in collapsed.items():
        connection.execute(
            text(
                """
                UPDATE manual_fact_metrics
                SET period_start = :period_start,
                    period_end = :period_end,
                    value = :value,
                    source = :source,
                    updated_at = :updated_at
                WHERE id = :id
                """
            ),
            {
                # ISO strings keep the statement free of driver-specific date adapters.
                'period_start': date(year, month, 1).isoformat(),
                'period_end': date(year, month, monthrange(year, month)[1]).isoformat(),
                'value': item['value'],
                'source': item['source'],
                'updated_at': item['updated_at'],
                'id': min(item['ids']),
            },
        )
    print(
        f'manual_fact_metrics month-anchored: {len(rows)} rows -> {len(collapsed)} '
        f'({len(merged_ids)} merged away)'
    )


def downgrade() -> None:
    """No-op: the original arbitrary ranges cannot be restored from the collapsed rows.

    Month-anchored rows stay readable for the previous code, which picked up every row
    contained in the viewed period.
    """
