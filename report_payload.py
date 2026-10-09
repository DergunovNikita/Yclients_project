"""Payload building blocks shared by every report builder.

Builders return the `base` payload of their `ReportRequest`, filled with cards, charts, tables and notes
produced by the helpers below, so the SPA sees one shape for every report.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

MONEY_FORMAT = 'money'
NUMBER_FORMAT = 'number'
PERCENT_FORMAT = 'percent'
DECIMAL_FORMAT = 'decimal'


@dataclass(frozen=True)
class ReportRequest:
    """Everything a report builder needs; `base` is the payload skeleton the builder fills and returns."""

    db: AsyncSession
    base: dict[str, Any]
    start: date
    end: date
    company_id: int | None
    staff_id: int | None
    granularity: str
    allowed_company_ids: list[int] | None
    factual_at: datetime
    period_preset: str | None = None
    # True for the run that supplies the comparison window: category charts then carry every row, not the top few,
    # because comparison looks the current top rows up by label and a row that was ranked lower would get no point.
    for_comparison: bool = False


def top_rows(req: ReportRequest, rows: list[Any], limit: int) -> list[Any]:
    """Rows for a category chart: the top `limit` in the report itself, all of them in the comparison run."""
    return rows if req.for_comparison else rows[:limit]


def card(label: str, value: Any, fmt: str = NUMBER_FORMAT) -> dict[str, Any]:
    return {'label': label, 'value': value, 'format': fmt}


def chart(
    chart_id: str,
    title: str,
    chart_type: str,
    labels: list[str],
    datasets: list[dict[str, Any]],
    *,
    stacked: bool = False,
    wide: bool = False,
    x_kind: str = 'category',
) -> dict[str, Any]:
    """Build a chart spec.

    `x_kind` is 'time' when labels are consecutive periods (comparison aligns such charts by index) and
    'category' when they name entities (comparison aligns them by label).
    """
    payload = {
        'id': chart_id,
        'title': title,
        'type': chart_type,
        'x_kind': x_kind,
        'labels': labels,
        'datasets': datasets,
    }
    if stacked:
        payload['stacked'] = True
    if wide:
        payload['wide'] = True
    return payload


def table(
    table_id: str,
    title: str,
    columns: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
    *,
    hide_when_empty: bool = False,
    wrap_headers: bool = False,
    row_key: str | None = None,
    x_kind: str | None = None,
) -> dict[str, Any]:
    """Build a table spec.

    `row_key` names the column that identifies a row across periods; comparison matches rows by it, so it is
    emitted only for tables that have such a column. `x_kind='time'` marks a table whose rows are consecutive
    periods: every window has its own period labels, so comparison pairs such rows by position, like a time chart.
    """
    payload = {
        'id': table_id,
        'title': title,
        'columns': [{'key': key, 'label': label, 'format': fmt} for key, label, fmt in columns],
        'rows': rows,
    }
    if row_key is not None:
        payload['row_key'] = row_key
    if x_kind is not None:
        payload['x_kind'] = x_kind
    if hide_when_empty:
        payload['hide_when_empty'] = True
    if wrap_headers:
        payload['wrap_headers'] = True
    return payload


def ranking_table(
    table_id: str,
    title: str,
    columns: list[tuple[str, str, str]],
    rows_by_metric: dict[str, list[dict[str, Any]]],
    default_metric: str,
    options: list[tuple[str, str]],
    *,
    hide_when_empty: bool = False,
) -> dict[str, Any]:
    payload = table(
        table_id, title, columns, rows_by_metric.get(default_metric, []), hide_when_empty=hide_when_empty
    )
    payload['ranking'] = {
        'default_metric': default_metric,
        'options': [{'key': key, 'label': label} for key, label in options],
        'rows_by_metric': rows_by_metric,
    }
    return payload


def without_empty_columns(
    columns: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
    optional_keys: set[str],
) -> list[tuple[str, str, str]]:
    """Drop optional columns that carry no value in any row.

    Personal-account top-ups do not exist in every tenant, and a permanently zero
    column is just noise. Kept as soon as one row has a value, so a tenant that
    does use them still sees that component of total revenue.
    """
    return [
        column
        for column in columns
        if column[0] not in optional_keys
        or any(float(row.get(column[0]) or 0) for row in rows)
    ]
