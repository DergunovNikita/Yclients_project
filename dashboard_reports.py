"""Report catalog and report-data builders for the product dashboard SPA."""

from __future__ import annotations

import importlib
import importlib.util
import traceback
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from calendar import monthrange
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from plan_config import normalize_staff_category

import dashboard_service
from dashboard_service import (
    COMPLETED_ATTENDANCE,
    GOODS_SALE_TYPE_ID,
    GOODS_SOLD_ITEM_TYPE,
    SERVICE_SOLD_ITEM_TYPE,
    _business_financial_master_condition,
    _business_staff_id_condition,
    _company_scope_clause,
    _appointment_factual_at_condition,
    _financial_staff_attribution_condition,
    _appointment_company_ids,
    _personal_account_condition,
    _pct_change,
    _physical_account_condition,
    _service_paid_filters,
    _source_coverage_status,
    business_appointment_condition,
    financial_appointment_match_condition,
    fetch_opz_year_facts,
    fetch_plan_fact,
    fetch_summary,
    fetch_reporting_windows,
    fetch_year_over_year_facts,
    DateRange,
    factual_branch_date,
    ReportingWindow,
    reporting_window_clause,
)
from payment_methods import (
    REASON_BEYOND_PERIOD,
    REASON_STARTS_MID_MONTH,
    describe_months,
    fetch_payment_breakdown,
    format_day,
    month_label,
    short_month_label,
)
from report_payload import (
    DECIMAL_FORMAT,
    MONEY_FORMAT,
    NUMBER_FORMAT,
    PERCENT_FORMAT,
    ReportRequest,
    card as _card,
    chart as _chart,
    ranking_table as _ranking_table,
    table as _table,
    without_empty_columns as _without_empty_columns,
)
from models import (
    AccountCatalog,
    Appointment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Staff,
    SyncSourceState,
)

REPORT_GRANULARITIES = {'day', 'week', 'month'}
# Daily history since 2018 is ~3,200 periods; this leaves room for decades of weeks and months.
MAX_PERIOD_BUCKETS = 6000


def _report_now() -> datetime:
    # Reports read the same clock as the rest of the dashboard, through the module so that
    # freezing `dashboard_service.factual_now` reaches reports too.
    return dashboard_service.factual_now()


YOY_ANNUAL_SOURCES = (
    'appointments_detail',
    'financial_transactions_detail',
    'goods_transactions_detail',
)
YOY_MONTHLY_SOURCES = YOY_ANNUAL_SOURCES[:2]


class ReportCalculationError(RuntimeError):
    """Raised when a report cannot produce any usable payload."""

    def __init__(self, message: str, *, stage: str = 'unknown') -> None:
        super().__init__(message)
        self.stage = stage


# Platform-admin only (`can_view_report_usage`) and built by the route with a tenant, unlike every other report.
REPORT_USAGE_ID = 'report_usage'
REPORT_GROUP_ORDER = ('finance', 'services', 'team', 'operations', 'clients', 'goods', 'diagnostics')


@dataclass(frozen=True)
class ReportDefinition:
    """One catalog entry. `builder` is a lazy 'module:function' path (see `_builder_for`), not part of the payload."""

    id: str
    title: str
    description: str
    group: str
    type: str
    themes: tuple[str, ...]
    roles: tuple[str, ...]
    builder: str
    # Whether the payload carries money. A role without the `revenue` metric cannot open such a report
    # or see it in the catalog; unknown ids fail closed the same way.
    requires_financials: bool
    granularity: bool = False
    compare: bool = False
    staff_filter: bool = True
    date_range: bool = True
    aliases: tuple[str, ...] = ()
    status: str = 'ready'
    required_sources: tuple[str, ...] = ('yclients',)

    def to_payload(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'group': self.group,
            'type': self.type,
            'themes': list(self.themes),
            'roles': list(self.roles),
            'filters': {
                'date_range': self.date_range,
                'branch': True,
                'staff': self.staff_filter,
                'granularity': self.granularity,
                'compare': self.compare,
            },
            'status': self.status,
            'required_sources': list(self.required_sources),
            'aliases': list(self.aliases),
        }


_OWNER_ROLES = ('владельцу', 'управляющему')
_MANAGER_ROLES = ('управляющему', 'владельцу')
_FRONT_DESK_ROLES = ('администратору', 'управляющему')
_PLATFORM_ADMIN_ROLES = ('платформенному администратору',)

# The catalog: one row per report. Catalog order is REPORT_GROUP_ORDER, then the order of this list.
# `aliases` are retired ids that still resolve here, so old links and favourites keep working.
REPORT_DEFINITIONS: tuple[ReportDefinition, ...] = (
    ReportDefinition(
        id='financial_overview',
        title='Финансовый обзор',
        description=(
            'Выручка, средний чек и завершённые визиты по периодам: услуги, товары и пополнения отдельно, '
            'с динамикой по дням, неделям или месяцам.'
        ),
        group='finance',
        type='финансовый',
        themes=('выручка', 'средний чек'),
        roles=_OWNER_ROLES,
        builder='reports_finance:build_financial_overview',
        requires_financials=True,
        granularity=True,
        compare=True,
        aliases=('revenue_dynamics', 'avg_check_dynamics', 'revenue_decomposition', 'day_overview'),
    ),
    ReportDefinition(
        id='payment_methods',
        title='Формы оплаты',
        description='Доли наличных, безналичных платежей, Яндекс Пэй и прочих касс в выручке по филиалам и месяцам.',
        group='finance',
        type='финансовый',
        themes=('выручка',),
        roles=_OWNER_ROLES,
        builder='dashboard_reports:build_payment_methods',
        requires_financials=True,
        # Branch-level totals by payment form have no per-employee reading.
        staff_filter=False,
    ),
    ReportDefinition(
        id='year_over_year',
        title='Сравнение по годам',
        description='Выручка, визиты, средний чек и ОПЗ по годам и по месяцам года к году.',
        group='finance',
        type='финансовый',
        themes=('выручка', 'средний чек'),
        roles=_OWNER_ROLES,
        builder='dashboard_reports:build_year_over_year',
        requires_financials=True,
        date_range=False,
        aliases=('seasonality',),
    ),
    ReportDefinition(
        id='avg_check_by_service',
        title='Услуги: количество, цена, выручка',
        description=(
            'Каждая услуга: сколько раз оказана, какую выручку и долю выручки даёт и по какой средней цене; '
            'дополнительные услуги отдельно.'
        ),
        group='services',
        type='операционный',
        themes=('услуги', 'средний чек'),
        roles=_OWNER_ROLES,
        builder='reports_services:build_avg_check_by_service',
        requires_financials=True,
        compare=True,
        aliases=('service_trends',),
    ),
    ReportDefinition(
        id='service_staff_profit',
        title='Услуги по мастерам',
        description='Услуги в разрезе мастеров: сколько раз каждый мастер оказал услугу, выручка и средняя цена.',
        group='services',
        type='операционный',
        themes=('услуги', 'мастера'),
        roles=_OWNER_ROLES,
        builder='reports_services:build_service_staff_profit',
        requires_financials=True,
        compare=True,
        aliases=('staff_services',),
    ),
    ReportDefinition(
        id='service_combos',
        title='Комбинации услуг',
        description='Какие услуги клиенты берут вместе в одном визите и как часто встречается каждая пара.',
        group='services',
        type='операционный',
        themes=('услуги',),
        roles=_OWNER_ROLES,
        builder='reports_services:build_service_combos',
        requires_financials=False,
        compare=True,
    ),
    ReportDefinition(
        id='staff_efficiency',
        title='Эффективность сотрудников',
        description='Сводка по сотрудникам: завершённые записи, клиенты, выручка услуг и выручка на запись.',
        group='team',
        type='управленческий',
        themes=('мастера',),
        roles=_MANAGER_ROLES,
        builder='reports_operations:build_staff_efficiency',
        requires_financials=True,
        compare=True,
    ),
    ReportDefinition(
        id='staff_leaderboard',
        title='Рейтинги и топы',
        description=(
            'Рейтинги мастеров и администраторов по выручке, допуслугам, косметике, ОПЗ, отзывам '
            'и выполнению плана среднего чека.'
        ),
        group='team',
        type='управленческий',
        themes=('мастера',),
        roles=_MANAGER_ROLES,
        builder='dashboard_reports:build_staff_leaderboard',
        # A mixed report (OPZ, reviews and percentages next to money): the route strips the money columns.
        requires_financials=False,
    ),
    ReportDefinition(
        id='peak_load',
        title='Загрузка по дням недели и часам',
        description=(
            'Завершённые визиты по дням недели и часам по местному времени филиала: '
            'когда загрузка пиковая, а когда кресла простаивают.'
        ),
        group='operations',
        type='операционный',
        themes=('записи',),
        roles=_MANAGER_ROLES,
        builder='reports_operations:build_peak_load',
        requires_financials=False,
        compare=True,
        aliases=('staff_time_heatmap',),
    ),
    ReportDefinition(
        id='bookings_dynamics',
        title='Записи и неявки',
        description='Динамика записей по периодам и мастерам: сколько записано, сколько завершено и сколько не пришло.',
        group='operations',
        type='операционный',
        themes=('записи',),
        roles=_MANAGER_ROLES,
        builder='reports_operations:build_bookings_dynamics',
        requires_financials=False,
        granularity=True,
        compare=True,
        aliases=('cancellation_analysis',),
    ),
    ReportDefinition(
        id='booking_channels',
        title='Каналы записи: онлайн и администратор',
        description='Сколько записей клиенты оформили сами онлайн и сколько создали сотрудники: по периодам и в долях.',
        group='operations',
        type='операционный',
        themes=('записи',),
        roles=_MANAGER_ROLES,
        builder='reports_operations:build_booking_channels',
        # Counts only: record totals and shares, no money anywhere in the payload.
        requires_financials=False,
        granularity=True,
        compare=True,
    ),
    ReportDefinition(
        id='new_vs_returning_cross',
        title='Новые и повторные клиенты',
        description='Сколько клиентов пришло впервые и сколько вернулось, их доли и изменение к прошлому периоду.',
        group='clients',
        type='клиентский',
        themes=('клиенты',),
        roles=_FRONT_DESK_ROLES,
        builder='dashboard_reports:build_new_vs_returning_cross',
        # Counts only, and the Overview links here, so a role without revenue must reach it.
        requires_financials=False,
        compare=True,
    ),
    ReportDefinition(
        id='top_clients_pareto',
        title='Клиентская база: концентрация и частотность',
        description='Какую долю выручки дают лучшие клиенты (принцип Парето) и как часто клиенты возвращаются.',
        group='clients',
        type='клиентский',
        themes=('клиенты', 'выручка'),
        roles=_FRONT_DESK_ROLES,
        builder='reports_clients:build_top_clients_pareto',
        requires_financials=True,
        compare=True,
        aliases=('rfm_analysis', 'client_labels', 'client_journey'),
    ),
    ReportDefinition(
        id='retention_3_6_12',
        title='Возвратность новых клиентов',
        description='Какая доля новых клиентов каждого месяца вернулась в течение 1, 3, 6 и 12 месяцев.',
        group='clients',
        type='клиентский',
        themes=('клиенты',),
        roles=_FRONT_DESK_ROLES,
        builder='reports_clients:build_retention_3_6_12',
        # Cohort sizes and percentages only; the payload carries no money.
        requires_financials=False,
        aliases=('client_cohorts', 'month_return'),
    ),
    ReportDefinition(
        id='revenue_at_risk',
        title='Отток клиентов',
        description=(
            'Клиенты, не вернувшиеся в течение 90 дней после последнего визита: их число и доля, '
            'выручка под риском и мастера, у которых они были в последний раз.'
        ),
        group='clients',
        type='клиентский',
        themes=('клиенты', 'отток'),
        roles=_FRONT_DESK_ROLES,
        builder='reports_clients:build_revenue_at_risk',
        requires_financials=True,
        compare=True,
        aliases=('lost_clients_list', 'return_priorities', 'losses_by_staff', 'churn_dynamics'),
    ),
    ReportDefinition(
        id='goods_dynamics',
        title='Товары',
        description=(
            'Продажи товаров: выручка, единицы и доля визитов с покупкой товара, '
            'в разрезе периодов, товаров и продавцов.'
        ),
        group='goods',
        type='товарный',
        themes=('товары', 'выручка'),
        roles=_OWNER_ROLES,
        builder='reports_goods:build_goods_dynamics',
        requires_financials=True,
        granularity=True,
        compare=True,
        aliases=('top_goods_revenue', 'goods_by_staff', 'goods_conversion'),
    ),
    ReportDefinition(
        id='nps_dashboard',
        title='Отзывы',
        description='Отзывы клиентов из YClients: количество, средняя оценка, распределение оценок и низкие оценки.',
        group='clients',
        type='клиентский',
        themes=('клиенты', 'отзывы'),
        roles=_OWNER_ROLES,
        builder='reports_feedback:build_nps_dashboard',
        requires_financials=False,
        compare=True,
    ),
    ReportDefinition(
        id=REPORT_USAGE_ID,
        title='Использование отчётов',
        description=(
            'Какие отчёты открывают сотрудники выбранного аккаунта: дни-пользователи, вызовы, доля со сравнением, '
            'время ответа и отчёты, которые давно не открывали.'
        ),
        group='diagnostics',
        type='служебный',
        themes=('диагностика',),
        roles=_PLATFORM_ADMIN_ROLES,
        # Dispatched by the route with an explicit tenant (the registry builder refuses to run without one).
        builder='report_usage:build_report_usage',
        requires_financials=False,
        # Own gate: `can_view_report_usage`, not a money metric.
        staff_filter=False,
    ),
)


def _build_registry(definitions: tuple[ReportDefinition, ...]) -> dict[str, ReportDefinition]:
    """Validate the table and index it in catalog order; a malformed table is a startup error."""
    ids = [definition.id for definition in definitions]
    if len(set(ids)) != len(ids):
        raise RuntimeError('duplicate report id in REPORT_DEFINITIONS')
    claimed = set(ids)
    for definition in definitions:
        if definition.group not in REPORT_GROUP_ORDER:
            raise RuntimeError(f'report {definition.id}: unknown group {definition.group}')
        module_name, separator, function_name = definition.builder.partition(':')
        if not (module_name and separator and function_name):
            raise RuntimeError(f'report {definition.id}: builder must be "module:function"')
        if importlib.util.find_spec(module_name) is None:
            raise RuntimeError(f'report {definition.id}: builder module {module_name} not found')
        for alias in definition.aliases:
            if alias in claimed:
                raise RuntimeError(f'report alias {alias} of {definition.id} is already taken')
            claimed.add(alias)
    ordered = sorted(definitions, key=lambda definition: REPORT_GROUP_ORDER.index(definition.group))
    return {definition.id: definition for definition in ordered}


REPORT_REGISTRY = _build_registry(REPORT_DEFINITIONS)
REPORT_ALIASES: dict[str, str] = {
    alias: definition.id for definition in REPORT_REGISTRY.values() for alias in definition.aliases
}


def resolve_report_id(raw: str | None) -> str | None:
    """Canonical id for a canonical id or a retired alias; None for anything unknown."""
    if raw in REPORT_REGISTRY:
        return raw
    return REPORT_ALIASES.get(raw) if raw else None


def report_requires_financials(report_id: str) -> bool:
    """Money gate of a report, read from the definition of the resolved id. Unknown ids fail closed."""
    definition = REPORT_REGISTRY.get(resolve_report_id(report_id) or '')
    return True if definition is None else definition.requires_financials


@lru_cache(maxsize=None)
def _builder_for(report_id: str) -> Callable[[ReportRequest], Awaitable[dict[str, Any]]]:
    """Resolve the builder named by the definition.

    Imported lazily: builder modules import helpers from this one, so importing them at the top
    would be circular.
    """
    module_name, _, function_name = REPORT_REGISTRY[report_id].builder.partition(':')
    return getattr(importlib.import_module(module_name), function_name)


# The demo tenant is seeded, not synced: it holds a few months of activity and no
# SyncSourceState coverage at all. year_over_year certifies whole years against
# that coverage, so for demo it can only ever render every metric as unknown.
DEMO_UNAVAILABLE_REPORTS = frozenset({'year_over_year', REPORT_USAGE_ID})


# Reports that disclose branch-level payment totals; the caller's right to them is
# `can_view_branch_payments`, which is stricter than the revenue gate.
BRANCH_PAYMENT_REPORTS = frozenset({'payment_methods'})


def fetch_report_registry(
    is_demo: bool = False,
    *,
    hide_financials: bool = False,
    hide_branch_payments: bool = False,
    hide_report_usage: bool = False,
) -> list[dict[str, Any]]:
    """Catalog of reports the caller can actually open.

    A card the role may not open is worse than no card: `/reports/data` answers 403 for it,
    so the only thing it can do is take the user to an error page.
    """
    return [
        definition.to_payload()
        for definition in REPORT_REGISTRY.values()
        if not (is_demo and definition.id in DEMO_UNAVAILABLE_REPORTS)
        and not (hide_financials and definition.requires_financials)
        and not (hide_branch_payments and definition.id in BRANCH_PAYMENT_REPORTS)
        and not (hide_report_usage and definition.id == REPORT_USAGE_ID)
    ]


async def fetch_report_data(
    db: AsyncSession,
    report_id: str,
    start: date,
    end: date,
    company_id: int | None = None,
    staff_id: int | None = None,
    granularity: str = 'day',
    compare_start: date | None = None,
    compare_end: date | None = None,
    compare_staff_id: int | None = None,
    allowed_company_ids: list[int] | None = None,
    period_preset: str | None = None,
    compare_previous: bool = False,
) -> dict[str, Any]:
    """Report payload for a canonical id or a retired alias.

    The payload always carries the canonical `report_id`; `requested_report_id` appears only when
    the caller asked by an alias. `compare_previous` measures the period against the Overview's
    baseline (`DateRange.previous_period`); an explicit compare window wins over it.
    """
    canonical_id = resolve_report_id(report_id)
    if canonical_id is None:
        raise ValueError(f'unknown report_id: {report_id}')
    if granularity not in REPORT_GRANULARITIES:
        raise ValueError('granularity must be one of day, week, month')
    if start > end:
        raise ValueError('start_date must be <= end_date')
    if compare_start and compare_end and compare_start > compare_end:
        raise ValueError('compare_start_date must be <= compare_end_date')

    factual_at = _report_now()
    # An explicit comparison governs the whole page. Letting the preset drive the deltas
    # inside the tables while the comparison block measures against the window the user
    # ticked would put two different percentages for one metric on one screen — the
    # segments table and the comparison panel of `new_vs_returning_cross` sit together.
    explicit_window = bool(compare_start and compare_end)
    # The comparison window must come from the preset the page was opened with, before it is dropped below.
    cmp_start, cmp_end = (
        (compare_start, compare_end)
        if explicit_window
        else _previous_window(start, end, period_preset)
        if compare_previous
        else (start, end)
    )
    if explicit_window or compare_staff_id is not None:
        period_preset = None
    data = await _fetch_report_payload(
        db,
        canonical_id,
        start,
        end,
        company_id,
        staff_id,
        granularity,
        allowed_company_ids,
        factual_at,
        period_preset,
    )
    if canonical_id != report_id:
        data['requested_report_id'] = report_id
    if REPORT_REGISTRY[canonical_id].compare and (explicit_window or compare_previous or compare_staff_id is not None):
        cmp_staff_id = compare_staff_id if compare_staff_id is not None else staff_id
        compare_data = await _fetch_report_payload(
            db,
            canonical_id,
            cmp_start,
            cmp_end,
            company_id,
            cmp_staff_id,
            granularity,
            allowed_company_ids,
            factual_at,
            # The preset names the primary window; the compare window is its own range.
            None,
            for_comparison=True,
        )
        data['comparison'] = _comparison_payload(data, compare_data, cmp_staff_id)
    return data


def _previous_window(start: date, end: date, period_preset: str | None) -> tuple[date, date]:
    try:
        previous = DateRange(start=start, end=end).previous_period(period_preset)
    except (OverflowError, ValueError) as exc:  # a period at the very start of the calendar has no baseline
        raise ValueError('the period has no previous period to compare with') from exc
    return previous.start, previous.end


def _comparison_payload(data: dict[str, Any], compare_data: dict[str, Any], staff_id: int | None) -> dict[str, Any]:
    """Comparison block: cards matched by label, charts and tables only for ids the current payload has.

    A table without a `row_key` cannot be paired row by row, so it is not carried at all (a list of events
    such as the negative reviews would only ship the previous window's rows for nothing).

    `raw` is left out on purpose: the page renders only cards, charts and tables, and the raw block
    of a long window is the largest part of a payload.
    """
    chart_ids = {item.get('id') for item in data.get('charts', [])}
    table_ids = {item.get('id') for item in data.get('tables', [])}
    return {
        'period': compare_data['period'],
        'staff_id': staff_id,
        'source_status': compare_data['source_status'],
        'cards': compare_data.get('cards', []),
        'rows': _comparison_rows(data.get('cards', []), compare_data.get('cards', [])),
        'charts': [
            {'id': item['id'], 'labels': item.get('labels', []), 'datasets': item.get('datasets', [])}
            for item in compare_data.get('charts', [])
            if item.get('id') in chart_ids
        ],
        'tables': [
            {'id': item['id'], 'rows': _comparison_table_rows(item)}
            for item in compare_data.get('tables', [])
            if item.get('id') in table_ids and item.get('row_key')
        ],
    }


def _comparison_table_rows(table_payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of a table; a ranking table is re-sorted client-side, so every metric's rows are merged by key."""
    ranking = table_payload.get('ranking')
    row_key = table_payload.get('row_key')
    if not ranking or not row_key:
        return table_payload.get('rows', [])
    merged: dict[Any, dict[str, Any]] = {}
    for rows in ranking.get('rows_by_metric', {}).values():
        for row in rows:
            merged.setdefault(row.get(row_key), row)
    return list(merged.values())


async def _fetch_report_payload(
    db: AsyncSession,
    report_id: str,
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    granularity: str,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
    period_preset: str | None = None,
    for_comparison: bool = False,
) -> dict[str, Any]:
    definition = REPORT_REGISTRY[report_id]
    base = {
        'report_id': definition.id,
        'title': definition.title,
        'period': {'start': start.isoformat(), 'end': end.isoformat(), 'granularity': granularity},
        'source_status': definition.status,
        'missing_sources': [],
        'cards': [],
        'charts': [],
        'tables': [],
        'notes': [],
        'raw': {},
    }
    if not definition.staff_filter:
        # No employee filter, and the roles that may open such a report are not clamped to a
        # staff row, so a stray staff_id must not narrow the branch totals.
        staff_id = None
    allowed_company_ids = await _appointment_company_ids(
        db, company_id, staff_id, allowed_company_ids
    )
    base['calculation_scope'] = await _report_calculation_scope(
        db,
        report_id,
        company_id,
        staff_id,
    )
    request = ReportRequest(
        db=db,
        base=base,
        start=start,
        end=end,
        company_id=company_id,
        staff_id=staff_id,
        granularity=granularity,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
        period_preset=period_preset,
        for_comparison=for_comparison,
    )
    return await _builder_for(report_id)(request)


async def build_payment_methods(req: ReportRequest) -> dict[str, Any]:
    return await _payment_methods_payload(
        req.db, req.base, req.start, req.end, req.allowed_company_ids, req.factual_at
    )


async def build_year_over_year(req: ReportRequest) -> dict[str, Any]:
    return await _year_over_year_payload(
        req.db, req.base, req.company_id, req.staff_id, req.allowed_company_ids, req.factual_at
    )


async def build_staff_leaderboard(req: ReportRequest) -> dict[str, Any]:
    return await _leaderboard_payload(
        req.db, req.base, req.start, req.end, req.company_id, req.staff_id, req.allowed_company_ids, req.factual_at
    )


async def build_new_vs_returning_cross(req: ReportRequest) -> dict[str, Any]:
    return await _client_recency_payload(
        req.db, req.base, req.start, req.end, req.company_id, req.staff_id, req.allowed_company_ids, req.factual_at,
        req.period_preset,
    )


async def _report_calculation_scope(
    db: AsyncSession,
    report_id: str,
    company_id: int | None,
    staff_id: int | None,
) -> dict[str, Any]:
    if staff_id is not None:
        staff = (
            await db.execute(
                select(
                    Staff.name,
                    Staff.position,
                    Staff.company_id,
                    Company.title.label('company_title'),
                )
                .join(Company, Company.id == Staff.company_id)
                .where(Staff.id == staff_id)
            )
        ).one_or_none()
        return {
            'kind': 'staff',
            'mode': 'plan_fact' if report_id == 'staff_leaderboard' else 'personal',
            'staff_id': staff_id,
            'staff_name': staff.name if staff is not None else None,
            'staff_category': (
                normalize_staff_category(staff.position)
                if staff is not None
                else None
            ),
            'company_id': int(staff.company_id) if staff is not None else company_id,
            'company_title': staff.company_title if staff is not None else None,
        }
    if company_id is not None:
        return {
            'kind': 'branch',
            'mode': 'aggregate',
            'company_id': company_id,
        }
    return {'kind': 'network', 'mode': 'aggregate'}


def _appointment_conditions(
    start: date | None,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    *,
    attended_only: bool = False,
    allowed_company_ids: list[int] | None = None,
    factual_at: datetime | None = None,
) -> list[Any]:
    """Shared appointment filters. A `start` of None opens the window to full history."""
    conditions = [
        Appointment.date <= end,
        business_appointment_condition(),
        reporting_window_clause(Appointment.company_id, Appointment.date),
    ]
    if start is not None:
        conditions.append(Appointment.date >= start)
    if attended_only:
        conditions.append(Appointment.attendance == COMPLETED_ATTENDANCE)
    if factual_at is not None:
        conditions.append(_appointment_factual_at_condition(factual_at))
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        conditions.append(scope)
    if staff_id is not None:
        conditions.append(Appointment.staff_id == staff_id)
    return conditions


def _period_key(value: date | datetime | None, granularity: str) -> str:
    if value is None:
        return ''
    day = value.date() if isinstance(value, datetime) else value
    if granularity == 'month':
        return day.replace(day=1).isoformat()
    if granularity == 'week':
        return (day - timedelta(days=day.weekday())).isoformat()
    return day.isoformat()


def _period_starts(start: date, end: date, granularity: str) -> list[str]:
    """Every period start from the one holding `start` to the one holding `end`, so empty periods stay on the axis.

    Raises ValueError past `MAX_PERIOD_BUCKETS`: the axis is zero-filled, so a window of centuries by day would
    otherwise allocate millions of rows for a request that has data on a few hundred of them.
    """
    current = date.fromisoformat(_period_key(start, granularity))
    keys = []
    while current <= end:
        if len(keys) >= MAX_PERIOD_BUCKETS:
            raise ValueError(f'period is too long for granularity {granularity}: choose a coarser one or a shorter period')
        keys.append(current.isoformat())
        try:
            if granularity == 'month':
                current = (current + timedelta(days=32)).replace(day=1)
            else:
                current += timedelta(days=7 if granularity == 'week' else 1)
        except OverflowError:
            break
    return keys


def _aggregate_daily(rows: list[dict[str, Any]], granularity: str) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: {
        'revenue': 0.0,
        'service_revenue': 0.0,
        'goods_revenue': 0.0,
        'topup_revenue': 0.0,
        'appointments': 0.0,
    })
    for row in rows:
        key = _period_key(date.fromisoformat(row['date']), granularity)
        grouped[key]['revenue'] += float(row.get('revenue') or 0)
        grouped[key]['service_revenue'] += float(row.get('service_revenue') or 0)
        grouped[key]['goods_revenue'] += float(row.get('goods_revenue') or 0)
        grouped[key]['topup_revenue'] += float(row.get('topup_revenue') or 0)
        grouped[key]['appointments'] += float(row.get('appointments') or 0)
    return [{'period': key, **values} for key, values in sorted(grouped.items())]


def _comparison_rows(current_cards: list[dict[str, Any]], compare_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compare_by_label = {card.get('label'): card for card in compare_cards}
    rows = []
    for card in current_cards:
        label = card.get('label')
        compare_card = compare_by_label.get(label)
        current_value = card.get('value')
        compare_value = compare_card.get('value') if compare_card else None
        delta = None
        delta_pct = None
        if isinstance(current_value, (int, float)) and isinstance(compare_value, (int, float)):
            delta = current_value - compare_value
            # A zero base has no percentage change; the page prints a dash instead of a made-up +100%.
            # Four decimals, not two: the page rounds once to one decimal, and a value already rounded to
            # -0.55 would land on -0.6 while the table cells of the same metric (exact values) say -0.5.
            delta_pct = round(100.0 * delta / compare_value, 4) if compare_value else None
        rows.append({
            'label': label,
            'format': card.get('format') or (compare_card.get('format') if compare_card else None),
            'current': current_value,
            'compare': compare_value,
            'delta': delta,
            'delta_pct': delta_pct,
        })
    return rows


async def _year_over_year_activity_bounds(
    db: AsyncSession,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    now: datetime,
) -> tuple[date, date, dict[str, Any]] | None:
    """Return the first YClients record, the latest factual metric activity and the OPZ facts.

    Both boundaries include every factual component used by Overview/plan-fact:
    completed visits, paid services, goods/top-up payments and goods movements.
    """
    today = now.date()
    appointment_conditions = [
        Appointment.date.is_not(None),
        Appointment.attendance == COMPLETED_ATTENDANCE,
        or_(
            Appointment.date < today,
            and_(
                Appointment.date == today,
                or_(
                    Appointment.datetime.is_(None),
                    Appointment.datetime <= now,
                ),
            ),
        ),
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
                func.max(Appointment.date).label('activity_end'),
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
                func.max(payment_day).label('activity_end'),
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

    direct_component = FinancialTransaction.sold_item_type == GOODS_SOLD_ITEM_TYPE
    if staff_id is not None:
        direct_component = or_(
            and_(
                FinancialTransaction.sold_item_type == GOODS_SOLD_ITEM_TYPE,
                _financial_staff_attribution_condition(staff_id),
            ),
            and_(
                _personal_account_condition(),
                _financial_staff_attribution_condition(staff_id),
            ),
        )
    else:
        direct_component = or_(direct_component, _personal_account_condition())
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
                func.max(payment_day).label('activity_end'),
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
                func.max(goods_day).label('activity_end'),
            ).where(*goods_conditions)
        )
    ).one()

    def as_date(value: Any) -> date | None:
        if value is None:
            return None
        return value if isinstance(value, date) and not isinstance(value, datetime) else date.fromisoformat(str(value)[:10])

    component_bounds = (
        appointment_bounds,
        service_bounds,
        direct_bounds,
        goods_bounds,
    )
    starts = [
        parsed
        for bounds in component_bounds
        if (parsed := as_date(bounds.activity_start)) is not None
    ]
    ends = [
        parsed
        for bounds in component_bounds
        if (parsed := as_date(bounds.activity_end)) is not None
    ]
    if not starts or not ends:
        return None

    activity_start = min(starts)
    opz_facts = await fetch_opz_year_facts(
        db,
        activity_start,
        today,
        company_id,
        staff_id,
        allowed_company_ids,
        factual_at=now,
    )
    if opz_facts['latest_date'] is not None:
        ends.append(opz_facts['latest_date'])
    return activity_start, max(ends), opz_facts


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
                SyncSourceState.source.in_(YOY_ANNUAL_SOURCES),
            )
        )
    ).scalars().all()
    return {(int(state.company_id), state.source): state for state in states}


def _year_over_year_missing_sources(
    state_by_key: dict[tuple[int, str], SyncSourceState],
    scope_company_ids: list[int],
    period_start: date,
    period_end: date,
    required_sources: tuple[str, ...] = YOY_ANNUAL_SOURCES,
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


def _mask_unknown_year_metrics(
    row: dict[str, Any],
    missing_sources: list[str],
) -> None:
    """Do not expose partial aggregates as factual zeroes or complete sums."""
    missing = set(missing_sources)
    appointments_known = 'appointments_detail' not in missing
    financials_known = 'financial_transactions_detail' not in missing
    goods_known = 'goods_transactions_detail' not in missing

    if not appointments_known:
        for metric in (
            'appointments',
            'service_count',
            'extra_service_count',
            'unique_clients',
            'visits_per_client',
            'opz_qty',
            'opz_pct',
        ):
            row[metric] = None

    if not (appointments_known and financials_known):
        for metric in (
            'revenue',
            'service_revenue',
            'goods_revenue',
            'topup_revenue',
            'extra_service_revenue',
            'avg_check',
        ):
            row[metric] = None

    if not goods_known:
        row['goods_count'] = None
    if 'staff_schedules' in missing:
        row['extra_service_count'] = None
        row['extra_service_revenue'] = None


def _year_periods(
    activity_start: date,
    activity_end: date,
    current_date: date,
    latest_fact_year: int | None = None,
) -> list[dict[str, Any]]:
    periods = []
    for year in range(activity_start.year, activity_end.year + 1):
        calendar_start = date(year, 1, 1)
        calendar_end = date(year, 12, 31)
        period_start = max(calendar_start, activity_start)
        period_end = min(calendar_end, activity_end)
        is_partial_year = (
            period_start != calendar_start
            or period_end != calendar_end
            or year == current_date.year
        )
        periods.append({
            'year': year,
            'start': period_start,
            'end': period_end,
            'is_partial_year': is_partial_year,
            'is_opening_year': year == activity_start.year,
            'is_latest_year': year == (
                latest_fact_year if latest_fact_year is not None else activity_end.year
            ),
        })
    return periods


def _year_row_from_summary(
    year: int,
    period_start: date,
    period_end: date,
    summary: dict[str, Any],
    *,
    is_partial_year: bool,
    is_opening_year: bool,
    is_latest_year: bool,
) -> dict[str, Any]:
    revenue = summary.get('revenue', {})
    average_check = summary.get('average_check', {})
    visits = summary.get('visit_metrics', {})
    appointments = int(revenue.get('appointments') or 0)
    return {
        'year': year,
        'period_start': period_start.isoformat(),
        'period_end': period_end.isoformat(),
        'revenue': float(revenue.get('total') or 0),
        'service_revenue': float(revenue.get('service_revenue') or 0),
        'goods_revenue': float(revenue.get('goods_revenue') or 0),
        'topup_revenue': float(revenue.get('topup_revenue') or 0),
        'appointments': appointments,
        'service_count': float(revenue.get('service_count') or 0),
        'goods_count': float(revenue.get('goods_count') or 0),
        'extra_service_count': float(revenue.get('extra_service_count') or 0),
        'extra_service_revenue': float(revenue.get('extra_service_revenue') or 0),
        'unique_clients': int(visits.get('unique_clients') or 0),
        # No completed visits means no average check; a zero would draw a real bar.
        'avg_check': float(average_check.get('total') or 0) if appointments else None,
        'visits_per_client': float(visits.get('visits_per_client') or 0),
        'opz_qty': float(visits.get('opz_qty') or 0),
        'opz_pct': float(visits.get('opz_pct') or 0),
        'source_status': average_check.get('source_status') or 'ready',
        'missing_components': list(average_check.get('missing_components') or []),
        'is_partial_year': is_partial_year,
        'period_status': 'Неполный' if is_partial_year else 'Полный',
        'is_opening_year': is_opening_year,
        'is_latest_year': is_latest_year,
    }


def _monthly_yoy_rows(
    year: int,
    daily: list[dict[str, Any]],
    period_start: date,
    period_end: date,
    state_by_key: dict[tuple[int, str], SyncSourceState],
    scope_company_ids: list[int],
    appointment_dependencies: dict[int, dict[int, tuple[date, date]]] | None = None,
    reporting_windows: dict[int, ReportingWindow] | None = None,
    opz_counts: dict[int, float] | None = None,
    opz_dependencies: dict[int, dict[int, tuple[date, date]]] | None = None,
) -> list[dict[str, Any]]:
    months = list(range(1, 13))
    monthly = {
        month: {
            'revenue': 0.0,
            'service_revenue': 0.0,
            'goods_revenue': 0.0,
            'topup_revenue': 0.0,
            'appointments': 0.0,
        }
        for month in months
    }
    for row in _aggregate_daily(daily, 'month'):
        month = date.fromisoformat(row['period']).month
        monthly[month]['revenue'] += float(row.get('revenue') or 0)
        monthly[month]['service_revenue'] += float(row.get('service_revenue') or 0)
        monthly[month]['goods_revenue'] += float(row.get('goods_revenue') or 0)
        monthly[month]['topup_revenue'] += float(row.get('topup_revenue') or 0)
        monthly[month]['appointments'] += float(row.get('appointments') or 0)

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
                YOY_MONTHLY_SOURCES,
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


async def _year_over_year_payload(
    db: AsyncSession,
    base: dict[str, Any],
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    report_now: datetime | None = None,
) -> dict[str, Any]:
    report_now = report_now or _report_now()
    scope_company_ids = await _appointment_company_ids(
        db, company_id, staff_id, allowed_company_ids
    )
    activity_bounds = await _year_over_year_activity_bounds(
        db,
        company_id,
        staff_id,
        scope_company_ids,
        report_now,
    )
    if activity_bounds is None:
        base['source_status'] = 'partial'
        base['notes'].append({
            'kind': 'missing',
            'title': 'Нет фактических записей YClients',
            'text': 'Для выбранного среза пока нельзя определить границы истории.',
        })
        base['raw'] = {
            'years': [],
            'months': [],
            'latest_year': None,
            'activity_start': None,
            'activity_end': None,
            'months_in_scope': [],
            'service_detail_excluded': True,
        }
        return base

    activity_start, activity_end, opz_facts = activity_bounds
    opz_by_year = opz_facts['counts']
    opz_dependencies_by_year = opz_facts['appointment_dependencies']
    report_end = report_now.date()
    periods = _year_periods(
        activity_start,
        report_end,
        report_end,
        latest_fact_year=activity_end.year,
    )
    state_by_key = await _year_over_year_source_states(db, scope_company_ids)
    reporting_windows = await fetch_reporting_windows(db, scope_company_ids)
    fact_rows = await fetch_year_over_year_facts(
        db,
        activity_start,
        report_end,
        company_id,
        staff_id,
        scope_company_ids,
        factual_at=report_now,
    )
    year_rows = []
    monthly_by_year: dict[int, list[dict[str, Any]]] = {}

    for period in periods:
        period_start = period['start']
        period_end = period['end']
        year = int(period['year'])
        annual = fact_rows['annual'].get(year, {})
        completed = int(annual.get('appointments') or 0)
        unique_clients = int(annual.get('unique_clients') or 0)
        revenue = float(annual.get('revenue') or 0)
        extra_service_count = float(annual.get('extra_service_count') or 0)
        extra_service_revenue = float(annual.get('extra_service_revenue') or 0)
        summary = {
            'revenue': {
                'total': revenue,
                'service_revenue': float(annual.get('service_revenue') or 0),
                'goods_revenue': float(annual.get('goods_revenue') or 0),
                'topup_revenue': float(annual.get('topup_revenue') or 0),
                'extra_service_revenue': extra_service_revenue,
                'appointments': completed,
                'service_count': float(annual.get('service_count') or 0),
                'goods_count': float(annual.get('goods_count') or 0),
                'extra_service_count': extra_service_count,
            },
            'average_check': {
                'total': revenue / completed if completed else 0.0,
                'source_status': 'ready',
                'missing_components': [],
            },
            'visit_metrics': {
                'unique_clients': unique_clients,
                'visits_per_client': completed / unique_clients if unique_clients else 0.0,
                'opz_qty': float(opz_by_year.get(year, 0.0)),
                'opz_pct': (
                    100.0 * float(opz_by_year.get(year, 0.0)) / completed
                    if completed
                    else 0.0
                ),
            },
        }
        year_row = _year_row_from_summary(
            year,
            period_start,
            period_end,
            summary,
            is_partial_year=bool(period['is_partial_year']),
            is_opening_year=bool(period['is_opening_year']),
            is_latest_year=bool(period['is_latest_year']),
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
            year_row['opz_qty'] = None
            year_row['opz_pct'] = None
        year_rows.append(year_row)
        daily = [
            {
                'date': date(year, month, 1).isoformat(),
                **values,
            }
            for month, values in fact_rows['monthly'].get(year, {}).items()
        ]
        monthly_by_year[year] = _monthly_yoy_rows(
            year,
            daily,
            period_start,
            period_end,
            state_by_key,
            scope_company_ids,
            fact_rows['appointment_dependencies']['monthly'].get(year),
            reporting_windows,
            opz_facts['monthly_counts'].get(year),
            opz_facts['monthly_appointment_dependencies'].get(year),
        )

    year_rows = _with_year_changes(year_rows)
    latest_index = next(
        (
            index
            for index, row in enumerate(year_rows)
            if int(row['year']) == activity_end.year
        ),
        None,
    )
    latest = year_rows[latest_index] if latest_index is not None else {}
    previous = (
        year_rows[latest_index - 1]
        if latest_index is not None and latest_index > 0
        else {}
    )
    months = [f'{month:02d}' for month in range(1, 13)]
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
            'клиент засчитывается один раз в месяц в помесячном ряду и один раз в год '
            'в годовом, поэтому сумма месяцев может превышать год. Ручные добавки ОПЗ '
            'входят в итог филиала и сети и не учитываются при фильтре по сотруднику.'
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
    base['cards'] = [
        _card('Выручка последнего года', latest.get('revenue', 0), MONEY_FORMAT),
        _card('Изменение выручки год к году', latest.get('revenue_change_pct'), PERCENT_FORMAT),
        _card('Визиты последнего года', latest.get('appointments', 0), NUMBER_FORMAT),
        _card('Изменение визитов год к году', latest.get('appointments_change_pct'), PERCENT_FORMAT),
        _card('Средний чек последнего года', latest.get('avg_check', 0), MONEY_FORMAT),
        _card('Клиенты последнего года', latest.get('unique_clients', 0), NUMBER_FORMAT),
        _card('ОПЗ последнего года', latest.get('opz_qty'), NUMBER_FORMAT),
        _card('Изменение ОПЗ год к году', latest.get('opz_qty_change_pct'), PERCENT_FORMAT),
        _card('ОПЗ % последнего года', latest.get('opz_pct'), PERCENT_FORMAT),
    ]
    # The grid is two columns wide, so each metric is a row: by year on the left, its
    # month-by-month year-over-year comparison on the right.
    base['charts'] = [
        _chart(
            'year_revenue',
            'Выручка по годам',
            'bar',
            [str(row['year']) for row in year_rows],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in year_rows], 'format': MONEY_FORMAT}],
        ),
        _chart(
            'monthly_revenue_yoy',
            'Помесячная выручка год к году',
            'line',
            months,
            [
                {
                    'label': str(year),
                    'data': [row['revenue'] for row in monthly_by_year[year]],
                    'format': MONEY_FORMAT,
                    'fill': False,
                }
                for year in sorted(monthly_by_year)
            ],
        ),
        _chart(
            'year_appointments',
            'Визиты по годам',
            'bar',
            [str(row['year']) for row in year_rows],
            [{'label': 'Визиты', 'data': [row['appointments'] for row in year_rows], 'format': NUMBER_FORMAT}],
        ),
        _chart(
            'monthly_appointments_yoy',
            'Помесячные визиты год к году',
            'line',
            months,
            [
                {
                    'label': str(year),
                    'data': [row['appointments'] for row in monthly_by_year[year]],
                    'format': NUMBER_FORMAT,
                    'fill': False,
                }
                for year in sorted(monthly_by_year)
            ],
        ),
        _chart(
            'year_avg_check',
            'Средний чек по годам',
            'bar',
            [str(row['year']) for row in year_rows],
            [{
                'label': 'Средний чек',
                'data': [row['avg_check'] for row in year_rows],
                'format': MONEY_FORMAT,
            }],
        ),
        _chart(
            'monthly_avg_check_yoy',
            'Помесячный средний чек год к году',
            'line',
            months,
            [
                {
                    'label': str(year),
                    'data': [row['avg_check'] for row in monthly_by_year[year]],
                    'format': MONEY_FORMAT,
                    'fill': False,
                }
                for year in sorted(monthly_by_year)
            ],
        ),
        _chart(
            'year_opz',
            'ОПЗ по годам',
            'bar',
            [str(row['year']) for row in year_rows],
            [{'label': 'ОПЗ', 'data': [row['opz_qty'] for row in year_rows], 'format': NUMBER_FORMAT}],
        ),
        _chart(
            'monthly_opz_yoy',
            'Помесячные ОПЗ год к году',
            'line',
            months,
            [
                {
                    'label': str(year),
                    'data': [row['opz_qty'] for row in monthly_by_year[year]],
                    'format': NUMBER_FORMAT,
                    'fill': False,
                }
                for year in sorted(monthly_by_year)
            ],
        ),
        _chart(
            'year_opz_pct',
            'ОПЗ % по годам',
            'bar',
            [str(row['year']) for row in year_rows],
            [{'label': 'ОПЗ %', 'data': [row['opz_pct'] for row in year_rows], 'format': PERCENT_FORMAT}],
        ),
        _chart(
            'monthly_opz_pct_yoy',
            'Помесячный ОПЗ % год к году',
            'line',
            months,
            [
                {
                    'label': str(year),
                    'data': [row['opz_pct'] for row in monthly_by_year[year]],
                    'format': PERCENT_FORMAT,
                    'fill': False,
                }
                for year in sorted(monthly_by_year)
            ],
        ),
    ]
    base['tables'] = [
        _table(
            'years',
            'Годовые агрегаты',
            _without_empty_columns([
                ('year', 'Год', NUMBER_FORMAT),
                ('period_start', 'С', 'date'),
                ('period_end', 'По', 'date'),
                ('period_status', 'Период', 'text'),
                ('source_status', 'Покрытие', 'text'),
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
            ], year_rows, {'topup_revenue'}),
            year_rows,
            row_key='year',
        ),
        _table(
            'months',
            'Помесячные агрегаты',
            _without_empty_columns([
                ('year', 'Год', NUMBER_FORMAT),
                ('month_label', 'Месяц', 'text'),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('appointments', 'Визиты', NUMBER_FORMAT),
                ('avg_check', 'Средний чек', MONEY_FORMAT),
                ('opz_qty', 'ОПЗ', NUMBER_FORMAT),
                ('opz_pct', 'ОПЗ %', PERCENT_FORMAT),
                ('service_revenue', 'Услуги', MONEY_FORMAT),
                ('goods_revenue', 'Товары', MONEY_FORMAT),
                ('topup_revenue', 'Пополнения', MONEY_FORMAT),
            ], monthly_rows, {'topup_revenue'}),
            monthly_rows,
        ),
    ]
    base['raw'] = {
        'years': year_rows,
        'months': monthly_rows,
        'latest_year': latest.get('year'),
        'previous_year': previous.get('year'),
        'activity_start': activity_start.isoformat(),
        'activity_end': activity_end.isoformat(),
        'months_in_scope': months,
        'service_detail_excluded': True,
    }
    return base


async def _payment_methods_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_ids: list[int],
    factual_at: datetime,
) -> dict[str, Any]:
    breakdown = await fetch_payment_breakdown(db, start, end, company_ids, factual_at)
    branches = breakdown['branches']
    multi = len(branches) > 1
    # Revenue includes top-ups, so the coverage verdict is the Overview's, not a new one.
    source_status, missing_components = await _source_coverage_status(
        db, start, end, None, None, company_ids_override=company_ids
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
        base['raw'] = {
            'yandex_pay_applied': breakdown['yandex_pay_applied'],
            'countable_months': breakdown['countable_months'],
            'branches': [],
        }
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
        return round(value / revenue * 100, 2) if value is not None and revenue else None

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
        _card('Выручка', total['revenue'], MONEY_FORMAT),
        _card('Доля наличных', share(total['cash'], total['revenue']), PERCENT_FORMAT),
        _card('Доля безналичных', share(total['cashless'], total['revenue']), PERCENT_FORMAT),
        _card('Доля Яндекс Пэй', share(total['yandex_pay'], total['revenue']), PERCENT_FORMAT),
    ]
    # The network column is last in the chart, first in the tables: bars read branch by branch.
    chart_rows = branches + ([total] if multi else [])
    chart_labels = [row['title'] for row in branches] + (['Все филиалы'] if multi else [])
    base['charts'] = [
        _chart(
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
        _chart(
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
        base['charts'].append(_chart(
            'payment_shares_by_month',
            'Доли форм оплаты по месяцам',
            'line',
            [short_month_label(row['month']) for row in monthly],
            [
                {
                    'label': label,
                    'data': [share(row[key], row['revenue']) for row in monthly],
                    'format': PERCENT_FORMAT,
                    'fill': False,
                }
                for key, label in forms
            ],
            wide=True,
            x_kind='time',
        ))
    table_columns = [('metric', 'Показатель', 'text')]
    # Shares first: the structure is what the report is read for, the roubles back it up.
    # Branch titles are long column headers, so they wrap instead of stretching the table.
    base['tables'] = [
        _table(
            'payment_shares',
            'Доли форм оплаты',
            table_columns + [(key, label, PERCENT_FORMAT) for key, label in columns],
            [metric_row(label, key, True) for key, label in forms],
            wrap_headers=True,
            row_key='metric',
        ),
        _table(
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
        base['notes'].append({
            'kind': 'warning',
            'title': 'Яндекс Пэй не учтён',
            'text': f'{_unapplied_phrases(unapplied, branches, end)} Безналичные показаны целиком, как в YClients.{trend_note}',
        })
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
    partial_note = _data_through_note(branches, end, factual_branch_date(factual_at))
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
    base['raw'] = {
        'yandex_pay_applied': breakdown['yandex_pay_applied'],
        'countable_months': breakdown['countable_months'],
        'branches': branches,
    }
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
            f"{b['title']} (данные по {format_day(date.fromisoformat(b['yandex_pay_unapplied_data_through']), end.year)})"
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


async def _staff_rows(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> list[dict[str, Any]]:
    appt_stmt = (
        select(
            Appointment.staff_id.label('staff_id'),
            func.min(Staff.name).label('staff_name'),
            func.min(Company.title).label('company_title'),
            func.count(func.distinct(Appointment.id)).label('appointments'),
            func.count(
                func.distinct(
                    case((Appointment.attendance == COMPLETED_ATTENDANCE, Appointment.id))
                )
            ).label('completed'),
            func.count(
                func.distinct(
                    case((Appointment.attendance != COMPLETED_ATTENDANCE, Appointment.id))
                )
            ).label('not_completed'),
            func.count(func.distinct(Appointment.client_id)).label('clients'),
        )
        .outerjoin(Staff, Staff.id == Appointment.staff_id)
        .outerjoin(Company, Company.id == Appointment.company_id)
        .where(and_(*_appointment_conditions(
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids=allowed_company_ids,
            factual_at=factual_at,
        )))
        .group_by(Appointment.staff_id)
    )
    rev_stmt = (
        select(
            Appointment.staff_id.label('staff_id'),
            func.min(Staff.name).label('staff_name'),
            func.min(Company.title).label('company_title'),
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
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
        .outerjoin(Staff, Staff.id == Appointment.staff_id)
        .outerjoin(Company, Company.id == Appointment.company_id)
        .where(
            _service_paid_filters(
                start,
                end,
                company_id,
                staff_id,
                allowed_company_ids=allowed_company_ids,
                factual_at=factual_at,
            ),
            _physical_account_condition(),
        )
        .group_by(Appointment.staff_id)
    )
    appt_rows = (await db.execute(appt_stmt)).all()
    rows_by_staff: dict[int | None, dict[str, Any]] = {}
    for row in appt_rows:
        completed = int(row.completed or 0)
        rows_by_staff[row.staff_id] = {
            'staff_id': row.staff_id,
            'staff_name': row.staff_name or f"staff {row.staff_id or '—'}",
            'company_title': row.company_title,
            'appointments': int(row.appointments or 0),
            'completed': completed,
            'not_completed': int(row.not_completed or 0),
            'clients': int(row.clients or 0),
            'revenue': 0.0,
            'avg_check': 0.0,
        }
    for row in (await db.execute(rev_stmt)).all():
        item = rows_by_staff.setdefault(row.staff_id, {
            'staff_id': row.staff_id,
            'staff_name': row.staff_name or f"staff {row.staff_id or '—'}",
            'company_title': row.company_title,
            'appointments': 0,
            'completed': 0,
            'not_completed': 0,
            'clients': 0,
            'revenue': 0.0,
            'avg_check': 0.0,
        })
        item['revenue'] = float(row.revenue or 0)
        item['avg_check'] = (
            item['revenue'] / item['completed'] if item['completed'] else 0.0
        )
    rows = list(rows_by_staff.values())
    # staff_id is a unique final tiebreaker: without it, staff tied on revenue and
    # completed count swap places whenever the query plan changes.
    rows.sort(
        key=lambda item: (
            item['revenue'],
            item['completed'],
            item['staff_id'] if item['staff_id'] is not None else -1,
        ),
        reverse=True,
    )
    return rows


async def _clients_rows(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
    include_lifetime_visits: bool = False,
) -> list[dict[str, Any]]:
    visits_stmt = (
        select(
            Appointment.client_id.label('client_id'),
            func.count(func.distinct(Appointment.id)).label('visits'),
            func.max(Appointment.date).label('last_visit'),
        )
        .where(and_(*_appointment_conditions(
            start,
            end,
            company_id,
            staff_id,
            attended_only=True,
            allowed_company_ids=allowed_company_ids,
            factual_at=factual_at,
        )))
        .group_by(Appointment.client_id)
    )
    revenue_stmt = (
        select(
            Appointment.client_id.label('client_id'),
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
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
        .where(
            _service_paid_filters(
                start,
                end,
                company_id,
                staff_id,
                allowed_company_ids=allowed_company_ids,
                factual_at=factual_at,
            ),
            _physical_account_condition(),
        )
        .group_by(Appointment.client_id)
    )
    # Visit-frequency buckets follow the client's whole history at the branch, so they
    # need a count the period-scoped `visits` cannot give. Only the clients report reads
    # it, so callers that ignore it do not pay for the extra query.
    clients: dict[int | None, dict[str, Any]] = {}
    for row in (await db.execute(visits_stmt)).all():
        client_id = int(row.client_id) if row.client_id is not None else None
        clients[client_id] = {
            'visits': int(row.visits or 0),
            'last_visit': row.last_visit,
            'revenue': 0.0,
            'lifetime_visits': 0 if include_lifetime_visits else None,
        }
    for row in (await db.execute(revenue_stmt)).all():
        client_id = int(row.client_id) if row.client_id is not None else None
        client = clients.setdefault(
            client_id,
            {
                'visits': 0,
                'last_visit': None,
                'revenue': 0.0,
                'lifetime_visits': 0 if include_lifetime_visits else None,
            },
        )
        client['revenue'] = float(row.revenue or 0)
    # Deliberately not bounded by the report's client ids: binding them would put tens of
    # thousands of parameters into one statement and trip asyncpg's 32767 ceiling on a
    # full-history report. The aggregate is grouped in Postgres and cheap. The staff
    # filter is dropped on purpose — it selects clients, it does not shorten their
    # history — and a client who only ever paid keeps a lifetime count of 0.
    if include_lifetime_visits:
        lifetime_stmt = (
            select(
                Appointment.client_id.label('client_id'),
                func.count(func.distinct(Appointment.id)).label('lifetime_visits'),
            )
            .where(and_(*_appointment_conditions(
                None,
                end,
                company_id,
                None,
                attended_only=True,
                allowed_company_ids=allowed_company_ids,
                factual_at=factual_at,
            )))
            .group_by(Appointment.client_id)
        )
        for row in (await db.execute(lifetime_stmt)).all():
            client_id = int(row.client_id) if row.client_id is not None else None
            client = clients.get(client_id)
            if client is not None:
                client['lifetime_visits'] = int(row.lifetime_visits or 0)

    rows = []
    for client_id, client in clients.items():
        revenue = float(client['revenue'])
        visits = int(client['visits'])
        last_visit = client['last_visit']
        as_of_date = min(end, factual_at.date())
        recency = (as_of_date - last_visit).days if last_visit else None
        rows.append({
            'client_id': client_id,
            'visits': visits,
            # None where it was not asked for, so a caller reading it without requesting
            # it cannot silently bucket every client as "0 визитов"
            'lifetime_visits': (
                None if client['lifetime_visits'] is None else int(client['lifetime_visits'])
            ),
            'last_visit': last_visit.isoformat() if last_visit else None,
            'days_since_last_visit': recency,
            'revenue': revenue,
            'avg_check': revenue / visits if visits else 0.0,
        })
    # client_id is a unique final tiebreaker (it can be None for the "no client" bucket,
    # which sorts last): without it, clients tied on revenue swap places whenever the
    # query plan changes.
    rows.sort(
        key=lambda item: (
            item['revenue'],
            item['client_id'] if item['client_id'] is not None else -1,
        ),
        reverse=True,
    )
    return rows


def _client_pareto_rows(
    rows: list[dict[str, Any]],
    total_revenue: float | None = None,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    # client_id as the final tiebreaker only decides which of two equally-ranked clients
    # lands on which side of a decile cut; since they are tied on revenue, every bucket's
    # revenue/clients totals are unaffected either way — this only removes the plan-order
    # dependence of *which* client that is.
    sorted_rows = sorted(
        rows,
        key=lambda item: (
            float(item.get('revenue') or 0),
            item.get('client_id') if item.get('client_id') is not None else -1,
        ),
        reverse=True,
    )
    revenue_denominator = (
        float(total_revenue)
        if total_revenue is not None
        else sum(float(row.get('revenue') or 0) for row in sorted_rows)
    )
    total_clients = len(sorted_rows)
    top_count = max(1, (total_clients + 9) // 10)
    middle_count = max(0, (total_clients * 5 + 9) // 10 - top_count)
    buckets = [
        ('Топ 10% клиентов', sorted_rows[:top_count]),
        ('Следующие 40%', sorted_rows[top_count:top_count + middle_count]),
        ('Остальные 50%', sorted_rows[top_count + middle_count:]),
    ]
    out = []
    for label, bucket_rows in buckets:
        revenue = sum(float(row.get('revenue') or 0) for row in bucket_rows)
        clients = len(bucket_rows)
        out.append({
            'bucket': label,
            'clients': clients,
            'clients_pct': 100.0 * clients / total_clients if total_clients else 0.0,
            'revenue': revenue,
            'revenue_pct': 100.0 * revenue / revenue_denominator if revenue_denominator else 0.0,
            'avg_revenue_per_client': revenue / clients if clients else 0.0,
        })
    return out


async def _client_recency_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
    period_preset: str | None = None,
) -> dict[str, Any]:
    # The Overview's new/repeat cards link straight here, so both must measure against
    # the same baseline. Without the preset this would silently fall back to the plain
    # preceding window and contradict the card the user just clicked.
    summary = await fetch_summary(
        db,
        start,
        end,
        company_id,
        staff_id,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
        period_preset=period_preset,
    )
    visits = summary.get('visit_metrics', {})
    rows = [
        {
            'segment': 'Новые',
            'clients': int(visits.get('new_clients') or 0),
            'share': float(visits.get('new_clients_pct') or 0),
            'clients_change_pct': visits.get('new_clients_change_pct'),
            'share_change_pct': visits.get('new_clients_pct_change_pct'),
        },
        {
            'segment': 'Повторные',
            'clients': int(visits.get('repeat_clients') or 0),
            'share': float(visits.get('repeat_clients_pct') or 0),
            'clients_change_pct': visits.get('repeat_clients_change_pct'),
            'share_change_pct': visits.get('repeat_clients_pct_change_pct'),
        },
    ]
    base['notes'].append({
        'kind': 'formula',
        'title': 'Обезличенный расчет',
        'text': 'Отчет показывает только агрегаты по сегментам клиентов без имен, телефонов и клиентских карточек.',
    })
    base['cards'] = [
        _card('Уникальные клиенты', visits.get('unique_clients', 0), NUMBER_FORMAT),
        _card('Новые клиенты', visits.get('new_clients', 0), NUMBER_FORMAT),
        _card('Доля новых', visits.get('new_clients_pct', 0), PERCENT_FORMAT),
        _card('Повторные клиенты', visits.get('repeat_clients', 0), NUMBER_FORMAT),
        _card('Доля повторных', visits.get('repeat_clients_pct', 0), PERCENT_FORMAT),
        _card('Визитов на клиента', visits.get('visits_per_client', 0), DECIMAL_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'new_repeat_clients',
            'Новые и повторные клиенты',
            'doughnut',
            [row['segment'] for row in rows],
            [{'label': 'Клиентов', 'data': [row['clients'] for row in rows], 'format': NUMBER_FORMAT}],
        )
    ]
    base['tables'] = [
        _table(
            'segments',
            'Сегменты за период',
            [
                ('segment', 'Сегмент', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('share', 'Доля', PERCENT_FORMAT),
                ('clients_change_pct', 'Изменение клиентов', PERCENT_FORMAT),
                ('share_change_pct', 'Изменение доли', PERCENT_FORMAT),
            ],
            rows,
            row_key='segment',
        )
    ]
    base['raw'] = {'segments': rows, 'summary_metrics': visits}
    return base


async def _leaderboard_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    try:
        return await _leaderboard_payload_impl(
            db,
            base,
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids,
            factual_at,
        )
    except Exception as error:  # noqa: BLE001 - mapped to a retryable report error by the route
        traceback.print_exc()
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        stage = getattr(error, 'stage', 'payload')
        print(
            'staff_leaderboard '
            f'status=failed stage={stage} start={start.isoformat()} end={end.isoformat()} '
            f'company_id={company_id} staff_id={staff_id}'
        )
        if isinstance(error, ReportCalculationError):
            raise
        raise ReportCalculationError('staff leaderboard calculation failed', stage=stage) from error
    finally:
        duration_ms = round((time.perf_counter() - started_at) * 1000)
        print(
            'staff_leaderboard '
            f'status=finished start={start.isoformat()} end={end.isoformat()} '
            f'company_id={company_id} staff_id={staff_id} duration_ms={duration_ms}'
        )


async def _leaderboard_payload_impl(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    stage_started = time.perf_counter()
    try:
        plan = await fetch_plan_fact(
            db,
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids=allowed_company_ids,
            force_allowed=allowed_company_ids is not None,
            include_extra_service_revenue=True,
            include_all_staff_in_leaderboards=True,
            factual_at=factual_at,
        )
    except Exception as error:  # noqa: BLE001 - stage is added before route mapping
        raise ReportCalculationError('staff leaderboard plan/fact failed', stage='plan_fact') from error
    print(
        'staff_leaderboard '
        f'status=ready stage=plan_fact start={start.isoformat()} end={end.isoformat()} '
        f'company_id={company_id} staff_id={staff_id} '
        f'duration_ms={round((time.perf_counter() - stage_started) * 1000)}'
    )
    boards = plan.get('staff_leaderboards', {})
    partial_reasons = set(boards.get('_partial_reasons') or [])
    if partial_reasons:
        base['source_status'] = 'partial'
        if 'extra_service_revenue' in partial_reasons:
            base['notes'].append({
                'kind': 'warning',
                'title': 'Часть рейтинга временно недоступна',
                'text': 'Не удалось рассчитать суммы допуслуг. Количество и проценты показаны полностью.',
            })
        if 'staff_schedules' in partial_reasons:
            base['notes'].append({
                'kind': 'warning',
                'title': 'Не все графики администраторов загружены',
                'text': (
                    'Администраторы без полного покрытия выбранного периода исключены '
                    'из рейтинга допуслуг.'
                ),
            })

    staff_col = ('staff', 'Сотрудник', 'text')
    branch_col = ('company_title', 'Барбершоп', 'text')
    qty_col = ('qty', 'Кол-во, шт', NUMBER_FORMAT)
    extra_share_col = ('share_pct', 'Доля доп. услуг по метке, %', PERCENT_FORMAT)
    cosmo_share_col = ('share_pct', 'Доля продаж косметики, %', PERCENT_FORMAT)

    revenue_barber = boards.get('revenue_barber', boards.get('revenue_top', []))
    revenue_admin = boards.get('revenue_admin', [])
    if any(
        boards.get(key)
        for key in (
            'extra_services_admin',
            'opz_admin',
            'revenue_admin',
        )
    ):
        base['notes'].append({
            'kind': 'info',
            'title': 'Область расчета показателей администраторов',
            'text': (
                'Личная выручка и косметика относятся к самому продавцу; '
                'ОПЗ — к записям под ответственностью администратора; '
                'допуслуги — ко всему филиалу во время его смен.'
            ),
        })
    if staff_id is not None:
        selected_revenue = (revenue_barber or revenue_admin)
        base['cards'] = [
            _card(
                'Личная выручка выбранного сотрудника',
                selected_revenue[0]['value'] if selected_revenue else 0,
                MONEY_FORMAT,
            ),
        ]
    else:
        base['cards'] = [
            _card(
                'Топ выручка мастера',
                revenue_barber[0]['value'] if revenue_barber else 0,
                MONEY_FORMAT,
            ),
        ]
    charts = []
    if revenue_barber:
        charts.append(
            _chart(
                'leaderboard_revenue_barber',
                'Топ по выручке — мастера',
                'bar',
                [row['staff'] for row in revenue_barber],
                [{'label': 'Выручка', 'data': [row['value'] for row in revenue_barber], 'format': MONEY_FORMAT}],
            )
        )
    if revenue_admin:
        charts.append(
            _chart(
                'leaderboard_revenue_admin',
                'Топ по личной выручке — администраторы',
                'bar',
                [row['staff'] for row in revenue_admin],
                [{'label': 'Выручка', 'data': [row['value'] for row in revenue_admin], 'format': MONEY_FORMAT}],
            )
        )
    if charts:
        base['charts'] = charts
    base['tables'] = [
        _ranking_table(
            'extra_services',
            'Топ по допуслугам — мастера',
            [staff_col, branch_col, qty_col, ('sum', 'Сумма', MONEY_FORMAT), ('pct', 'Доп. услуги, %', PERCENT_FORMAT), extra_share_col],
            boards.get('extra_services_barber_rankings', boards.get('extra_services_rankings', {})),
            'pct',
            [('qty', 'По количеству'), ('sum', 'По сумме'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        _ranking_table(
            'extra_services_admin',
            'Топ по допуслугам филиала во время смен — администраторы',
            [staff_col, branch_col, qty_col, ('pct', 'Доп. услуги, %', PERCENT_FORMAT)],
            boards.get('extra_services_admin_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        _ranking_table(
            'cosmo_barber',
            'Топ по косметике — мастера',
            [staff_col, branch_col, qty_col, ('sum', 'Сумма', MONEY_FORMAT), ('pct', 'Косметика, %', PERCENT_FORMAT), cosmo_share_col],
            boards.get('cosmo_barber_rankings', {}),
            'sum',
            [('qty', 'По количеству'), ('sum', 'По сумме'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        _ranking_table(
            'cosmo_admin',
            'Топ по косметике — админы',
            [staff_col, branch_col, qty_col, ('sum', 'Сумма', MONEY_FORMAT), ('pct', 'Косметика, %', PERCENT_FORMAT), cosmo_share_col],
            boards.get('cosmo_admin_rankings', {}),
            'sum',
            [('qty', 'По количеству'), ('sum', 'По сумме'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        _ranking_table(
            'opz_barber',
            'Топ по ОПЗ — мастера',
            [staff_col, branch_col, qty_col, ('pct', 'ОПЗ, %', PERCENT_FORMAT)],
            boards.get('opz_barber_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        _ranking_table(
            'opz_admin',
            'Топ по ОПЗ в записях под ответственностью — администраторы',
            [staff_col, branch_col, qty_col, ('pct', 'ОПЗ, %', PERCENT_FORMAT)],
            boards.get('opz_admin_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('pct', 'По проценту')],
            hide_when_empty=True,
        ),
        # Not hide_when_empty: manual review facts are often unfilled, so this
        # top anchors the report (and shows its empty state) even when all
        # other leaderboards are empty for the period.
        _table(
            'reviews_admin',
            'Топ по отзывам — админы',
            [staff_col, branch_col, ('value', 'Отзывы', NUMBER_FORMAT)],
            boards.get('reviews_admin', []),
        ),
        _table(
            'revenue_barber',
            'Топ по выручке — мастера',
            [staff_col, branch_col, ('value', 'Выручка', MONEY_FORMAT)],
            revenue_barber,
            hide_when_empty=True,
        ),
        _table(
            'revenue_admin',
            'Топ по личной выручке — администраторы',
            [staff_col, branch_col, ('value', 'Выручка', MONEY_FORMAT)],
            revenue_admin,
            hide_when_empty=True,
        ),
        _table(
            'avg_check_plan_branch',
            'Топ выполнения плана среднего чека — барбершопы',
            [('staff', 'Барбершоп', 'text'), ('plan', 'План', MONEY_FORMAT), ('fact', 'Факт', MONEY_FORMAT), ('pct', 'Выполнение, %', PERCENT_FORMAT)],
            boards.get('avg_check_plan_branch', []),
            hide_when_empty=True,
        ),
        _table(
            'avg_check_plan_staff',
            'Топ выполнения плана среднего чека — мастера',
            [staff_col, branch_col, ('plan', 'План', MONEY_FORMAT), ('fact', 'Факт', MONEY_FORMAT), ('pct', 'Выполнение, %', PERCENT_FORMAT)],
            boards.get('avg_check_plan_staff', []),
            hide_when_empty=True,
        ),
    ]
    base['raw'] = {}
    return base


