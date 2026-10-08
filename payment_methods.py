"""Payment-method breakdown of revenue and the hand-entered Yandex Pay totals.

YClients lists no Yandex Pay payments, so its monthly total per branch is typed in and then
carved out of the cashless bucket. Everything else here is read from the same rows the
Overview's revenue is summed from.
"""

from __future__ import annotations

import math
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import dashboard_service
from dashboard_service import (
    ACCOUNT_TYPE_BUCKETS,
    OTHER_ACCOUNT_BUCKET,
    DateRange,
    _attach_manual_fact_authors,
    _month_slices,
    _month_value,
    _plan_month_range,
    branches_not_yet_left,
    factual_branch_date,
    fetch_branches,
    fetch_reporting_windows,
    fetch_revenue_by_account_type,
    reporting_window_clause,
)
from models import METHOD_YANDEX_PAY, ManualPaymentAmount
from portal_audit import log_portal_audit

MAX_AMOUNT = Decimal('1000000000')
CENT = Decimal('0.01')
CONFLICT_DETAIL = 'Value was changed by someone else; reload and try again'
REASON_STARTS_MID_MONTH = 'period_starts_mid_month'
REASON_BEYOND_PERIOD = 'value_beyond_period'


class PaymentRowNotOpen(ValueError):
    """The branch is not an open row of the editor for this month (unknown, or left the tenant).

    Its own type for the reason the manual-fact editors have one: the text names ids, the SPA
    prints `detail` verbatim, so the router logs it and answers with a generic sentence.
    """


class ManualPaymentConflict(Exception):
    """The stored value differs from the one the editor was showing when the user edited it."""


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


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_amount(raw: Any, field: str) -> Optional[Decimal]:
    """Validate a posted amount: ``None`` (nothing entered) or a rouble sum rounded to kopecks."""
    if raw is None or raw == '':
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f'{field} must be a number') from None
    if not math.isfinite(number):
        raise ValueError(f'{field} must be a finite number')
    if number < 0:
        raise ValueError(f'{field} cannot be negative')
    exact = Decimal(str(number))
    # Before quantizing: a value like 1e30 does not fit the context precision and would raise
    # `InvalidOperation` there instead of an ordinary validation error.
    if exact > MAX_AMOUNT:
        raise ValueError(f'{field} cannot exceed {MAX_AMOUNT}')
    # `+ 0` turns the "-0.00" that `-0.0` quantizes to into a plain zero.
    return _quantize(exact) + 0


def _money(value: Decimal | float | None) -> float | None:
    return None if value is None else round(float(value), 2)


def _iso(day: Optional[date]) -> Optional[str]:
    return None if day is None else day.isoformat()


def entry_dates(month_start: date, month_end: date, today: date) -> tuple[Optional[date], Optional[date]]:
    """(default, latest) "data through" an editor may offer for a month, or (None, None) before it starts.

    A finished month defaults to its last day. The running month defaults to yesterday: today's
    payments are still arriving, so a total typed now rarely covers them. The 1st has no
    yesterday inside the month, so it offers the 1st itself.
    """
    if month_start > today:
        return None, None
    latest = min(month_end, today)
    if month_end < today:
        return month_end, latest
    return max(month_start, today - timedelta(days=1)), latest


def format_day(day: date, reference_year: int) -> str:
    """'07.10', or '07.10.2025' when the year is not the one the reader is looking at."""
    return day.strftime('%d.%m') if day.year == reference_year else day.strftime('%d.%m.%Y')


def parse_data_through(raw: Any, month_start: date, month_end: date, today: date) -> Optional[date]:
    """A posted "data through" day, validated against its month and the branch's today."""
    if raw is None:
        return None
    if not isinstance(raw, date) or isinstance(raw, datetime):
        raise ValueError('data_through must be a date')
    if not month_start <= raw <= month_end:
        raise ValueError('data_through must fall inside the month')
    if raw > today:
        raise ValueError('data_through cannot be in the future')
    return raw


async def _editor_branches(
    db: AsyncSession,
    month_start: date,
    company_id: Optional[int],
    allowed_company_ids: Optional[list[int]],
    force_allowed: bool,
) -> list[dict[str, Any]]:
    branches = await fetch_branches(db, allowed_company_ids, force_allowed=force_allowed)
    # A branch that left the tenant is not ours to enter totals for; a month before it opened
    # still is (the value shows with `counted: false`), exactly like the manual-fact editors.
    branches = await branches_not_yet_left(db, branches, month_start)
    if company_id is not None:
        branches = [branch for branch in branches if int(branch['id']) == int(company_id)]
    return branches


async def _stored_amounts(
    db: AsyncSession,
    month_start: date,
    company_ids: list[int],
) -> dict[int, Any]:
    if not company_ids:
        return {}
    rows = (
        await db.execute(
            select(
                ManualPaymentAmount.id,
                ManualPaymentAmount.company_id,
                ManualPaymentAmount.amount,
                ManualPaymentAmount.data_through,
                ManualPaymentAmount.updated_at,
                ManualPaymentAmount.updated_by_user_id,
            ).where(
                ManualPaymentAmount.period_start == month_start,
                ManualPaymentAmount.company_id.in_(company_ids),
                ManualPaymentAmount.method_code == METHOD_YANDEX_PAY,
            )
        )
    ).all()
    return {int(row.company_id): row for row in rows}


def _same_amount(stored: Optional[Decimal | float], expected: Optional[Decimal]) -> bool:
    if stored is None or expected is None:
        return stored is None and expected is None
    return _quantize(Decimal(str(stored))) == expected


def _same_entry(
    row: Any,
    amount: Optional[Decimal],
    data_through: Optional[date],
) -> bool:
    """Does the stored row (or its absence) equal the (amount, data_through) pair?"""
    if row is None:
        return amount is None and data_through is None
    return _same_amount(row.amount, amount) and row.data_through == data_through


async def fetch_yandex_pay_editor(
    db: AsyncSession,
    month: str,
    company_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
    *,
    portal_account_id: Optional[int],
) -> dict[str, Any]:
    """Editor payload: one row per branch with the stored total and the cashless sum to check it against."""
    month_start, month_end = _plan_month_range(month)
    branches = await _editor_branches(db, month_start, company_id, allowed_company_ids, force_allowed)
    company_ids = [int(branch['id']) for branch in branches]
    stored = await _stored_amounts(db, month_start, company_ids)
    windows = await fetch_reporting_windows(db, company_ids)
    today = factual_branch_date()
    editable = month_start <= today.replace(day=1)
    default_through, max_through = entry_dates(month_start, month_end, today)
    by_type = {}
    # A month that has not started has no revenue to check against (and `9999-12` would overflow
    # the day window).
    if company_ids and editable:
        by_type = await fetch_revenue_by_account_type(
            db, DateRange(month_start, month_end), company_ids, dashboard_service.factual_now()
        )
    rows = []
    for branch in branches:
        branch_id = int(branch['id'])
        item = stored.get(branch_id)
        window = windows.get(branch_id)
        rows.append({
            'company_id': branch_id,
            'company_title': branch['title'],
            # `None`, not `0`: the editor posts back every row it renders, and a zero for an
            # empty cell would read as an edit and re-sign the row on the first save.
            'value': _money(item.amount) if item is not None else None,
            # The last day the value covers; `None` exactly when there is no value.
            'data_through': _iso(item.data_through) if item is not None else None,
            'default_data_through': _iso(default_through),
            'max_data_through': _iso(max_through),
            'cashless_yclients': _money(by_type.get(branch_id, {}).get('cashless', 0.0)),
            # Same anchor as the report cut: the stored row is judged by its period_end.
            'counted': window is None or window.covers(month_end),
            'updated_at': item.updated_at.isoformat() if item is not None and item.updated_at else None,
            'updated_by': item.updated_by_user_id if item is not None else None,
        })
    await _attach_manual_fact_authors(db, rows, portal_account_id)
    rows.sort(key=lambda row: (row['company_title'] or '', row['company_id']))
    return {
        'month': _month_value(month_start),
        'period': {'start': month_start.isoformat(), 'end': month_end.isoformat()},
        'editable': editable,
        'default_data_through': _iso(default_through),
        'max_data_through': _iso(max_through),
        'total_value': round(sum(row['value'] or 0.0 for row in rows if row['counted']), 2),
        'rows': rows,
    }


async def save_yandex_pay(
    db: AsyncSession,
    month: str,
    company_id: Optional[int],
    items: list[dict[str, Any]],
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
    *,
    actor_user_id: Optional[int],
    portal_account_id: Optional[int],
) -> None:
    """Write changed rows in one transaction, refusing the whole batch if any row went stale.

    A row's value is the pair (amount, data_through). Each item carries the pair the editor
    showed (`previous_value`, `previous_data_through`); it is the optimistic lock. Rows whose
    pair did not change are not touched, so the author column keeps meaning "who last changed
    this" even though the editor posts every row it renders. An amount without a date gets the
    month's default date; no amount clears the row, and the date is ignored.

    Raises:
        ValueError: invalid month, row, amount or date.
        ManualPaymentConflict: a row was changed by someone else since the editor loaded it.
    """
    month_start, month_end = _plan_month_range(month)
    today = factual_branch_date()
    if month_start > today.replace(day=1):
        raise ValueError('month cannot be in the future')
    default_through, _ = entry_dates(month_start, month_end, today)

    Entry = tuple[Optional[Decimal], Optional[date]]
    changes: dict[int, tuple[Entry, Entry]] = {}
    for item in items:
        try:
            item_company_id = int(item.get('company_id'))
        except (TypeError, ValueError):
            raise ValueError('company_id is required for every row') from None
        if company_id is not None and item_company_id != int(company_id):
            raise ValueError(f'company {item_company_id} does not match selected company {company_id}')
        if item_company_id in changes:
            raise ValueError(f'duplicate company_id: {item_company_id}')
        new_value = parse_amount(item.get('value'), 'value')
        previous_value = parse_amount(item.get('previous_value'), 'previous_value')
        # Without an amount there is nothing for a date to describe, so it is neither checked nor kept.
        new_through = (
            parse_data_through(item.get('data_through'), month_start, month_end, today)
            if new_value is not None
            else None
        )
        previous_through = item.get('previous_data_through')
        if not (previous_through is None or isinstance(previous_through, date)):
            raise ValueError('previous_data_through must be a date')
        changes[item_company_id] = (
            (new_value, new_through),
            (previous_value, previous_through if previous_value is not None else None),
        )
    if not changes:
        return

    branches = await _editor_branches(db, month_start, company_id, allowed_company_ids, force_allowed)
    open_ids = {int(branch['id']) for branch in branches}
    unknown = sorted(set(changes) - open_ids)
    if any(changes[branch_id][1][0] is not None for branch_id in unknown):
        # The editor showed a value for a row that is no longer open (the branch left the tenant
        # since): a stale editor to reload, not a row that was never open.
        raise ManualPaymentConflict(CONFLICT_DETAIL)
    # An untouched empty row writes nothing, so its being closed since the editor loaded must not
    # refuse the rows that were actually edited; only typing into a closed row is refused.
    unknown = [branch_id for branch_id in unknown if changes[branch_id][0][0] is not None]
    if unknown:
        raise PaymentRowNotOpen(f'company {unknown[0]} is not open for entry in {_month_value(month_start)}')

    stored = await _stored_amounts(db, month_start, sorted(changes))
    pending: dict[int, tuple[Entry, Entry]] = {}
    for branch_id, (new, previous) in changes.items():
        if new == previous:
            continue
        row = stored.get(branch_id)
        if not _same_entry(row, *previous):
            raise ManualPaymentConflict(CONFLICT_DETAIL)
        new_value, new_through = new
        if new_value is not None and new_through is None:
            new_through = default_through
        # A default that lands on what is already stored is not an edit either.
        if _same_entry(row, new_value, new_through):
            continue
        pending[branch_id] = ((new_value, new_through), previous)
    if not pending:
        return

    now = dashboard_service.factual_now()
    for branch_id, ((new_value, new_through), (previous_value, previous_through)) in sorted(pending.items()):
        row = stored.get(branch_id)
        if row is None:
            db.add(_new_row(month_start, month_end, branch_id, new_value, new_through, now, actor_user_id))
            try:
                await db.flush()
            except IntegrityError:
                # Someone inserted the same (month, branch) between our read and our write.
                await db.rollback()
                raise ManualPaymentConflict(CONFLICT_DETAIL) from None
            continue
        guard = (
            ManualPaymentAmount.id == row.id,
            ManualPaymentAmount.amount == previous_value,
            ManualPaymentAmount.data_through == previous_through,
        )
        if new_value is None:
            result = await db.execute(delete(ManualPaymentAmount).where(*guard))
        else:
            result = await db.execute(
                update(ManualPaymentAmount)
                .where(*guard)
                .values(
                    amount=new_value,
                    data_through=new_through,
                    updated_at=now,
                    updated_by_user_id=actor_user_id,
                    source='dashboard',
                )
                .execution_options(synchronize_session=False)
            )
        if result.rowcount != 1:
            await db.rollback()
            raise ManualPaymentConflict(CONFLICT_DETAIL)

    await log_portal_audit(
        db,
        actor_user_id=actor_user_id,
        portal_account_id=portal_account_id,
        action='manual_payment.updated',
        target_type='manual_payment',
        target_id=_month_value(month_start),
        metadata={
            'method_code': METHOD_YANDEX_PAY,
            'rows': [
                {
                    'company_id': branch_id,
                    'old': _money(previous_value),
                    'new': _money(new_value),
                    'old_data_through': _iso(previous_through),
                    'new_data_through': _iso(new_through),
                }
                for branch_id, ((new_value, new_through), (previous_value, previous_through)) in sorted(
                    pending.items()
                )
            ],
        },
    )
    await db.commit()


def _new_row(
    month_start: date,
    month_end: date,
    company_id: int,
    amount: Decimal,
    data_through: date,
    now: datetime,
    actor_user_id: Optional[int],
) -> ManualPaymentAmount:
    return ManualPaymentAmount(
        period_start=month_start,
        period_end=month_end,
        company_id=company_id,
        method_code=METHOD_YANDEX_PAY,
        amount=amount,
        data_through=data_through,
        source='dashboard',
        updated_at=now,
        updated_by_user_id=actor_user_id,
    )


def _month_end(month_start: date) -> date:
    return month_start.replace(day=monthrange(month_start.year, month_start.month)[1])


def _unapplied_reason(
    start: date,
    end: date,
    covered_months: list[date],
    entries: dict[date, tuple[Decimal, date]],
) -> tuple[Optional[str], Optional[date]]:
    """Why a branch's Yandex Pay cannot stand against this period's revenue, if it cannot.

    A total covers the stretch from the 1st of its month to its "data through" day, so it is
    comparable only with a period that spans that whole stretch: one that starts after the 1st of
    a month has no honest sum for that month's first days, and one that ends before the day the
    total reaches would set more Yandex Pay than revenue against each other. A month without a
    value blocks nothing — it is a gap, reported as such.
    """
    if any(start > month_start for month_start in covered_months):
        return REASON_STARTS_MID_MONTH, None
    beyond = [through for _, through in entries.values() if through > end]
    if beyond:
        return REASON_BEYOND_PERIOD, max(beyond)
    return None, None


async def fetch_payment_breakdown(
    db: AsyncSession,
    start: date,
    end: date,
    company_ids: list[int],
    factual_at: datetime,
) -> dict[str, Any]:
    """Per-branch revenue split into cash, cashless, Yandex Pay and other accounts.

    A Yandex Pay value covers its month from the 1st through its "data through" day, so it
    counts into a period only when `start <= 1st of its month` and `end >= data through`.
    The table decides this per branch, all or nothing (`_unapplied_reason`): a half-covered
    month has no honest figure, so the branch keeps its whole YClients cashless sum and the
    reason is reported. `monthly` splits the same numbers by calendar month for the trend
    chart and decides per month and value, so one cut-off end does not blank the whole line.
    """
    month_starts = [date(first.year, first.month, 1) for first, _ in _month_slices(start, end)]
    today = factual_branch_date(factual_at)

    windows = await fetch_reporting_windows(db, company_ids)
    # A branch whose window never meets the period has no facts here; listing it would only add
    # a column of zeros (and revive a branch that has already left the tenant).
    branches = [
        branch
        for branch in await fetch_branches(db, company_ids, force_allowed=True)
        if (window := windows.get(int(branch['id']))) is None or window.intersect(start, end) is not None
    ]
    scoped_ids = [int(branch['id']) for branch in branches]
    # One grouped read: the table's period buckets are the sum of the months, which partition them exactly.
    by_month = (
        await fetch_revenue_by_account_type(db, DateRange(start, end), scoped_ids, factual_at, by_month=True)
        if scoped_ids
        else {}
    )
    by_type: dict[int, dict[str, float]] = {}
    for (branch_id, _), buckets in by_month.items():
        total = by_type.setdefault(branch_id, dict.fromkeys(buckets, 0.0))
        for name, amount in buckets.items():
            total[name] += amount

    stored: dict[tuple[int, date], tuple[Decimal, date]] = {}
    if scoped_ids and month_starts:
        rows = await db.execute(
            select(
                ManualPaymentAmount.company_id,
                ManualPaymentAmount.period_start,
                ManualPaymentAmount.amount,
                ManualPaymentAmount.data_through,
            ).where(
                ManualPaymentAmount.method_code == METHOD_YANDEX_PAY,
                ManualPaymentAmount.company_id.in_(scoped_ids),
                # A range, not `IN (months)`: a multi-decade period would overflow the bind-parameter limit.
                ManualPaymentAmount.period_start.between(month_starts[0], month_starts[-1]),
                reporting_window_clause(ManualPaymentAmount.company_id, ManualPaymentAmount.period_end),
            )
        )
        stored = {
            (int(row.company_id), row.period_start): (Decimal(str(row.amount)), row.data_through)
            for row in rows.all()
        }

    def window_covers(branch_id: int, month_start: date) -> bool:
        window = windows.get(branch_id)
        return window is None or window.covers(_month_end(month_start))

    result = []
    for branch in branches:
        branch_id = int(branch['id'])
        buckets = by_type.get(branch_id, {})
        cash = buckets.get(ACCOUNT_TYPE_BUCKETS[0], 0.0)
        cashless_yclients = buckets.get(ACCOUNT_TYPE_BUCKETS[1], 0.0)
        other = buckets.get(OTHER_ACCOUNT_BUCKET, 0.0)

        covered = [month_start for month_start in month_starts if window_covers(branch_id, month_start)]
        entries = {
            month_start: stored[(branch_id, month_start)]
            for month_start in covered
            if (branch_id, month_start) in stored
        }
        reason, reason_through = _unapplied_reason(start, end, covered, entries)
        yandex_pay: Optional[float] = None
        missing: list[str] = []
        through: dict[str, str] = {}
        if reason is None:
            for month_start in covered:
                # A month that has not started cannot be entered yet, so it is not a gap.
                if month_start not in entries and month_start <= today.replace(day=1):
                    missing.append(_month_value(month_start))
            yandex_pay = float(sum(amount for amount, _ in entries.values())) if entries else None
            through = {_month_value(month_start): entry[1].isoformat() for month_start, entry in entries.items()}
        result.append({
            'company_id': branch_id,
            'title': branch['title'],
            'revenue': round(cash + cashless_yclients + other, 2),
            'cash': round(cash, 2),
            'cashless_yclients': round(cashless_yclients, 2),
            'yandex_pay': _money(yandex_pay),
            # `+ 0.0`: a float sum a hair under Yandex Pay rounds to -0.0, which the SPA prints as "-0".
            'cashless': round(cashless_yclients - (yandex_pay or 0.0), 2) + 0.0,
            'other': round(other, 2),
            'yandex_pay_missing_months': missing,
            'yandex_pay_data_through': through,
            'yandex_pay_applied': reason is None,
            'yandex_pay_unapplied_reason': reason,
            'yandex_pay_unapplied_data_through': _iso(reason_through),
        })
    monthly = []
    for month_start in month_starts:
        sums = {name: 0.0 for name in (*ACCOUNT_TYPE_BUCKETS.values(), OTHER_ACCOUNT_BUCKET)}
        for branch in branches:
            for name, amount in by_month.get((int(branch['id']), month_start), {}).items():
                sums[name] += amount
        cash, cashless_yclients, other = (sums[ACCOUNT_TYPE_BUCKETS[0]], sums[ACCOUNT_TYPE_BUCKETS[1]], sums['other'])
        # The same rule as the table's, asked of each value on its own.
        entered = []
        for branch in branches:
            entry = stored.get((int(branch['id']), month_start))
            if entry is not None and start <= month_start and end >= entry[1]:
                entered.append(entry[0])
        yandex_pay = float(sum(entered)) if entered else None
        monthly.append({
            'month': _month_value(month_start),
            'revenue': round(cash + cashless_yclients + other, 2),
            'cash': round(cash, 2),
            'cashless_yclients': round(cashless_yclients, 2),
            'yandex_pay': _money(yandex_pay),
            'cashless': round(cashless_yclients - (yandex_pay or 0.0), 2) + 0.0,
            'other': round(other, 2),
        })
    return {
        'yandex_pay_applied': all(branch['yandex_pay_applied'] for branch in result),
        'countable_months': sorted(
            {month for branch in result for month in branch['yandex_pay_data_through']}
        ),
        'branches': result,
        'monthly': monthly,
    }
