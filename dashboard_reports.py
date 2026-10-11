"""Report catalog and report-data builders for the product dashboard SPA."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import dashboard_service
import reports_clients
import reports_feedback
import reports_finance
import reports_goods
import reports_operations
import reports_services
import reports_team
from dashboard_service import DateRange, _appointment_company_ids
from models import Company, Staff
from plan_config import normalize_staff_category
from report_payload import ReportInputError, ReportRequest

REPORT_GRANULARITIES = {'day', 'week', 'month'}


def _report_now() -> datetime:
    # Reports read the same clock as the rest of the dashboard, through the module so that
    # freezing `dashboard_service.factual_now` reaches reports too.
    return dashboard_service.factual_now()


# Platform-admin only (`can_view_report_usage`) and built by the route with a tenant, unlike every other report.
REPORT_USAGE_ID = 'report_usage'
REPORT_GROUP_ORDER = ('finance', 'services', 'team', 'operations', 'clients', 'goods', 'diagnostics')


ReportBuilder = Callable[[ReportRequest], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ReportDefinition:
    """One catalog entry. `builder` is the report's coroutine function and is not part of the payload."""

    id: str
    title: str
    description: str
    group: str
    themes: tuple[str, ...]
    roles: tuple[str, ...]
    builder: ReportBuilder
    # Whether the payload carries money. A role without the `revenue` metric cannot open such a report
    # or see it in the catalog; unknown ids fail closed the same way.
    requires_financials: bool
    granularity: bool = False
    compare: bool = False
    staff_filter: bool = True
    date_range: bool = True
    aliases: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'group': self.group,
            'themes': list(self.themes),
            'roles': list(self.roles),
            'filters': {
                'date_range': self.date_range,
                'staff': self.staff_filter,
                'granularity': self.granularity,
                'compare': self.compare,
            },
            'aliases': list(self.aliases),
        }


_OWNER_ROLES = ('владельцу', 'управляющему')
_MANAGER_ROLES = ('управляющему', 'владельцу')
_FRONT_DESK_ROLES = ('администратору', 'управляющему')
_PLATFORM_ADMIN_ROLES = ('платформенному администратору',)

async def _refuse_report_usage(req: ReportRequest) -> dict[str, Any]:
    """The report needs a tenant, which a `ReportRequest` does not carry, so the route calls
    `report_usage.fetch_report_usage` directly and the dispatcher must never run it."""
    raise RuntimeError('report_usage is built by fetch_report_usage with an explicit tenant')


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
        themes=('выручка', 'средний чек'),
        roles=_OWNER_ROLES,
        builder=reports_finance.build_financial_overview,
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
        themes=('выручка',),
        roles=_OWNER_ROLES,
        builder=reports_finance.build_payment_methods,
        requires_financials=True,
        # Branch-level totals by payment form have no per-employee reading.
        staff_filter=False,
    ),
    ReportDefinition(
        id='year_over_year',
        title='Сравнение по годам',
        description='Выручка, визиты, средний чек и ОПЗ по годам и по месяцам года к году.',
        group='finance',
        themes=('выручка', 'средний чек'),
        roles=_OWNER_ROLES,
        builder=reports_finance.build_year_over_year,
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
        themes=('услуги', 'средний чек'),
        roles=_OWNER_ROLES,
        builder=reports_services.build_avg_check_by_service,
        requires_financials=True,
        compare=True,
        aliases=('service_trends',),
    ),
    ReportDefinition(
        id='service_staff_profit',
        title='Услуги по мастерам',
        description='Услуги в разрезе мастеров: сколько раз каждый мастер оказал услугу, выручка и средняя цена.',
        group='services',
        themes=('услуги', 'мастера'),
        roles=_OWNER_ROLES,
        builder=reports_services.build_service_staff_profit,
        requires_financials=True,
        compare=True,
        aliases=('staff_services',),
    ),
    ReportDefinition(
        id='service_combos',
        title='Комбинации услуг',
        description='Какие услуги клиенты берут вместе в одном визите и как часто встречается каждая пара.',
        group='services',
        themes=('услуги',),
        roles=_OWNER_ROLES,
        builder=reports_services.build_service_combos,
        requires_financials=False,
        compare=True,
    ),
    ReportDefinition(
        id='staff_efficiency',
        title='Эффективность сотрудников',
        description='Сводка по сотрудникам: завершённые записи, клиенты, выручка услуг и выручка на запись.',
        group='team',
        themes=('мастера',),
        roles=_MANAGER_ROLES,
        builder=reports_operations.build_staff_efficiency,
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
        themes=('мастера',),
        roles=_MANAGER_ROLES,
        builder=reports_team.build_staff_leaderboard,
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
        themes=('записи',),
        roles=_MANAGER_ROLES,
        builder=reports_operations.build_peak_load,
        requires_financials=False,
        compare=True,
        aliases=('staff_time_heatmap',),
    ),
    ReportDefinition(
        id='bookings_dynamics',
        title='Записи и неявки',
        description='Динамика записей по периодам и мастерам: сколько записано, сколько завершено и сколько не пришло.',
        group='operations',
        themes=('записи',),
        roles=_MANAGER_ROLES,
        builder=reports_operations.build_bookings_dynamics,
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
        themes=('записи',),
        roles=_MANAGER_ROLES,
        builder=reports_operations.build_booking_channels,
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
        themes=('клиенты',),
        roles=_FRONT_DESK_ROLES,
        builder=reports_clients.build_new_vs_returning_cross,
        # Counts only, and the Overview links here, so a role without revenue must reach it.
        requires_financials=False,
        compare=True,
    ),
    ReportDefinition(
        id='top_clients_pareto',
        title='Клиентская база: концентрация и частотность',
        description='Какую долю выручки дают лучшие клиенты (принцип Парето) и как часто клиенты возвращаются.',
        group='clients',
        themes=('клиенты', 'выручка'),
        roles=_FRONT_DESK_ROLES,
        builder=reports_clients.build_top_clients_pareto,
        requires_financials=True,
        compare=True,
        aliases=('rfm_analysis', 'client_labels', 'client_journey'),
    ),
    ReportDefinition(
        id='retention_3_6_12',
        title='Возвратность новых клиентов',
        description='Какая доля новых клиентов каждого месяца вернулась в течение 1, 3, 6 и 12 месяцев.',
        group='clients',
        themes=('клиенты',),
        roles=_FRONT_DESK_ROLES,
        builder=reports_clients.build_retention_3_6_12,
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
        themes=('клиенты', 'отток'),
        roles=_FRONT_DESK_ROLES,
        builder=reports_clients.build_revenue_at_risk,
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
        themes=('товары', 'выручка'),
        roles=_OWNER_ROLES,
        builder=reports_goods.build_goods_dynamics,
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
        themes=('клиенты', 'отзывы'),
        roles=_OWNER_ROLES,
        builder=reports_feedback.build_nps_dashboard,
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
        themes=('диагностика',),
        roles=_PLATFORM_ADMIN_ROLES,
        # Dispatched by the route with an explicit tenant (the registry builder refuses to run without one).
        builder=_refuse_report_usage,
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
        if not inspect.iscoroutinefunction(definition.builder):
            raise RuntimeError(f'report {definition.id}: builder must be an async function')
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
        raise ReportInputError(f'unknown report_id: {report_id}')
    if granularity not in REPORT_GRANULARITIES:
        raise ReportInputError('granularity must be one of day, week, month')
    if start > end:
        raise ReportInputError('start_date must be <= end_date')
    if compare_start and compare_end and compare_start > compare_end:
        raise ReportInputError('compare_start_date must be <= compare_end_date')

    factual_at = _report_now()
    # An explicit comparison governs the whole page. Letting the preset drive the deltas
    # inside the tables while the comparison block measures against the window the user
    # ticked would put two different percentages for one metric on one screen — the
    # segments table and the comparison panel of `new_vs_returning_cross` sit together.
    explicit_window = bool(compare_start and compare_end)
    compares = REPORT_REGISTRY[canonical_id].compare
    # The comparison window must come from the preset the page was opened with, before it is dropped below.
    # A report without comparison ignores the request, so it must not fail on a window it will never use.
    cmp_start, cmp_end = (
        (compare_start, compare_end)
        if explicit_window
        else _previous_window(start, end, period_preset)
        if compare_previous and compares
        else (start, end)
    )
    if explicit_window:
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
    if compares and (explicit_window or compare_previous or compare_staff_id is not None):
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
        raise ReportInputError('the period has no previous period to compare with') from exc
    return previous.start, previous.end


def _comparison_payload(data: dict[str, Any], compare_data: dict[str, Any], staff_id: int | None) -> dict[str, Any]:
    """Comparison block: cards matched by label, charts and tables only for ids the current payload has.

    A table without a `row_key` cannot be paired row by row, so it is not carried at all (a list of events
    such as the negative reviews would only ship the previous window's rows for nothing).
    """
    chart_ids = {item.get('id') for item in data.get('charts', [])}
    current_tables = {item.get('id'): item for item in data.get('tables', [])}
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
            {'id': item['id'], 'rows': _comparison_table_rows(current_tables[item['id']], item)}
            for item in compare_data.get('tables', [])
            if item.get('id') in current_tables and item.get('row_key')
        ],
    }


def _comparison_table_rows(current: dict[str, Any], previous: dict[str, Any]) -> list[dict[str, Any]]:
    """Rows of the comparison table that the page can pair with a row of the current one.

    The comparison run of a long-tailed table (`service_combos` returns every pair) carries rows the current window
    does not show; the page looks the current rows up by key and never reads the rest. Tables of consecutive periods
    are paired by position, so they go whole.
    """
    rows = previous.get('rows', [])
    if previous.get('x_kind') == 'time':
        return rows
    row_key = previous['row_key']
    shown = {str(row.get(row_key)) for row in current.get('rows', [])}
    return [row for row in rows if str(row.get(row_key)) in shown]


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
        'source_status': 'ready',
        'missing_sources': [],
        'cards': [],
        'charts': [],
        'tables': [],
        'notes': [],
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
    return await definition.builder(request)


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


