"""Builders for the finance reports."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from dashboard_service import (
    COMPLETED_ATTENDANCE,
    GOODS_SALE_TYPE_ID,
    SERVICE_SOLD_ITEM_TYPE,
    DateRange,
    ReportingWindow,
    _appointment_factual_at_condition,
    _average_check_block,
    _business_financial_master_condition,
    _business_staff_id_condition,
    _coerce_date,
    _company_scope_clause,
    _direct_revenue_classifiers,
    _opz_split_fields,
    _pct_change,
    _physical_account_condition,
    _source_coverage_status,
    business_appointment_condition,
    factual_branch_date,
    fetch_opz_year_facts,
    fetch_reporting_windows,
    fetch_revenue_daily,
    fetch_year_over_year_facts,
    financial_appointment_match_condition,
    reporting_window_clause,
)
from models import AccountCatalog, Appointment, FinancialTransaction, GoodTransaction, SyncSourceState
from payment_methods import REASON_BEYOND_PERIOD, REASON_STARTS_MID_MONTH, fetch_payment_breakdown
from report_common import (
    describe_months,
    format_day,
    month_label,
    percent,
    period_key,
    period_starts,
    short_month_label,
)
from report_payload import (
    DATE_FORMAT,
    MONEY_FORMAT,
    NUMBER_FORMAT,
    PERCENT_FORMAT,
    TEXT_FORMAT,
    ReportRequest,
    card,
    chart,
    table,
    without_empty_columns,
)


# Goods movements (`goods_transactions_detail`) only fed a units-sold figure the report never showed; the goods
# revenue column comes from payments, which `financial_transactions_detail` certifies.
YOY_SOURCES = (
    'appointments_detail',
    'financial_transactions_detail',
)


async def build_financial_overview(req: ReportRequest) -> dict[str, Any]:
    """Revenue, visits and average check for the period and per sub-period.

    Cards come straight from `_average_check_block`, the block the Overview reads, so they match it to
    the kopeck. A period's average check is that period's revenue over that period's completed visits:
    the card's formula applied to a slice, which makes the card the weighted mean of the rows.
    """
    base = req.base
    avg = await _average_check_block(
        req.db,
        DateRange(start=req.start, end=req.end),
        req.company_id,
        req.staff_id,
        company_ids=req.allowed_company_ids,
        factual_at=req.factual_at,
    )
    daily = await fetch_revenue_daily(
        req.db,
        req.start,
        req.end,
        req.company_id,
        req.staff_id,
        allowed_company_ids=req.allowed_company_ids,
        include_opz=False,
        factual_at=req.factual_at,
    )
    active = {row['period']: row for row in _aggregate_daily(daily, req.granularity)}
    # Quiet periods stay on the axis as zeros: comparison pairs periods by position, and a gap would shift
    # every later period against its counterpart.
    quiet = dict.fromkeys(('revenue', 'service_revenue', 'goods_revenue', 'topup_revenue', 'appointments'), 0.0)
    periods = [
        active.get(key) or {'period': key, **quiet}
        for key in period_starts(req.start, req.end, req.granularity)
    ]
    for row in periods:
        # A day with payments but no completed visits has no average check, not a zero one.
        row['average_check'] = row['revenue'] / row['appointments'] if row['appointments'] else None

    base['missing_sources'] = sorted(avg['missing_components'] or [])
    base['notes'].append({'kind': 'formula', 'title': 'Средний чек общий', 'text': 'income / completed_appointments'})
    base['cards'] = [
        card('Выручка', avg['numerator'], MONEY_FORMAT),
        card('Услуги', avg['service_revenue'], MONEY_FORMAT),
        card('Товары', avg['goods_revenue'], MONEY_FORMAT),
        card('Пополнения', avg['topup_revenue'], MONEY_FORMAT),
        card('Завершённые визиты', avg['completed_appointments'], NUMBER_FORMAT),
        card('Средний чек', avg['total'], MONEY_FORMAT),
        card('Уникальные клиенты', avg['unique_clients'], NUMBER_FORMAT),
    ]
    labels = [row['period'] for row in periods]
    base['charts'] = [
        chart(
            'revenue_periods',
            'Выручка и завершённые визиты',
            'line',
            labels,
            [
                {'label': 'Выручка', 'data': [row['revenue'] for row in periods], 'format': MONEY_FORMAT},
                {
                    'label': 'Завершённые визиты',
                    'data': [row['appointments'] for row in periods],
                    'format': NUMBER_FORMAT,
                    'axis': 'y1',
                },
            ],
            x_kind='time',
        ),
        chart(
            'avg_check_periods',
            'Средний чек',
            'line',
            labels,
            [{'label': 'Средний чек', 'data': [row['average_check'] for row in periods], 'format': MONEY_FORMAT}],
            x_kind='time',
        ),
    ]
    base['tables'] = [
        table(
            'periods',
            'Динамика по периодам',
            [
                ('period', 'Период', TEXT_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('appointments', 'Завершённые визиты', NUMBER_FORMAT),
                ('average_check', 'Средний чек', MONEY_FORMAT),
                ('service_revenue', 'Услуги', MONEY_FORMAT),
                ('goods_revenue', 'Товары', MONEY_FORMAT),
                ('topup_revenue', 'Пополнения', MONEY_FORMAT),
            ],
            periods,
            row_key='period',
            x_kind='time',
        ),
    ]
    return base


def _aggregate_daily(rows: list[dict[str, Any]], granularity: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: {
        'revenue': 0.0,
        'service_revenue': 0.0,
        'goods_revenue': 0.0,
        'topup_revenue': 0.0,
        'appointments': 0.0,
    })
    for row in rows:
        key = period_key(date.fromisoformat(row['date']), granularity)
        grouped[key]['revenue'] += float(row.get('revenue') or 0)
        grouped[key]['service_revenue'] += float(row.get('service_revenue') or 0)
        grouped[key]['goods_revenue'] += float(row.get('goods_revenue') or 0)
        grouped[key]['topup_revenue'] += float(row.get('topup_revenue') or 0)
        grouped[key]['appointments'] += float(row.get('appointments') or 0)
    return [{'period': key, **values} for key, values in sorted(grouped.items())]


# --- year_over_year ---------------------------------------------------------------------------------


async def _year_over_year_activity_bounds(
    db: AsyncSession,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int],
    now: datetime,
) -> tuple[date, dict[str, Any]] | None:
    """Return the first YClients record and the OPZ facts.

    The first record includes every factual component used by Overview/plan-fact:
    completed visits, paid services, goods/top-up payments and goods movements.
    """
    appointment_conditions = [
        Appointment.date.is_not(None),
        Appointment.attendance == COMPLETED_ATTENDANCE,
        _appointment_factual_at_condition(now),
        business_appointment_condition(),
        reporting_window_clause(Appointment.company_id, Appointment.date),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        appointment_conditions.append(scope)
    if staff_id is not None:
        appointment_conditions.append(Appointment.staff_id == staff_id)
    appointment_bounds = (
        await db.execute(
            select(
                func.min(Appointment.date).label('activity_start'),
            ).where(*appointment_conditions)
        )
    ).one()

    payment_day = func.date(FinancialTransaction.date)
    service_conditions = [
        FinancialTransaction.date.is_not(None),
        FinancialTransaction.date <= now,
        FinancialTransaction.amount > 0,
        FinancialTransaction.sold_item_type == SERVICE_SOLD_ITEM_TYPE,
        Appointment.attendance == COMPLETED_ATTENDANCE,
        _appointment_factual_at_condition(now),
        business_appointment_condition(),
        _physical_account_condition(),
        # Both anchors, matching _service_paid_filters — the payment clause is what
        # stops this bound from opening a year the branch predates.
        reporting_window_clause(Appointment.company_id, Appointment.date),
        reporting_window_clause(FinancialTransaction.company_id, payment_day),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        service_conditions.append(scope)
    if staff_id is not None:
        service_conditions.append(Appointment.staff_id == staff_id)
    service_bounds = (
        await db.execute(
            select(
                func.min(payment_day).label('activity_start'),
            )
            .select_from(FinancialTransaction)
            .join(Appointment, financial_appointment_match_condition())
            .outerjoin(
                AccountCatalog,
                and_(
                    AccountCatalog.company_id == FinancialTransaction.company_id,
                    AccountCatalog.account_id == FinancialTransaction.account_id,
                ),
            )
            .where(*service_conditions)
        )
    ).one()

    direct_component = or_(*_direct_revenue_classifiers(staff_id))
    direct_conditions = [
        FinancialTransaction.date.is_not(None),
        FinancialTransaction.date <= now,
        FinancialTransaction.amount > 0,
        _business_financial_master_condition(now),
        _physical_account_condition(),
        direct_component,
        reporting_window_clause(FinancialTransaction.company_id, payment_day),
    ]
    scope = _company_scope_clause(FinancialTransaction.company_id, company_id, allowed_company_ids)
    if scope is not None:
        direct_conditions.append(scope)
    direct_bounds = (
        await db.execute(
            select(
                func.min(payment_day).label('activity_start'),
            )
            .select_from(FinancialTransaction)
            .outerjoin(
                AccountCatalog,
                and_(
                    AccountCatalog.company_id == FinancialTransaction.company_id,
                    AccountCatalog.account_id == FinancialTransaction.account_id,
                ),
            )
            .where(*direct_conditions)
        )
    ).one()

    goods_day = func.date(GoodTransaction.date)
    goods_conditions = [
        GoodTransaction.date.is_not(None),
        GoodTransaction.date <= now,
        GoodTransaction.type_id == GOODS_SALE_TYPE_ID,
        _business_staff_id_condition(GoodTransaction.master_id),
        reporting_window_clause(GoodTransaction.company_id, goods_day),
    ]
    scope = _company_scope_clause(GoodTransaction.company_id, company_id, allowed_company_ids)
    if scope is not None:
        goods_conditions.append(scope)
    if staff_id is not None:
        goods_conditions.append(GoodTransaction.master_id == staff_id)
    goods_bounds = (
        await db.execute(
            select(
                func.min(goods_day).label('activity_start'),
            ).where(*goods_conditions)
        )
    ).one()

    component_bounds = (
        appointment_bounds,
        service_bounds,
        direct_bounds,
        goods_bounds,
    )
    starts = [
        _coerce_date(bounds.activity_start)
        for bounds in component_bounds
        if bounds.activity_start is not None
    ]
    if not starts:
        return None

    activity_start = min(starts)
    opz_facts = await fetch_opz_year_facts(
        db,
        activity_start,
        factual_branch_date(now),
        company_id,
        staff_id,
        allowed_company_ids,
        factual_at=now,
    )
    return activity_start, opz_facts


async def _year_over_year_source_states(
    db: AsyncSession,
    scope_company_ids: list[int],
) -> dict[tuple[int, str], SyncSourceState]:
    if not scope_company_ids:
        return {}

    states = (
        await db.execute(
            select(SyncSourceState).where(
                SyncSourceState.company_id.in_(scope_company_ids),
                SyncSourceState.source.in_(YOY_SOURCES),
            )
        )
    ).scalars().all()
    return {(int(state.company_id), state.source): state for state in states}


def _year_over_year_missing_sources(
    state_by_key: dict[tuple[int, str], SyncSourceState],
    scope_company_ids: list[int],
    period_start: date,
    period_end: date,
    required_sources: tuple[str, ...] = YOY_SOURCES,
    appointment_dependencies: dict[int, tuple[date, date]] | None = None,
    reporting_windows: dict[int, ReportingWindow] | None = None,
) -> list[str]:
    missing = set()
    for item_company_id in scope_company_ids:
        # A branch contributes no facts outside its reporting window, so demanding sync
        # coverage there would blank a year the branch simply predates or has left — and a
        # branch that did not exist yet has no coverage to demand at all.
        # Unlike _source_coverage_status, which answers for a user-chosen range and calls
        # a fully unreportable period uncertifiable, the years here are derived from already
        # trimmed facts — a year outside every branch's window never reaches this function.
        window = (reporting_windows or {}).get(item_company_id)
        reportable = (
            window.intersect(period_start, period_end)
            if window is not None
            else (period_start, period_end)
        )
        if reportable is None:
            continue
        branch_period_start, branch_period_end = reportable
        for source in required_sources:
            state = state_by_key.get((item_company_id, source))
            required_start = branch_period_start
            required_end = branch_period_end
            if source == 'appointments_detail' and appointment_dependencies:
                dependency = appointment_dependencies.get(item_company_id)
                if dependency is not None:
                    required_start = min(required_start, dependency[0])
                    required_end = max(required_end, dependency[1])
            if (
                state is None
                or state.period_start > required_start
                or state.period_end < required_end
            ):
                missing.add(source)
    return sorted(missing)


# The keys `_opz_split_fields` fills, so the masks below cannot miss one it gains.
OPZ_SPLIT_METRICS = tuple(_opz_split_fields(0.0, 0.0, 0, 0))


def _mask_unknown_year_metrics(
    row: dict[str, Any],
    missing_sources: list[str],
) -> None:
    """Do not expose partial aggregates as factual zeroes or complete sums."""
    missing = set(missing_sources)
    appointments_known = 'appointments_detail' not in missing
    financials_known = 'financial_transactions_detail' not in missing

    if not appointments_known:
        for metric in (
            'appointments',
            'extra_service_count',
            'unique_clients',
            'visits_per_client',
            'opz_qty',
            'opz_pct',
            *OPZ_SPLIT_METRICS,
        ):
            row[metric] = None

    if not (appointments_known and financials_known):
        for metric in (
            'revenue',
            'service_revenue',
            'goods_revenue',
            'topup_revenue',
            'avg_check',
        ):
            row[metric] = None


def _year_periods(activity_start: date, report_end: date) -> list[dict[str, Any]]:
    periods = []
    for year in range(activity_start.year, report_end.year + 1):
        calendar_start = date(year, 1, 1)
        calendar_end = date(year, 12, 31)
        period_start = max(calendar_start, activity_start)
        period_end = min(calendar_end, report_end)
        is_partial_year = (
            period_start != calendar_start
            or period_end != calendar_end
            or year == report_end.year
        )
        periods.append({
            'year': year,
            'start': period_start,
            'end': period_end,
            'is_partial_year': is_partial_year,
        })
    return periods


def _year_row(
    year: int,
    period: dict[str, Any],
    annual: dict[str, Any],
    opz_qty: float,
    opz_split: tuple[float, float],
) -> dict[str, Any]:
    appointments = int(annual.get('appointments') or 0)
    revenue = float(annual.get('revenue') or 0)
    unique_clients = int(annual.get('unique_clients') or 0)
    is_partial_year = bool(period['is_partial_year'])
    return {
        'year': year,
        'period_start': period['start'].isoformat(),
        'period_end': period['end'].isoformat(),
        'revenue': revenue,
        'service_revenue': float(annual.get('service_revenue') or 0),
        'goods_revenue': float(annual.get('goods_revenue') or 0),
        'topup_revenue': float(annual.get('topup_revenue') or 0),
        'appointments': appointments,
        'extra_service_count': float(annual.get('extra_service_count') or 0),
        'unique_clients': unique_clients,
        # No completed visits means no average check, visits per client or OPZ rate; a zero would draw a real bar.
        'avg_check': revenue / appointments if appointments else None,
        'visits_per_client': (appointments / unique_clients if unique_clients else 0.0) if appointments else None,
        'opz_qty': opz_qty,
        'opz_pct': 100.0 * opz_qty / appointments if appointments else None,
        **_opz_split_fields(*opz_split, appointments, int(annual.get('first_visits') or 0)),
        'source_status': 'ready',
        'missing_components': [],
        'is_partial_year': is_partial_year,
        'period_status': 'Неполный' if is_partial_year else 'Полный',
    }


def _monthly_yoy_rows(
    year: int,
    monthly_facts: dict[int, dict[str, Any]],
    period_start: date,
    period_end: date,
    state_by_key: dict[tuple[int, str], SyncSourceState],
    scope_company_ids: list[int],
    appointment_dependencies: dict[int, dict[int, tuple[date, date]]] | None = None,
    reporting_windows: dict[int, ReportingWindow] | None = None,
    opz_counts: dict[int, float] | None = None,
    opz_dependencies: dict[int, dict[int, tuple[date, date]]] | None = None,
    opz_split_counts: tuple[dict[int, float], dict[int, float]] = ({}, {}),
) -> list[dict[str, Any]]:
    metrics = ('revenue', 'service_revenue', 'goods_revenue', 'topup_revenue', 'appointments', 'first_visits')
    monthly = {
        month: {metric: float(monthly_facts.get(month, {}).get(metric) or 0) for metric in metrics}
        for month in range(1, 13)
    }

    rows = []
    for month, values in monthly.items():
        month_start = date(year, month, 1)
        month_end = date(year + (month == 12), 1 if month == 12 else month + 1, 1) - timedelta(days=1)
        in_activity_period = month_end >= period_start and month_start <= period_end
        slice_start = max(month_start, period_start)
        slice_end = min(month_end, period_end)
        missing_sources = (
            _year_over_year_missing_sources(
                state_by_key,
                scope_company_ids,
                slice_start,
                slice_end,
                YOY_SOURCES,
                (appointment_dependencies or {}).get(month),
                reporting_windows,
            )
            if in_activity_period
            else []
        )
        # Mirrors the annual row: an OPZ-only coverage gap hides OPZ but not revenue or visits.
        opz_missing = (
            _year_over_year_missing_sources(
                state_by_key,
                scope_company_ids,
                slice_start,
                slice_end,
                required_sources=('appointments_detail',),
                appointment_dependencies=(opz_dependencies or {}).get(month),
                reporting_windows=reporting_windows,
            )
            if in_activity_period
            else []
        )
        appointments_known = 'appointments_detail' not in missing_sources
        financials_known = 'financial_transactions_detail' not in missing_sources
        revenue_known = appointments_known and financials_known
        opz_known = in_activity_period and appointments_known and 'appointments_detail' not in opz_missing
        opz_qty = float((opz_counts or {}).get(month, 0.0)) if opz_known else None
        opz_split = (
            _opz_split_fields(
                *(float(counts.get(month, 0.0)) for counts in opz_split_counts),
                values['appointments'],
                values['first_visits'],
            )
            if opz_known
            else dict.fromkeys(OPZ_SPLIT_METRICS)
        )
        # Same formula as the annual row: revenue over completed visits. A month with
        # no visits has no average check rather than a zero one.
        avg_check = (
            values['revenue'] / values['appointments']
            if in_activity_period and revenue_known and values['appointments']
            else None
        )
        rows.append({
            'year': year,
            'month': month,
            'month_label': f'{month:02d}',
            'revenue': values['revenue'] if in_activity_period and revenue_known else None,
            'service_revenue': values['service_revenue'] if in_activity_period and revenue_known else None,
            'goods_revenue': values['goods_revenue'] if in_activity_period and revenue_known else None,
            'topup_revenue': values['topup_revenue'] if in_activity_period and revenue_known else None,
            'appointments': values['appointments'] if in_activity_period and appointments_known else None,
            'avg_check': avg_check,
            'opz_qty': opz_qty,
            # Like avg_check: a month without visits has no rate rather than a zero one.
            'opz_pct': 100.0 * opz_qty / values['appointments'] if opz_known and values['appointments'] else None,
            **opz_split,
            'in_activity_period': in_activity_period,
            'source_status': (
                'ready' if in_activity_period and not missing_sources and not opz_missing else 'partial'
            ),
            'missing_components': sorted({*missing_sources, *opz_missing}),
        })
    return rows


def _period_shape(row: dict[str, Any]) -> tuple[int, int, int, int]:
    period_start = date.fromisoformat(row['period_start'])
    period_end = date.fromisoformat(row['period_end'])
    return period_start.month, period_start.day, period_end.month, period_end.day


def _with_year_changes(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    previous: dict[str, Any] | None = None
    out = []
    for row in sorted(rows, key=lambda item: item['year']):
        enriched = dict(row)
        comparable_period = (
            previous is not None
            and _period_shape(row) == _period_shape(previous)
            and not row.get('is_partial_year')
            and not previous.get('is_partial_year')
        )
        for metric in (
            'revenue',
            'appointments',
            'avg_check',
            'unique_clients',
            'service_revenue',
            'goods_revenue',
            'topup_revenue',
            'opz_qty',
            'opz_pct',
            'opz_first_visit_pct',
            'opz_repeat_visit_pct',
        ):
            metric_comparable = (
                comparable_period
                and row.get(metric) is not None
                and previous.get(metric) is not None
            )
            enriched[f'{metric}_change_pct'] = (
                _pct_change(float(row[metric]), float(previous[metric]))
                if metric_comparable
                else None
            )
        if (
            comparable_period
            and row.get('source_status') == 'ready'
            and previous.get('source_status') == 'ready'
        ):
            enriched['comparison_status'] = 'comparable'
        elif previous is None:
            enriched['comparison_status'] = 'no_previous'
        elif row.get('source_status') != 'ready' or previous.get('source_status') != 'ready':
            enriched['comparison_status'] = 'incomplete_source'
        else:
            enriched['comparison_status'] = 'different_period'
        out.append(enriched)
        previous = row
    return out


# (metric, series label, format, (by-year chart id and title, month-by-month chart id and title))
YEAR_OVER_YEAR_CHART_METRICS = (
    (
        'revenue', 'Выручка', MONEY_FORMAT,
        ('year_revenue', 'Выручка по годам', 'monthly_revenue_yoy', 'Помесячная выручка год к году'),
    ),
    (
        'appointments', 'Визиты', NUMBER_FORMAT,
        ('year_appointments', 'Визиты по годам', 'monthly_appointments_yoy', 'Помесячные визиты год к году'),
    ),
    (
        'avg_check', 'Средний чек', MONEY_FORMAT,
        ('year_avg_check', 'Средний чек по годам', 'monthly_avg_check_yoy', 'Помесячный средний чек год к году'),
    ),
    (
        'opz_qty', 'ОПЗ', NUMBER_FORMAT,
        ('year_opz', 'ОПЗ по годам', 'monthly_opz_yoy', 'Помесячные ОПЗ год к году'),
    ),
    (
        'opz_pct', 'ОПЗ %', PERCENT_FORMAT,
        ('year_opz_pct', 'ОПЗ % по годам', 'monthly_opz_pct_yoy', 'Помесячный ОПЗ % год к году'),
    ),
)


def _year_and_month_charts(
    year_rows: list[dict[str, Any]],
    monthly_by_year: dict[int, list[dict[str, Any]]],
    metric: str,
    label: str,
    fmt: str,
    ids: tuple[str, str, str, str],
    skip_empty_years: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """A metric by year (bars) next to its month-by-month year-over-year lines.

    With `skip_empty_years` a year with no value in any month is left off the lines, and the
    remaining ones keep the colour they would have had, so they match the neighbouring charts.
    """
    year_id, year_title, month_id, month_title = ids
    series = []
    for position, year in enumerate(sorted(monthly_by_year)):
        values = [row[metric] for row in monthly_by_year[year]]
        if skip_empty_years and all(value is None for value in values):
            continue
        item = {'label': str(year), 'data': values, 'format': fmt}
        if skip_empty_years:
            item['colorIndex'] = position
        series.append(item)
    return (
        chart(
            year_id,
            year_title,
            'bar',
            [str(row['year']) for row in year_rows],
            [{'label': label, 'data': [row[metric] for row in year_rows], 'format': fmt}],
        ),
        chart(month_id, month_title, 'line', [f'{month:02d}' for month in range(1, 13)], series),
    )


async def build_year_over_year(req: ReportRequest) -> dict[str, Any]:
    base = req.base
    db = req.db
    report_now = req.factual_at
    scope_company_ids = req.allowed_company_ids
    activity_bounds = await _year_over_year_activity_bounds(
        db, req.company_id, req.staff_id, scope_company_ids, report_now
    )
    if activity_bounds is None:
        base['source_status'] = 'partial'
        base['notes'].append({
            'kind': 'missing',
            'title': 'Нет фактических записей YClients',
            'text': 'Для выбранного среза пока нельзя определить границы истории.',
        })
        return base

    activity_start, opz_facts = activity_bounds
    opz_by_year = opz_facts['counts']
    opz_split_by_year = (opz_facts['monthly_first_visit_counts'], opz_facts['monthly_repeat_visit_counts'])
    opz_dependencies_by_year = opz_facts['appointment_dependencies']
    report_end = factual_branch_date(report_now)
    periods = _year_periods(activity_start, report_end)
    state_by_key = await _year_over_year_source_states(db, scope_company_ids)
    reporting_windows = await fetch_reporting_windows(db, scope_company_ids)
    fact_rows = await fetch_year_over_year_facts(
        db,
        activity_start,
        report_end,
        req.company_id,
        req.staff_id,
        scope_company_ids,
        factual_at=report_now,
    )
    year_rows = []
    monthly_by_year: dict[int, list[dict[str, Any]]] = {}

    for period in periods:
        period_start = period['start']
        period_end = period['end']
        year = int(period['year'])
        year_row = _year_row(
            year,
            period,
            fact_rows['annual'].get(year, {}),
            float(opz_by_year.get(year, 0.0)),
            tuple(sum(counts.get(year, {}).values()) for counts in opz_split_by_year),
        )
        technical_missing = _year_over_year_missing_sources(
            state_by_key,
            scope_company_ids,
            period_start,
            period_end,
            appointment_dependencies=fact_rows['appointment_dependencies']['annual'].get(year),
            reporting_windows=reporting_windows,
        )
        opz_missing = _year_over_year_missing_sources(
            state_by_key,
            scope_company_ids,
            period_start,
            period_end,
            required_sources=('appointments_detail',),
            appointment_dependencies=opz_dependencies_by_year.get(year),
            reporting_windows=reporting_windows,
        )
        year_row['missing_components'] = sorted({
            *year_row['missing_components'],
            *technical_missing,
            *opz_missing,
        })
        if year_row['missing_components']:
            year_row['source_status'] = 'partial'
        _mask_unknown_year_metrics(year_row, technical_missing)
        if 'appointments_detail' in opz_missing:
            for metric in ('opz_qty', 'opz_pct', *OPZ_SPLIT_METRICS):
                year_row[metric] = None
        year_rows.append(year_row)
        monthly_by_year[year] = _monthly_yoy_rows(
            year,
            fact_rows['monthly'].get(year, {}),
            period_start,
            period_end,
            state_by_key,
            scope_company_ids,
            fact_rows['appointment_dependencies']['monthly'].get(year),
            reporting_windows,
            opz_facts['monthly_counts'].get(year),
            opz_facts['monthly_appointment_dependencies'].get(year),
            tuple(counts.get(year, {}) for counts in opz_split_by_year),
        )

    year_rows = _with_year_changes(year_rows)
    monthly_rows = [
        row
        for year in sorted(monthly_by_year)
        for row in monthly_by_year[year]
    ]

    base['notes'].append({
        'kind': 'formula',
        'title': 'Единая формула с Обзором и План/факт',
        'text': (
            'Выручка, завершенные визиты и средний чек рассчитаны по тем же '
            'оплаченным компонентам и бизнес-фильтрам. Первый и последний годы '
            'показываются за их фактический период. ОПЗ считается по формуле Обзора: '
            'одно событие на визит, после которого клиент записался, поэтому месяцы '
            'складываются в год. ОПЗ после первого и после повторного визита считаются '
            'от своих визитов (первых в филиале и остальных) и ручную добавку не включают. '
            'Ручные добавки ОПЗ входят в итог филиала и сети и не учитываются при фильтре '
            'по сотруднику.'
        ),
    })
    partial_rows = [row for row in year_rows if row.get('source_status') != 'ready']
    if partial_rows:
        base['source_status'] = 'partial'
        base['missing_sources'] = sorted({
            component
            for row in partial_rows
            for component in row.get('missing_components', [])
        })
        base['notes'].append({
            'kind': 'warning',
            'title': 'Есть технически неполные компоненты',
            'text': 'Годы сохранены в отчете; доступные факты показаны вместе со статусом покрытия.',
        })
    base['period'] = {
        'start': activity_start.isoformat(),
        'end': report_end.isoformat(),
        'granularity': 'month',
    }
    # No cards: the latest year is almost always partial, so its change cards only showed a dash, and
    # its values are already the last bar of every by-year chart. The report has no period to pick.
    # The grid is two columns wide, so each metric is a row: by year on the left, its
    # month-by-month year-over-year comparison on the right.
    base['charts'] = [
        year_chart
        for metric, label, fmt, ids in YEAR_OVER_YEAR_CHART_METRICS
        for year_chart in _year_and_month_charts(year_rows, monthly_by_year, metric, label, fmt, ids)
    ]
    # A part with no visits in any year (a history of first visits only) has no rate to plot, and an
    # all-empty chart reads as a broken one, so its pair of charts and empty years are left out.
    for key, title in (('first', 'после первого визита'), ('repeat', 'после повторного визита')):
        metric = f'opz_{key}_visit_pct'
        if any(row[metric] is not None for row in year_rows):
            base['charts'].extend(_year_and_month_charts(
                year_rows,
                monthly_by_year,
                metric,
                'ОПЗ %',
                PERCENT_FORMAT,
                (
                    f'year_opz_{key}_visit_pct',
                    f'ОПЗ % {title} по годам',
                    f'monthly_opz_{key}_visit_pct_yoy',
                    f'Помесячный ОПЗ % {title} год к году',
                ),
                skip_empty_years=True,
            ))
    base['tables'] = [
        table(
            'years',
            'Годовые агрегаты',
            without_empty_columns([
                ('year', 'Год', NUMBER_FORMAT),
                ('period_start', 'С', DATE_FORMAT),
                ('period_end', 'По', DATE_FORMAT),
                ('period_status', 'Период', TEXT_FORMAT),
                ('source_status', 'Покрытие', TEXT_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('revenue_change_pct', 'Выручка YoY', PERCENT_FORMAT),
                ('appointments', 'Визиты', NUMBER_FORMAT),
                ('appointments_change_pct', 'Визиты YoY', PERCENT_FORMAT),
                ('avg_check', 'Средний чек', MONEY_FORMAT),
                ('avg_check_change_pct', 'Средний чек YoY', PERCENT_FORMAT),
                ('unique_clients', 'Клиенты', NUMBER_FORMAT),
                ('unique_clients_change_pct', 'Клиенты YoY', PERCENT_FORMAT),
                ('service_revenue', 'Услуги', MONEY_FORMAT),
                ('goods_revenue', 'Товары', MONEY_FORMAT),
                ('topup_revenue', 'Пополнения', MONEY_FORMAT),
                ('extra_service_count', 'Доп. услуги', NUMBER_FORMAT),
                ('opz_qty', 'ОПЗ', NUMBER_FORMAT),
                ('opz_qty_change_pct', 'ОПЗ YoY', PERCENT_FORMAT),
                ('opz_pct', 'ОПЗ %', PERCENT_FORMAT),
                ('opz_pct_change_pct', 'ОПЗ % YoY', PERCENT_FORMAT),
                ('opz_first_visit_qty', 'ОПЗ после первого визита', NUMBER_FORMAT),
                ('opz_first_visit_pct', 'ОПЗ % после первого визита', PERCENT_FORMAT),
                ('opz_first_visit_pct_change_pct', 'ОПЗ % после первого визита YoY', PERCENT_FORMAT),
                ('opz_repeat_visit_qty', 'ОПЗ после повторного визита', NUMBER_FORMAT),
                ('opz_repeat_visit_pct', 'ОПЗ % после повторного визита', PERCENT_FORMAT),
                ('opz_repeat_visit_pct_change_pct', 'ОПЗ % после повторного визита YoY', PERCENT_FORMAT),
            ], year_rows, {'topup_revenue'}),
            year_rows,
            row_key='year',
        ),
        table(
            'months',
            'Помесячные агрегаты',
            without_empty_columns([
                ('year', 'Год', NUMBER_FORMAT),
                ('month_label', 'Месяц', TEXT_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('appointments', 'Визиты', NUMBER_FORMAT),
                ('avg_check', 'Средний чек', MONEY_FORMAT),
                ('opz_qty', 'ОПЗ', NUMBER_FORMAT),
                ('opz_pct', 'ОПЗ %', PERCENT_FORMAT),
                ('opz_first_visit_qty', 'ОПЗ после первого визита', NUMBER_FORMAT),
                ('opz_first_visit_pct', 'ОПЗ % после первого визита', PERCENT_FORMAT),
                ('opz_repeat_visit_qty', 'ОПЗ после повторного визита', NUMBER_FORMAT),
                ('opz_repeat_visit_pct', 'ОПЗ % после повторного визита', PERCENT_FORMAT),
                ('service_revenue', 'Услуги', MONEY_FORMAT),
                ('goods_revenue', 'Товары', MONEY_FORMAT),
                ('topup_revenue', 'Пополнения', MONEY_FORMAT),
            ], monthly_rows, {'topup_revenue'}),
            monthly_rows,
        ),
    ]
    return base


# --- payment_methods --------------------------------------------------------------------------------


async def build_payment_methods(req: ReportRequest) -> dict[str, Any]:
    base = req.base
    breakdown = await fetch_payment_breakdown(req.db, req.start, req.end, req.allowed_company_ids, req.factual_at)
    branches = breakdown['branches']
    multi = len(branches) > 1
    # Revenue includes top-ups, so the coverage verdict is the Overview's, not a new one.
    source_status, missing_components = await _source_coverage_status(
        req.db, req.start, req.end, None, None, company_ids_override=req.allowed_company_ids
    )
    base['missing_sources'] = sorted(missing_components)
    if source_status == 'partial':
        base['source_status'] = 'partial'
    if not branches:
        # Every branch of the selection is outside its reporting window for this period: there
        # is no column to draw, and a table of one "Показатель" header reads as a broken report.
        base['notes'].append({
            'kind': 'warning',
            'title': 'Нет данных',
            'text': 'За выбранный период у филиала нет данных: он не входил в отчётность за эти даты.',
        })
        return base

    def total_of(key: str) -> float | None:
        values = [branch[key] for branch in branches if branch[key] is not None]
        # `+ 0.0` keeps a cancelled-out sum from rounding to -0.0, which the SPA prints as "-0".
        return round(sum(values), 2) + 0.0 if values else None

    total = {
        'revenue': total_of('revenue') or 0.0,
        'cash': total_of('cash') or 0.0,
        'cashless': total_of('cashless') or 0.0,
        'yandex_pay': total_of('yandex_pay'),
        'other': total_of('other') or 0.0,
    }
    forms = [('cash', 'Наличные'), ('cashless', 'Безналичные'), ('yandex_pay', 'Яндекс Пэй')]
    if any(branch['other'] for branch in branches):
        forms.append(('other', 'Прочие кассы'))

    def share(value: float | None, revenue: float) -> float | None:
        return percent(value, revenue, 2) if value is not None else None

    def positive_or_none(value: float | None) -> float | None:
        return value if value is not None and value >= 0 else None

    columns = ([('total', 'Все филиалы')] if multi else []) + [
        (f"company_{branch['company_id']}", branch['title']) for branch in branches
    ]
    column_values = ([total] if multi else []) + branches

    def metric_row(label: str, key: str, as_share: bool) -> dict[str, Any]:
        row: dict[str, Any] = {'metric': label}
        for (column_key, _), values in zip(columns, column_values):
            row[column_key] = share(values[key], values['revenue']) if as_share else values[key]
        return row

    base['cards'] = [
        card('Выручка', total['revenue'], MONEY_FORMAT),
        card('Доля наличных', share(total['cash'], total['revenue']), PERCENT_FORMAT),
        card('Доля безналичных', share(total['cashless'], total['revenue']), PERCENT_FORMAT),
        card('Доля Яндекс Пэй', share(total['yandex_pay'], total['revenue']), PERCENT_FORMAT),
    ]
    # The network column is last in the chart, first in the tables: bars read branch by branch.
    chart_rows = branches + ([total] if multi else [])
    chart_labels = [row['title'] for row in branches] + (['Все филиалы'] if multi else [])
    base['charts'] = [
        chart(
            'payment_shares_by_branch',
            'Доли форм оплаты по филиалам',
            'bar',
            chart_labels,
            [
                {
                    'label': label,
                    'data': [share(values[key], values['revenue']) for values in chart_rows],
                    'format': PERCENT_FORMAT,
                }
                for key, label in forms
            ],
            stacked=True,
        ),
        chart(
            'payment_shares_network',
            'Структура выручки',
            'doughnut',
            [label for _, label in forms],
            [{
                'label': 'Доля',
                # An arc is drawn from |value|, so a negative share (Yandex Pay above the cashless
                # sum, flagged in the notes) would show up as a positive slice: left out here, and
                # still negative in the tables.
                'data': [positive_or_none(share(total[key], total['revenue'])) for key, _ in forms],
                'format': PERCENT_FORMAT,
            }],
        ),
    ]
    if not total['revenue']:
        # No revenue, no shares: both charts would be empty frames.
        base['charts'] = []
    # A trend needs two points; the last chart, across the whole grid, because twelve months do not fit half of it.
    monthly = breakdown['monthly']
    trend_drawn = bool(total['revenue']) and len(monthly) >= 2
    if trend_drawn:
        base['charts'].append(chart(
            'payment_shares_by_month',
            'Доли форм оплаты по месяцам',
            'line',
            [short_month_label(row['month']) for row in monthly],
            [
                {
                    'label': label,
                    'data': [share(row[key], row['revenue']) for row in monthly],
                    'format': PERCENT_FORMAT,
                }
                for key, label in forms
            ],
            wide=True,
            x_kind='time',
        ))
    table_columns = [('metric', 'Показатель', TEXT_FORMAT)]
    # Shares first: the structure is what the report is read for, the roubles back it up.
    # Branch titles are long column headers, so they wrap instead of stretching the table.
    base['tables'] = [
        table(
            'payment_shares',
            'Доли форм оплаты',
            table_columns + [(key, label, PERCENT_FORMAT) for key, label in columns],
            [metric_row(label, key, True) for key, label in forms],
            wrap_headers=True,
            row_key='metric',
        ),
        table(
            'payment_amounts',
            'Суммы по формам оплаты',
            table_columns + [(key, label, MONEY_FORMAT) for key, label in columns],
            [metric_row('Выручка', 'revenue', False)] + [metric_row(label, key, False) for key, label in forms],
            wrap_headers=True,
            row_key='metric',
        ),
    ]

    base['notes'].append({
        'kind': 'info',
        'title': 'Методика',
        'text': (
            'Выручка считается так же, как на Обзоре: услуги завершённых визитов, товары и пополнения. '
            'Кассы делятся по типу счёта YClients; счета без типа попадают в «Прочие кассы». '
            'Яндекс Пэй вводится вручную за месяц с 1-го числа по дату «данные по» и вычитается из безналичных.'
        ),
    })
    unapplied = [branch for branch in branches if not branch['yandex_pay_applied']]
    if unapplied:
        # The trend decides value by value, so it can carve Yandex Pay out where the tables above do not.
        trend_note = (
            ' На графике по месяцам Яндекс Пэй вычтен там, где введённая сумма укладывается в период.'
            if trend_drawn and any(row['yandex_pay'] is not None for row in monthly)
            else ''
        )
        base['notes'].append(
            {
                'kind': 'warning',
                'title': 'Яндекс Пэй не учтён',
                'text': (
                    f'{_unapplied_phrases(unapplied, branches, req.end)} '
                    f'Безналичные показаны целиком, как в YClients.{trend_note}'
                ),
            }
        )
    missing = [branch for branch in branches if branch['yandex_pay_missing_months']]
    if missing:
        # A long period turns "every missing month of every branch" into a wall of text, so a
        # branch with nothing entered is one name in one phrase, and the months are shown
        # only where some were entered and the gap is the news.
        untouched = [branch['title'] for branch in missing if branch['yandex_pay'] is None]
        gaps = [
            f"{branch['title']}: {describe_months(branch['yandex_pay_missing_months'])}"
            for branch in missing
            if branch['yandex_pay'] is not None
        ]
        phrases = gaps + ([f"Не введён ни за один месяц периода: {', '.join(untouched)}"] if untouched else [])
        base['notes'].append({
            'kind': 'warning',
            'title': 'Яндекс Пэй введён не за все месяцы',
            'text': '. '.join(phrases),
        })
    partial_note = _data_through_note(branches, req.end, factual_branch_date(req.factual_at))
    if partial_note:
        base['notes'].append(partial_note)
    excess = [branch for branch in branches if branch['cashless'] < 0]
    if excess:
        base['notes'].append({
            'kind': 'warning',
            'title': 'Яндекс Пэй больше безналичных YClients',
            'text': (
                f"{', '.join(branch['title'] for branch in excess)}. "
                'Проверьте введённую сумму или способ проведения оплат Яндекс Пэй в YClients.'
            ),
        })
    # The table nets a branch over the whole period, so one month can dip below zero in the trend
    # while every branch above stays positive; without a word the line reads as a broken report.
    negative_months = [row['month'] for row in monthly if row['cashless'] < 0] if trend_drawn else []
    if negative_months:
        base['notes'].append({
            'kind': 'warning',
            'title': 'Яндекс Пэй больше безналичных YClients по месяцам',
            'text': (
                f"{describe_months(negative_months)}. "
                'Проверьте введённую сумму или способ проведения оплат Яндекс Пэй в YClients.'
            ),
        })
    return base


def _unapplied_phrases(unapplied: list[dict[str, Any]], branches: list[dict[str, Any]], end: date) -> str:
    """Why Yandex Pay is not netted out of these branches' cashless, in the words the period's reader needs."""

    def who(group: list[dict[str, Any]]) -> str:
        return '' if len(group) == len(branches) else f"{', '.join(branch['title'] for branch in group)}: "

    starts_mid = [b for b in unapplied if b['yandex_pay_unapplied_reason'] == REASON_STARTS_MID_MONTH]
    beyond = [b for b in unapplied if b['yandex_pay_unapplied_reason'] == REASON_BEYOND_PERIOD]
    phrases = []
    if starts_mid:
        phrases.append(
            f'{who(starts_mid)}Яндекс Пэй вводится за месяц с 1-го числа, а период начинается позже — '
            'выберите период с 1-го числа месяца.'
        )
    if beyond:
        covered = ', '.join(
            f"{b['title']} (данные по "
            f"{format_day(date.fromisoformat(b['yandex_pay_unapplied_data_through']), end.year)})"
            for b in beyond
        )
        phrases.append(
            f'Введённая сумма Яндекс Пэй покрывает дни после конца периода: {covered}. '
            'Продлите период хотя бы до этой даты.'
        )
    return ' '.join(phrases)


def _data_through_note(
    branches: list[dict[str, Any]],
    end: date,
    today: date,
) -> dict[str, Any] | None:
    """Info note for counted Yandex Pay values entered before their month was over, by month and date."""
    partial: dict[str, dict[date, list[str]]] = {}
    for branch in branches:
        for month, iso in branch['yandex_pay_data_through'].items():
            through = date.fromisoformat(iso)
            month_start = date.fromisoformat(f'{month}-01')
            if through < month_start.replace(day=monthrange(month_start.year, month_start.month)[1]):
                partial.setdefault(month, {}).setdefault(through, []).append(branch['title'])
    if not partial:
        return None
    lines = []
    for month, by_day in sorted(partial.items()):
        groups = [
            f"{'данные по' if index == 0 else 'по'} {format_day(through, end.year)} — {', '.join(titles)}"
            for index, (through, titles) in enumerate(sorted(by_day.items(), reverse=True))
        ]
        lines.append(f"{month_label(month)}: {'; '.join(groups)}")
    text = '. '.join(lines) + '.'
    revenue_through = min(end, today)
    if any(through < revenue_through for by_day in partial.values() for through in by_day):
        text += (
            f' Выручка YClients посчитана по {format_day(revenue_through, end.year)}, поэтому доля Яндекс Пэй '
            'за дни после даты «данные по» может быть занижена.'
        )
    return {'kind': 'info', 'title': 'Яндекс Пэй внесён не за весь месяц', 'text': text}
