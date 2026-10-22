"""Query and label helpers shared by several report builders.

They live apart from `dashboard_reports` (the catalog and dispatcher) so that builder modules can
import them without importing the module that imports the builders.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from dashboard_service import (
    COMPLETED_ATTENDANCE,
    _appointment_factual_at_condition,
    _company_scope_clause,
    business_appointment_condition,
    reporting_window_clause,
)
from models import Appointment
from report_payload import MAX_PERIOD_BUCKETS, ReportInputError


def appointment_conditions(
    start: date | None,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    *,
    attended_only: bool = False,
    allowed_company_ids: list[int] | None = None,
    factual_at: datetime,
) -> list[Any]:
    """Shared appointment filters. A `start` of None opens the window to full history.

    `factual_at` has no default: a caller that forgets it would count visits that have not happened yet.
    """
    conditions = [
        Appointment.date <= end,
        business_appointment_condition(),
        reporting_window_clause(Appointment.company_id, Appointment.date),
    ]
    if start is not None:
        conditions.append(Appointment.date >= start)
    if attended_only:
        conditions.append(Appointment.attendance == COMPLETED_ATTENDANCE)
    conditions.append(_appointment_factual_at_condition(factual_at))
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        conditions.append(scope)
    if staff_id is not None:
        conditions.append(Appointment.staff_id == staff_id)
    return conditions


def percent(part: int | float, whole: int | float, digits: int | None = None) -> float | None:
    """`part` as a percentage of `whole`, rounded to `digits` places when given; None for a zero `whole`."""
    if not whole:
        return None
    value = 100.0 * part / whole
    return value if digits is None else round(value, digits)


def period_key(value: date | datetime | None, granularity: str) -> str:
    if value is None:
        return ''
    day = value.date() if isinstance(value, datetime) else value
    if granularity == 'month':
        return day.replace(day=1).isoformat()
    if granularity == 'week':
        return (day - timedelta(days=day.weekday())).isoformat()
    return day.isoformat()


def period_starts(start: date, end: date, granularity: str) -> list[str]:
    """Every period start from the one holding `start` to the one holding `end`, so empty periods stay on the axis.

    Raises ReportInputError past `MAX_PERIOD_BUCKETS`: the axis is zero-filled, so a window of centuries by day would
    otherwise allocate millions of rows for a request that has data on a few hundred of them.
    """
    current = date.fromisoformat(period_key(start, granularity))
    keys = []
    while current <= end:
        if len(keys) >= MAX_PERIOD_BUCKETS:
            raise ReportInputError(
                f'period is too long for granularity {granularity}: choose a coarser one or a shorter period'
            )
        keys.append(current.isoformat())
        try:
            if granularity == 'month':
                current = (current + timedelta(days=32)).replace(day=1)
            else:
                current += timedelta(days=7 if granularity == 'week' else 1)
        except OverflowError:
            break
    return keys


RU_MONTHS = (
    'январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
    'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь',
)


def month_label(month: str) -> str:
    """'2026-06' as 'июнь 2026', for text a person reads."""
    year, number = month.split('-')
    return f'{RU_MONTHS[int(number) - 1]} {year}'


SHORT_RU_MONTHS = ('янв', 'фев', 'мар', 'апр', 'май', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек')


def short_month_label(month: str) -> str:
    """'2026-06' as 'июн 2026', for an axis."""
    year, number = month.split('-')
    return f'{SHORT_RU_MONTHS[int(number) - 1]} {year}'


def describe_months(months: list[str]) -> str:
    """ISO months as calendar text; a run of three or more neighbours becomes 'январь 2026 – март 2026'."""
    ordinals = sorted({int(month[:4]) * 12 + int(month[5:7]) - 1 for month in months})
    runs: list[list[int]] = []
    for ordinal in ordinals:
        if runs and ordinal == runs[-1][-1] + 1:
            runs[-1].append(ordinal)
        else:
            runs.append([ordinal])

    def label(ordinal: int) -> str:
        return month_label(f'{ordinal // 12:04d}-{ordinal % 12 + 1:02d}')

    parts = []
    for run in runs:
        if len(run) > 2:
            parts.append(f'{label(run[0])} – {label(run[-1])}')
        else:
            parts.extend(label(ordinal) for ordinal in run)
    return ', '.join(parts)


def format_day(day: date, reference_year: int) -> str:
    """'07.10', or '07.10.2025' when the year is not the one the reader is looking at."""
    return day.strftime('%d.%m') if day.year == reference_year else day.strftime('%d.%m.%Y')
