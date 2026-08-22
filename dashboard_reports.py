"""Report catalog and report-data builders for the product dashboard SPA."""

from __future__ import annotations

import traceback
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from plan_config import normalize_staff_category

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
    business_appointment_condition,
    financial_appointment_match_condition,
    fetch_extra_services,
    fetch_appointments_breakdown,
    fetch_opz_year_facts,
    fetch_paid_goods_rows,
    fetch_plan_fact,
    fetch_revenue_daily,
    fetch_summary,
    fetch_reporting_start_dates,
    fetch_staff_service_attribution_status,
    fetch_top_services,
    fetch_year_over_year_facts,
    reporting_start_clause,
)
from models import (
    AccountCatalog,
    Appointment,
    Comment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Staff,
    SyncSourceState,
)

REPORT_GRANULARITIES = {'day', 'week', 'month'}
MONEY_FORMAT = 'money'
NUMBER_FORMAT = 'number'
PERCENT_FORMAT = 'percent'
DECIMAL_FORMAT = 'decimal'


def _report_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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

REPORT_ORDER = (
    'avg_check_by_service',
    'avg_check_dynamics',
    'booking_channels',
    'bookings_dynamics',
    'cancellation_analysis',
    'chair_utilization',
    'churn_dynamics',
    'churn_prediction',
    'client_base_dynamics',
    'client_cohorts',
    'client_journey',
    'client_labels',
    'cohort_ltv_matrix',
    'conversion_funnel',
    'data_audit',
    'day_overview',
    'demographics_check',
    'devices_vs_booking',
    'financial_overview',
    'goal_conversions_report',
    'goods_by_staff',
    'goods_conversion',
    'goods_dynamics',
    'losses_by_staff',
    'lost_clients_list',
    'market_benchmarks',
    'marketing_funnel',
    'master_motivation',
    'mind_index',
    'month_return',
    'new_vs_returning_cross',
    'nps_dashboard',
    'peak_hours_site_vs_salon',
    'peak_load',
    'price_elasticity',
    'retention_3_6_12',
    'retention_opz_dynamics',
    'return_priorities',
    'revenue_at_risk',
    'revenue_decomposition',
    'revenue_dynamics',
    'revenue_factor_analysis',
    'revenue_forecast',
    'rfm_analysis',
    'schedule_optimizer',
    'search_phrases_efficiency',
    'seasonality',
    'service_combos',
    'service_consumption',
    'service_staff_profit',
    'service_trends',
    'staff_efficiency',
    'staff_leaderboard',
    'staff_salary',
    'staff_services',
    'staff_time_heatmap',
    'top_clients_pareto',
    'top_goods_revenue',
    'traffic_source_roi',
    'visit_forecast',
    'year_over_year',
)

READY_REPORTS = {
    'avg_check_by_service',
    'avg_check_dynamics',
    'booking_channels',
    'bookings_dynamics',
    'cancellation_analysis',
    'client_cohorts',
    'client_journey',
    'client_labels',
    'day_overview',
    'financial_overview',
    'goods_by_staff',
    'goods_conversion',
    'goods_dynamics',
    'losses_by_staff',
    'lost_clients_list',
    'new_vs_returning_cross',
    'peak_load',
    'retention_3_6_12',
    'return_priorities',
    'revenue_at_risk',
    'revenue_decomposition',
    'revenue_dynamics',
    'rfm_analysis',
    'service_combos',
    'service_staff_profit',
    'service_trends',
    'staff_efficiency',
    'staff_leaderboard',
    'staff_services',
    'staff_time_heatmap',
    'top_clients_pareto',
    'top_goods_revenue',
    'year_over_year',
}

SOURCE_MISSING_REPORTS = {
    'conversion_funnel',
    'demographics_check',
    'devices_vs_booking',
    'goal_conversions_report',
    'market_benchmarks',
    'peak_hours_site_vs_salon',
    'search_phrases_efficiency',
    'traffic_source_roi',
}

PARTIAL_REPORTS = {'nps_dashboard'}

FINANCE_REPORTS = {
    'avg_check_dynamics',
    'day_overview',
    'financial_overview',
    'revenue_decomposition',
    'revenue_dynamics',
    'booking_channels',
    'year_over_year',
}
BOOKING_REPORTS = {'bookings_dynamics', 'cancellation_analysis', 'peak_load'}
STAFF_REPORTS = {
    'staff_efficiency',
    'staff_leaderboard',
    'staff_services',
    'staff_time_heatmap',
}
LEADERBOARD_REPORTS = {'staff_leaderboard'}
SERVICE_REPORTS = {'avg_check_by_service', 'service_combos', 'service_staff_profit', 'service_trends'}
CLIENT_REPORTS = {
    'client_cohorts',
    'client_journey',
    'client_labels',
    'new_vs_returning_cross',
    'retention_3_6_12',
    'rfm_analysis',
    'top_clients_pareto',
}
CHURN_REPORTS = {'losses_by_staff', 'lost_clients_list', 'return_priorities', 'revenue_at_risk'}
GOODS_REPORTS = {'goods_by_staff', 'goods_conversion', 'goods_dynamics', 'top_goods_revenue'}
MONEY_REPORTS = FINANCE_REPORTS | GOODS_REPORTS | SERVICE_REPORTS | CLIENT_REPORTS | CHURN_REPORTS | STAFF_REPORTS
TITLE_OVERRIDES = {
    'avg_check_by_service': 'Средний чек по услугам',
    'avg_check_dynamics': 'Динамика среднего чека',
    'booking_channels': 'Каналы записи',
    'bookings_dynamics': 'Динамика записей',
    'cancellation_analysis': 'Анализ отмен',
    'chair_utilization': 'Загрузка кресел',
    'churn_dynamics': 'Динамика оттока',
    'churn_prediction': 'Скоринг риска оттока',
    'client_base_dynamics': 'Динамика клиентской базы',
    'client_cohorts': 'Когортный анализ клиентов',
    'client_journey': 'Путь клиента',
    'client_labels': 'Сегменты и метки клиентов',
    'cohort_ltv_matrix': 'Когортная LTV-матрица',
    'conversion_funnel': 'Маркетинговая воронка',
    'data_audit': 'Аудит данных',
    'day_overview': 'Обзор дня',
    'demographics_check': 'Демография: сайт и CRM',
    'devices_vs_booking': 'Устройства и запись',
    'financial_overview': 'Финансовый обзор',
    'goal_conversions_report': 'Конверсии целей',
    'goods_by_staff': 'Товары по мастерам',
    'goods_conversion': 'Конверсия визитов в товары',
    'goods_dynamics': 'Динамика товаров',
    'losses_by_staff': 'Потери клиентов по мастерам',
    'lost_clients_list': 'Потерянные клиенты',
    'market_benchmarks': 'Рыночные бенчмарки',
    'marketing_funnel': 'Воронка новых клиентов',
    'master_motivation': 'Мотивация мастеров',
    'mind_index': 'MInd индекс мастеров',
    'month_return': 'Возвратность месяц к месяцу',
    'new_vs_returning_cross': 'Новые и повторные клиенты',
    'nps_dashboard': 'NPS и отзывы',
    'peak_hours_site_vs_salon': 'Пиковые часы: сайт и салон',
    'peak_load': 'Пиковая загрузка',
    'price_elasticity': 'Эластичность цены',
    'retention_3_6_12': 'Возвратность 3/6/12',
    'retention_opz_dynamics': 'Динамика удержания и ОПЗ',
    'return_priorities': 'Приоритеты возврата',
    'revenue_at_risk': 'Выручка под риском',
    'revenue_decomposition': 'Декомпозиция выручки',
    'revenue_dynamics': 'Динамика выручки',
    'revenue_factor_analysis': 'Факторный анализ выручки',
    'revenue_forecast': 'Прогноз выручки',
    'rfm_analysis': 'RFM-анализ',
    'schedule_optimizer': 'Оптимизатор расписания',
    'search_phrases_efficiency': 'Эффективность поисковых фраз',
    'seasonality': 'Сезонность',
    'service_combos': 'Комбинации услуг',
    'service_consumption': 'Потребление услуг',
    'service_staff_profit': 'Услуги x мастера',
    'service_trends': 'Тренды услуг',
    'staff_efficiency': 'Эффективность сотрудников',
    'staff_leaderboard': 'Рейтинги и топы',
    'staff_salary': 'Зарплата сотрудников',
    'staff_services': 'Услуги по мастерам',
    'staff_time_heatmap': 'Тепловая карта мастеров',
    'top_clients_pareto': 'Клиентская выручка и Парето',
    'top_goods_revenue': 'Топ товаров по выручке',
    'traffic_source_roi': 'ROI источников трафика',
    'visit_forecast': 'Прогноз визитов',
    'year_over_year': 'Сравнение по годам',
}


@dataclass(frozen=True)
class ReportDefinition:
    id: str
    title: str
    description: str
    group: str
    type: str
    themes: tuple[str, ...]
    roles: tuple[str, ...]
    filters: dict[str, bool]
    status: str
    required_sources: tuple[str, ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            'id': self.id,
            'title': self.title,
            'description': self.description,
            'group': self.group,
            'type': self.type,
            'themes': list(self.themes),
            'roles': list(self.roles),
            'filters': self.filters,
            'status': self.status,
            'required_sources': list(self.required_sources),
        }


def _group_for(report_id: str) -> str:
    if report_id in FINANCE_REPORTS:
        return 'finance'
    if report_id in BOOKING_REPORTS:
        return 'operations'
    if report_id in STAFF_REPORTS:
        return 'team'
    if report_id in SERVICE_REPORTS:
        return 'services'
    if report_id in CLIENT_REPORTS:
        return 'clients'
    if report_id in CHURN_REPORTS:
        return 'churn'
    if report_id in GOODS_REPORTS:
        return 'goods'
    if report_id in SOURCE_MISSING_REPORTS:
        return 'marketing'
    if report_id in {'data_audit'}:
        return 'diagnostics'
    return 'advanced'


def report_requires_financials(report_id: str) -> bool:
    normalized = (report_id or '').strip()
    if normalized == 'staff_leaderboard':
        # This mixed report also contains OPZ, review and percentage rankings.
        # Its individual money components are filtered by the route.
        return False
    return (
        normalized in MONEY_REPORTS
        or 'revenue' in normalized
        or 'avg_check' in normalized
        or 'ltv' in normalized
        or 'price' in normalized
        or 'profit' in normalized
    )


def _type_for(group: str) -> str:
    return {
        'finance': 'финансовый',
        'operations': 'операционный',
        'team': 'управленческий',
        'clients': 'клиентский',
        'services': 'операционный',
        'churn': 'клиентский',
        'goods': 'товарный',
        'marketing': 'маркетинговый',
        'plans': 'управленческий',
    }.get(group, 'аналитический')


def _themes_for(report_id: str, group: str) -> tuple[str, ...]:
    themes = set()
    if group == 'finance' or 'revenue' in report_id:
        themes.add('выручка')
    if 'avg_check' in report_id or report_id == 'master_avg_check':
        themes.add('средний чек')
    if group == 'operations' or 'booking' in report_id:
        themes.add('записи')
    if group == 'team' or 'staff' in report_id or 'master' in report_id:
        themes.add('мастера')
    if group in {'clients', 'churn'} or 'client' in report_id:
        themes.add('клиенты')
    if group == 'services' or 'service' in report_id:
        themes.add('услуги')
    if group == 'churn' or 'lost' in report_id or 'risk' in report_id:
        themes.add('отток')
    if group == 'marketing':
        themes.add('маркетинг')
    if group == 'goods':
        themes.add('товары')
    if report_id == 'nps_dashboard':
        themes.add('NPS и отзывы')
    return tuple(sorted(themes)) or ('обзор',)


def _roles_for(group: str) -> tuple[str, ...]:
    if group == 'marketing':
        return ('маркетологу', 'владельцу')
    if group in {'team', 'operations', 'plans'}:
        return ('управляющему', 'владельцу')
    if group in {'clients', 'churn'}:
        return ('администратору', 'управляющему')
    return ('владельцу', 'управляющему')


GRANULARITY_REPORTS = {
    'avg_check_dynamics',
    'booking_channels',
    'bookings_dynamics',
    'financial_overview',
    'goods_conversion',
    'goods_dynamics',
    'revenue_decomposition',
    'revenue_dynamics',
    'service_trends',
}

# Reports where period-over-period comparison is meaningful: time-series and
# headline aggregate KPIs. Rankings, lists, matrices and detail reports omit it.
COMPARE_REPORTS = GRANULARITY_REPORTS | {
    'cancellation_analysis',
    'day_overview',
    'new_vs_returning_cross',
}


def _filters_for(report_id: str, group: str, status: str) -> dict[str, bool]:
    return {
        'date_range': report_id != 'year_over_year',
        'branch': True,
        'staff': group in {'team', 'services', 'clients', 'churn', 'goods', 'operations'} or report_id in READY_REPORTS,
        'granularity': report_id in GRANULARITY_REPORTS,
        'compare': status == 'ready' and report_id in COMPARE_REPORTS,
    }


def _status_for(report_id: str) -> str:
    if report_id in READY_REPORTS:
        return 'ready'
    if report_id in PARTIAL_REPORTS:
        return 'partial'
    if report_id in SOURCE_MISSING_REPORTS:
        return 'source_missing'
    return 'planned'


def _required_sources_for(report_id: str, status: str) -> tuple[str, ...]:
    if status == 'ready':
        return ('yclients',)
    if report_id == 'nps_dashboard':
        return ('yclients_comments', 'telegram_nps')
    if report_id == 'market_benchmarks':
        return ('market_benchmark_data',)
    if status == 'source_missing':
        return ('yandex_metrika',)
    return ('yclients', 'scheduled_report_calculation')


def _build_registry() -> dict[str, ReportDefinition]:
    registry: dict[str, ReportDefinition] = {}
    for report_id in REPORT_ORDER:
        group = _group_for(report_id)
        status = _status_for(report_id)
        title = TITLE_OVERRIDES.get(report_id, report_id.replace('_', ' ').title())
        registry[report_id] = ReportDefinition(
            id=report_id,
            title=title,
            description=_description_for(report_id, status),
            group=group,
            type=_type_for(group),
            themes=_themes_for(report_id, group),
            roles=_roles_for(group),
            filters=_filters_for(report_id, group, status),
            status=status,
            required_sources=_required_sources_for(report_id, status),
        )
    return registry


def _description_for(report_id: str, status: str) -> str:
    if status == 'source_missing':
        return 'Отчет появится после подключения внешнего источника данных.'
    if status == 'planned':
        return 'Отчет включен в каталог и ожидает отдельной методологии расчета.'
    if report_id == 'nps_dashboard':
        return 'Отзывы YClients доступны сейчас; NPS-опросы требуют отдельного источника.'
    if report_id == 'year_over_year':
        return 'Год к году по выручке, визитам, среднему чеку и крупным агрегатам без среза по услугам.'
    return 'Отчет строится на текущих данных YClients в PostgreSQL.'


REPORT_REGISTRY = _build_registry()


# The demo tenant is seeded, not synced: it holds a few months of activity and no
# SyncSourceState coverage at all. year_over_year certifies whole years against
# that coverage, so for demo it can only ever render every metric as unknown.
DEMO_UNAVAILABLE_REPORTS = frozenset({'year_over_year'})


def fetch_report_registry(is_demo: bool = False) -> list[dict[str, Any]]:
    return [
        REPORT_REGISTRY[report_id].to_payload()
        for report_id in REPORT_ORDER
        if not (is_demo and report_id in DEMO_UNAVAILABLE_REPORTS)
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
) -> dict[str, Any]:
    if report_id not in REPORT_REGISTRY:
        raise ValueError(f'unknown report_id: {report_id}')
    if granularity not in REPORT_GRANULARITIES:
        raise ValueError('granularity must be one of day, week, month')
    if start > end:
        raise ValueError('start_date must be <= end_date')
    if compare_start and compare_end and compare_start > compare_end:
        raise ValueError('compare_start_date must be <= compare_end_date')

    factual_at = _report_now()
    data = await _fetch_report_payload(
        db,
        report_id,
        start,
        end,
        company_id,
        staff_id,
        granularity,
        allowed_company_ids,
        factual_at,
    )
    if (
        report_id in COMPARE_REPORTS
        and data['source_status'] == 'ready'
        and ((compare_start and compare_end) or compare_staff_id)
    ):
        cmp_start = compare_start or start
        cmp_end = compare_end or end
        cmp_staff_id = compare_staff_id if compare_staff_id is not None else staff_id
        compare_data = await _fetch_report_payload(
            db,
            report_id,
            cmp_start,
            cmp_end,
            company_id,
            cmp_staff_id,
            granularity,
            allowed_company_ids,
            factual_at,
        )
        data['comparison'] = {
            'period': compare_data['period'],
            'staff_id': cmp_staff_id,
            'source_status': compare_data['source_status'],
            'cards': compare_data.get('cards', []),
            'rows': _comparison_rows(data.get('cards', []), compare_data.get('cards', [])),
            'raw': compare_data.get('raw', {}),
        }
    return data


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
) -> dict[str, Any]:
    definition = REPORT_REGISTRY[report_id]
    base = {
        'report_id': definition.id,
        'title': definition.title,
        'period': {'start': start.isoformat(), 'end': end.isoformat(), 'granularity': granularity},
        'source_status': definition.status if definition.status != 'source_missing' else 'missing',
        'missing_sources': [],
        'cards': [],
        'charts': [],
        'tables': [],
        'notes': [],
        'raw': {},
    }
    if definition.status == 'source_missing':
        return _missing_payload(base, definition)
    if definition.status == 'planned':
        return _planned_payload(base, definition)
    allowed_company_ids = await _appointment_company_ids(
        db, company_id, staff_id, allowed_company_ids
    )
    if report_id == 'nps_dashboard':
        return await _nps_payload(db, base, definition, start, end, company_id, allowed_company_ids)
    if report_id in GOODS_REPORTS:
        return await _goods_payload(
            db,
            base,
            start,
            end,
            company_id,
            staff_id,
            granularity,
            allowed_company_ids,
            factual_at,
        )
    if report_id in LEADERBOARD_REPORTS:
        return await _leaderboard_payload(
            db,
            base,
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids,
            factual_at,
        )
    if report_id in STAFF_REPORTS:
        return await _staff_payload(
            db, base, start, end, company_id, staff_id, allowed_company_ids, factual_at
        )
    if report_id in SERVICE_REPORTS:
        return await _services_payload(
            db,
            base,
            start,
            end,
            company_id,
            staff_id,
            granularity,
            allowed_company_ids,
            factual_at,
        )
    if report_id == 'new_vs_returning_cross':
        return await _client_recency_payload(
            db, base, start, end, company_id, staff_id, allowed_company_ids, factual_at
        )
    if report_id in CLIENT_REPORTS:
        return await _clients_payload(
            db, base, start, end, company_id, staff_id, allowed_company_ids, factual_at
        )
    if report_id in CHURN_REPORTS:
        return await _churn_payload(
            db, base, start, end, company_id, staff_id, allowed_company_ids, factual_at
        )
    if report_id in BOOKING_REPORTS:
        return await _operations_payload(
            db,
            base,
            start,
            end,
            company_id,
            staff_id,
            granularity,
            allowed_company_ids,
            factual_at,
        )
    if report_id == 'year_over_year':
        return await _year_over_year_payload(
            db, base, company_id, staff_id, allowed_company_ids, factual_at
        )
    return await _financial_payload(
        db,
        base,
        start,
        end,
        company_id,
        staff_id,
        granularity,
        allowed_company_ids,
        factual_at,
    )


def _missing_payload(base: dict[str, Any], definition: ReportDefinition) -> dict[str, Any]:
    base['missing_sources'] = list(definition.required_sources)
    base['notes'].append({
        'kind': 'missing',
        'title': 'Источник данных не подключен',
        'text': 'Карточка отчета доступна в каталоге, но для расчета нужен внешний источник.',
    })
    return base


def _planned_payload(base: dict[str, Any], definition: ReportDefinition) -> dict[str, Any]:
    base['missing_sources'] = list(definition.required_sources)
    base['notes'].append({
        'kind': 'planned',
        'title': 'Отчет запланирован',
        'text': 'Для этого отчета нужен отдельный расчет или уточнение методологии. Контракт API уже стабилен.',
    })
    return base


def _card(label: str, value: Any, fmt: str = NUMBER_FORMAT) -> dict[str, Any]:
    return {'label': label, 'value': value, 'format': fmt}


def _chart(
    chart_id: str,
    title: str,
    chart_type: str,
    labels: list[str],
    datasets: list[dict[str, Any]],
) -> dict[str, Any]:
    return {'id': chart_id, 'title': title, 'type': chart_type, 'labels': labels, 'datasets': datasets}


def _table(
    table_id: str,
    title: str,
    columns: list[tuple[str, str, str]],
    rows: list[dict[str, Any]],
    *,
    hide_when_empty: bool = False,
) -> dict[str, Any]:
    table = {
        'id': table_id,
        'title': title,
        'columns': [{'key': key, 'label': label, 'format': fmt} for key, label, fmt in columns],
        'rows': rows,
    }
    if hide_when_empty:
        table['hide_when_empty'] = True
    return table


def _ranking_table(
    table_id: str,
    title: str,
    columns: list[tuple[str, str, str]],
    rows_by_metric: dict[str, list[dict[str, Any]]],
    default_metric: str,
    options: list[tuple[str, str]],
    *,
    hide_when_empty: bool = False,
) -> dict[str, Any]:
    table = _table(
        table_id, title, columns, rows_by_metric.get(default_metric, []), hide_when_empty=hide_when_empty
    )
    table['ranking'] = {
        'default_metric': default_metric,
        'options': [{'key': key, 'label': label} for key, label in options],
        'rows_by_metric': rows_by_metric,
    }
    return table


def _without_empty_columns(
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


def _appointment_conditions(
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    *,
    attended_only: bool = False,
    allowed_company_ids: list[int] | None = None,
    factual_at: datetime | None = None,
) -> list[Any]:
    conditions = [
        Appointment.date >= start,
        Appointment.date <= end,
        business_appointment_condition(),
        reporting_start_clause(Appointment.company_id, Appointment.date),
    ]
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
            delta_pct = _pct_change(float(current_value), float(compare_value))
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
    created_user_id: int | None = None,
) -> tuple[
    date,
    date,
    dict[int, float],
    dict[int, dict[int, tuple[date, date]]],
] | None:
    """Return the first YClients record and the latest factual metric activity.

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
        reporting_start_clause(Appointment.company_id, Appointment.date),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        appointment_conditions.append(scope)
    if created_user_id is not None:
        appointment_conditions.append(Appointment.created_user_id == created_user_id)
    elif staff_id is not None:
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
        reporting_start_clause(Appointment.company_id, Appointment.date),
        reporting_start_clause(FinancialTransaction.company_id, payment_day),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        service_conditions.append(scope)
    if created_user_id is not None:
        service_conditions.append(Appointment.created_user_id == created_user_id)
    elif staff_id is not None:
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
    if staff_id is not None and created_user_id is None:
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
        reporting_start_clause(FinancialTransaction.company_id, payment_day),
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
        reporting_start_clause(GoodTransaction.company_id, goods_day),
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
        created_user_id=created_user_id,
    )
    if opz_facts['latest_date'] is not None:
        ends.append(opz_facts['latest_date'])
    return (
        activity_start,
        max(ends),
        opz_facts['counts'],
        opz_facts['appointment_dependencies'],
    )


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
    reporting_starts: dict[int, date] | None = None,
) -> list[str]:
    missing = set()
    for item_company_id in scope_company_ids:
        # A branch contributes no facts before its reporting start, so demanding sync
        # coverage from earlier would blank a year the branch simply predates — and a
        # branch that did not exist yet has no coverage to demand at all.
        # Unlike _source_coverage_status, which answers for a user-chosen range and calls
        # a fully predating period uncertifiable, the years here are derived from already
        # trimmed facts — a year every branch predates never reaches this function.
        branch_start = (reporting_starts or {}).get(item_company_id)
        if branch_start is not None and branch_start > period_end:
            continue
        branch_period_start = (
            max(period_start, branch_start) if branch_start is not None else period_start
        )
        for source in required_sources:
            state = state_by_key.get((item_company_id, source))
            required_start = branch_period_start
            required_end = period_end
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


async def _year_over_year_created_user_id(
    db: AsyncSession,
    staff_id: int | None,
) -> int | None:
    if staff_id is None:
        return None
    staff = (
        await db.execute(
            select(Staff.position, Staff.user_id)
            .where(Staff.id == staff_id)
            .limit(1)
        )
    ).one_or_none()
    if (
        staff is not None
        and normalize_staff_category(staff.position) == 'administrator'
        and staff.user_id is not None
    ):
        return int(staff.user_id)
    return None


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
    reporting_starts: dict[int, date] | None = None,
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
                reporting_starts,
            )
            if in_activity_period
            else []
        )
        appointments_known = 'appointments_detail' not in missing_sources
        financials_known = 'financial_transactions_detail' not in missing_sources
        revenue_known = appointments_known and financials_known
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
            'in_activity_period': in_activity_period,
            'source_status': 'ready' if in_activity_period and not missing_sources else 'partial',
            'missing_components': missing_sources,
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
    created_user_id = await _year_over_year_created_user_id(db, staff_id)
    scope_company_ids = await _appointment_company_ids(
        db, company_id, staff_id, allowed_company_ids
    )
    activity_bounds = await _year_over_year_activity_bounds(
        db,
        company_id,
        staff_id,
        scope_company_ids,
        report_now,
        created_user_id,
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

    activity_start, activity_end, opz_by_year, opz_dependencies_by_year = activity_bounds
    report_end = report_now.date()
    periods = _year_periods(
        activity_start,
        report_end,
        report_end,
        latest_fact_year=activity_end.year,
    )
    state_by_key = await _year_over_year_source_states(db, scope_company_ids)
    reporting_starts = await fetch_reporting_start_dates(db, scope_company_ids)
    fact_rows = await fetch_year_over_year_facts(
        db,
        activity_start,
        report_end,
        company_id,
        staff_id,
        scope_company_ids,
        factual_at=report_now,
        created_user_id=created_user_id,
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
        attribution = await fetch_staff_service_attribution_status(
            db,
            period_start,
            period_end,
            staff_id,
            scope_company_ids,
            report_now,
        )
        attribution_missing = list(attribution.get('missing_sources') or [])
        if attribution.get('mode') == 'administrator_schedule':
            if attribution.get('source_status') == 'ready':
                attributed_extra_rows = await fetch_extra_services(
                    db,
                    period_start,
                    period_end,
                    company_id,
                    None,
                    staff_id,
                    allowed_company_ids=scope_company_ids,
                    factual_at=report_now,
                )
                extra_service_count: float | None = sum(
                    float(row.get('sold') or 0) for row in attributed_extra_rows
                )
                extra_service_revenue: float | None = sum(
                    float(row.get('revenue') or 0) for row in attributed_extra_rows
                )
            else:
                extra_service_count = None
                extra_service_revenue = None
        else:
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
            reporting_starts=reporting_starts,
        )
        opz_missing = _year_over_year_missing_sources(
            state_by_key,
            scope_company_ids,
            period_start,
            period_end,
            required_sources=('appointments_detail',),
            appointment_dependencies=opz_dependencies_by_year.get(year),
            reporting_starts=reporting_starts,
        )
        year_row['missing_components'] = sorted({
            *year_row['missing_components'],
            *technical_missing,
            *opz_missing,
            *attribution_missing,
        })
        if year_row['missing_components']:
            year_row['source_status'] = 'partial'
        _mask_unknown_year_metrics(year_row, [*technical_missing, *attribution_missing])
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
            reporting_starts,
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
            'показываются за их фактический период.'
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
    ]
    base['charts'] = [
        _chart(
            'year_revenue',
            'Выручка по годам',
            'bar',
            [str(row['year']) for row in year_rows],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in year_rows], 'format': MONEY_FORMAT}],
        ),
        _chart(
            'year_appointments',
            'Визиты по годам',
            'bar',
            [str(row['year']) for row in year_rows],
            [{'label': 'Визиты', 'data': [row['appointments'] for row in year_rows], 'format': NUMBER_FORMAT}],
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
                ('opz_pct', 'ОПЗ %', PERCENT_FORMAT),
            ], year_rows, {'topup_revenue'}),
            year_rows,
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


async def _financial_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    granularity: str,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    summary = await fetch_summary(
        db,
        start,
        end,
        company_id,
        staff_id,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
    )
    daily = _aggregate_daily(
        await fetch_revenue_daily(
            db,
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids=allowed_company_ids,
            factual_at=factual_at,
        ),
        granularity,
    )
    services = await fetch_top_services(
        db,
        start,
        end,
        company_id,
        15,
        staff_id,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
    )
    revenue = summary.get('revenue', {})
    avg = summary.get('average_check', {})
    visits = summary.get('visit_metrics', {})
    base['average_check_source_status'] = avg.get('source_status')
    base['missing_sources'] = sorted({
        *(avg.get('missing_components') or []),
        *(summary.get('missing_sources') or []),
    })
    if summary.get('source_status') == 'partial':
        base['source_status'] = 'partial'
    base['notes'].append({
        'kind': 'formula',
        'title': 'Средний чек общий',
        'text': avg.get('formula'),
    })
    base['cards'] = [
        _card('Выручка', revenue.get('total', 0), MONEY_FORMAT),
        _card('Услуги', revenue.get('service_revenue', 0), MONEY_FORMAT),
        _card('Товары', revenue.get('goods_revenue', 0), MONEY_FORMAT),
        _card('Пополнения', revenue.get('topup_revenue', 0), MONEY_FORMAT),
        _card('Записи', revenue.get('appointments', 0), NUMBER_FORMAT),
        _card('Средний чек', avg.get('total', 0), MONEY_FORMAT),
        _card('Уникальные клиенты', visits.get('unique_clients', 0), NUMBER_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'revenue_periods',
            'Выручка и записи',
            'line',
            [row['period'] for row in daily],
            [
                {'label': 'Выручка', 'data': [row['revenue'] for row in daily], 'format': MONEY_FORMAT},
                {'label': 'Записи', 'data': [row['appointments'] for row in daily], 'format': NUMBER_FORMAT, 'axis': 'y1'},
            ],
        ),
        _chart(
            'top_services',
            'Топ услуг по выручке',
            'bar',
            [row.get('title') or row.get('service_title') or 'Услуга' for row in services[:10]],
            [{'label': 'Выручка', 'data': [row.get('revenue', 0) for row in services[:10]], 'format': MONEY_FORMAT}],
        ),
    ]
    base['tables'] = [
        _table(
            'periods',
            'Динамика по периодам',
            [
                ('period', 'Период', 'text'),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('appointments', 'Записи', NUMBER_FORMAT),
                ('service_revenue', 'Услуги', MONEY_FORMAT),
                ('goods_revenue', 'Товары', MONEY_FORMAT),
                ('topup_revenue', 'Пополнения', MONEY_FORMAT),
            ],
            daily,
        ),
        _services_table('top_services', 'Услуги', services),
    ]
    base['raw'] = {
        'summary': summary,
        'average_check': avg,
        'daily': daily,
        'top_services': services,
    }
    return base


def _services_table(table_id: str, title: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    return _table(
        table_id,
        title,
        [
            ('title', 'Услуга', 'text'),
            ('sold', 'Кол-во', NUMBER_FORMAT),
            ('revenue', 'Выручка', MONEY_FORMAT),
            ('branch_count', 'Филиалов', NUMBER_FORMAT),
        ],
        [
            {
                'title': row.get('title') or row.get('service_title') or f"Услуга {row.get('service_id') or ''}",
                'sold': row.get('sold', 0),
                'revenue': row.get('revenue', 0),
                'branch_count': row.get('branch_count', 0),
            }
            for row in rows
        ],
    )


async def _services_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    granularity: str,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    all_services = await fetch_top_services(
        db,
        start,
        end,
        company_id,
        None,
        staff_id,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
    )
    all_extra = await fetch_extra_services(
        db,
        start,
        end,
        company_id,
        None,
        staff_id,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
    )
    summary = await fetch_summary(
        db,
        start,
        end,
        company_id,
        staff_id,
        include_appointments_breakdown=False,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
    )
    revenue = summary.get('revenue', {})
    attribution = summary.get('service_attribution', {})
    administrator_scope = attribution.get('mode') == 'administrator_schedule'
    if administrator_scope:
        total_revenue = sum(float(row.get('revenue') or 0) for row in all_services)
        total_sold = sum(float(row.get('sold') or 0) for row in all_services)
    else:
        total_revenue = float(revenue.get('service_revenue') or 0)
        total_sold = float(revenue.get('service_count') or 0)
    if attribution.get('source_status') == 'partial':
        base['source_status'] = 'partial'
        base['missing_sources'] = sorted({
            *base.get('missing_sources', []),
            *summary.get('missing_sources', []),
        })
    services = all_services[:25]
    extra = all_extra[:25]
    base['cards'] = [
        _card('Услуг оказано', total_sold, NUMBER_FORMAT),
        _card('Выручка услуг', total_revenue, MONEY_FORMAT),
        _card('Уникальных услуг', len(all_services), NUMBER_FORMAT),
        _card('Доп. услуг в списке', len(all_extra), NUMBER_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'service_revenue',
            'Услуги по выручке',
            'bar',
            [row.get('title') or 'Услуга' for row in services[:12]],
            [{'label': 'Выручка', 'data': [row.get('revenue', 0) for row in services[:12]], 'format': MONEY_FORMAT}],
        )
    ]
    if extra:
        base['charts'].append(
            _chart(
                'extra_services',
                'Доп. услуги',
                'bar',
                [row.get('title') or 'Доп. услуга' for row in extra[:12]],
                [{'label': 'Оказано', 'data': [row.get('sold', 0) for row in extra[:12]], 'format': NUMBER_FORMAT}],
            )
        )
    base['tables'] = [
        _services_table('services', 'Услуги', services),
        _services_table('extra_services', 'Дополнительные услуги', extra),
    ]
    base['raw'] = {
        'services': services,
        'extra_services': extra,
        'granularity': granularity,
        'service_attribution': attribution,
    }
    return base


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
    rows.sort(key=lambda item: (item['revenue'], item['completed']), reverse=True)
    return rows


async def _staff_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    rows = await _staff_rows(
        db, start, end, company_id, staff_id, allowed_company_ids, factual_at
    )
    total_revenue = sum(row['revenue'] for row in rows)
    total_completed = sum(row['completed'] for row in rows)
    base['notes'].append({
        'kind': 'formula',
        'title': 'Выручка услуг по сотрудникам',
        'text': (
            'Разрез включает физические оплаты услуг по дате платежа, как в Обзоре '
            'и План/факт; товары и пополнения показаны отдельно.'
        ),
    })
    base['cards'] = [
        _card('Сотрудников в отчете', len(rows), NUMBER_FORMAT),
        _card('Завершено записей', total_completed, NUMBER_FORMAT),
        _card('Выручка услуг', total_revenue, MONEY_FORMAT),
        _card('Выручка услуг / завершенная запись', total_revenue / total_completed if total_completed else 0, MONEY_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'staff_revenue',
            'Выручка услуг по сотрудникам',
            'bar',
            [row['staff_name'] for row in rows[:12]],
            [{'label': 'Выручка услуг', 'data': [row['revenue'] for row in rows[:12]], 'format': MONEY_FORMAT}],
        ),
        _chart(
            'staff_completed',
            'Завершенные записи',
            'bar',
            [row['staff_name'] for row in rows[:12]],
            [{'label': 'Записи', 'data': [row['completed'] for row in rows[:12]], 'format': NUMBER_FORMAT}],
        ),
    ]
    base['tables'] = [
        _table(
            'staff',
            'Сотрудники',
            [
                ('staff_name', 'Сотрудник', 'text'),
                ('company_title', 'Филиал', 'text'),
                ('completed', 'Завершено', NUMBER_FORMAT),
                ('appointments', 'Доступные записи', NUMBER_FORMAT),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('revenue', 'Выручка услуг', MONEY_FORMAT),
                ('avg_check', 'Выручка услуг / завершенная запись', MONEY_FORMAT),
            ],
            rows,
        )
    ]
    base['raw'] = {'staff': rows, 'revenue_scope': 'services'}
    return base


async def _clients_rows(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
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
    clients: dict[int | None, dict[str, Any]] = {}
    for row in (await db.execute(visits_stmt)).all():
        client_id = int(row.client_id) if row.client_id is not None else None
        clients[client_id] = {
            'visits': int(row.visits or 0),
            'last_visit': row.last_visit,
            'revenue': 0.0,
        }
    for row in (await db.execute(revenue_stmt)).all():
        client_id = int(row.client_id) if row.client_id is not None else None
        client = clients.setdefault(
            client_id,
            {'visits': 0, 'last_visit': None, 'revenue': 0.0},
        )
        client['revenue'] = float(row.revenue or 0)

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
            'last_visit': last_visit.isoformat() if last_visit else None,
            'days_since_last_visit': recency,
            'revenue': revenue,
            'avg_check': revenue / visits if visits else 0.0,
        })
    rows.sort(key=lambda item: item['revenue'], reverse=True)
    return rows


def _client_segment_rows(rows: list[dict[str, Any]], avg_revenue: float) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        'segment': '',
        'clients': 0,
        'visits': 0,
        'revenue': 0.0,
    })
    for row in rows:
        segment = _segment_client(row, avg_revenue)
        row['segment'] = segment
        bucket = grouped[segment]
        bucket['segment'] = segment
        bucket['clients'] += 1
        bucket['visits'] += int(row.get('visits') or 0)
        bucket['revenue'] += float(row.get('revenue') or 0)
    out = []
    for item in grouped.values():
        clients = int(item['clients'] or 0)
        visits = int(item['visits'] or 0)
        revenue = float(item['revenue'] or 0)
        item['avg_revenue_per_client'] = revenue / clients if clients else 0.0
        item['avg_visits_per_client'] = visits / clients if clients else 0.0
        out.append(item)
    return sorted(out, key=lambda item: item['clients'], reverse=True)


def _client_visit_frequency_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets = {
        '0 визитов': {'bucket': '0 визитов', 'clients': 0, 'revenue': 0.0},
        '1 визит': {'bucket': '1 визит', 'clients': 0, 'revenue': 0.0},
        '2-3 визита': {'bucket': '2-3 визита', 'clients': 0, 'revenue': 0.0},
        '4+ визита': {'bucket': '4+ визита', 'clients': 0, 'revenue': 0.0},
    }
    for row in rows:
        visits = int(row.get('visits') or 0)
        if visits == 0:
            key = '0 визитов'
        elif visits == 1:
            key = '1 визит'
        elif visits <= 3:
            key = '2-3 визита'
        else:
            key = '4+ визита'
        buckets[key]['clients'] += 1
        buckets[key]['revenue'] += float(row.get('revenue') or 0)
    total_clients = len(rows)
    out = []
    for item in buckets.values():
        clients = int(item['clients'] or 0)
        item['clients_pct'] = 100.0 * clients / total_clients if total_clients else 0.0
        out.append(item)
    return out


def _client_pareto_rows(
    rows: list[dict[str, Any]],
    total_revenue: float | None = None,
) -> list[dict[str, Any]]:
    if not rows:
        return []
    sorted_rows = sorted(rows, key=lambda item: float(item.get('revenue') or 0), reverse=True)
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


def _segment_client(row: dict[str, Any], avg_revenue: float) -> str:
    recency = row.get('days_since_last_visit')
    visits = int(row.get('visits') or 0)
    revenue = float(row.get('revenue') or 0)
    if recency is None:
        return 'Без визитов'
    if recency <= 45 and visits >= 3 and revenue >= avg_revenue:
        return 'Чемпионы'
    if recency <= 60:
        return 'Активные'
    if recency <= 120:
        return 'Под риском'
    return 'Потерянные'


async def _client_recency_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    summary = await fetch_summary(
        db,
        start,
        end,
        company_id,
        staff_id,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
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
        )
    ]
    base['raw'] = {'segments': rows, 'summary_metrics': visits}
    return base


async def _clients_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    rows = await _clients_rows(
        db, start, end, company_id, staff_id, allowed_company_ids, factual_at
    )
    total_revenue = sum(row['revenue'] for row in rows)
    total_visits = sum(row['visits'] for row in rows)
    identified_rows = [row for row in rows if row.get('client_id') is not None]
    anonymous_rows = [row for row in rows if row.get('client_id') is None]
    identified_revenue = sum(row['revenue'] for row in identified_rows)
    avg_revenue = identified_revenue / len(identified_rows) if identified_rows else 0.0
    segment_rows = _client_segment_rows(identified_rows, avg_revenue)
    frequency_rows = _client_visit_frequency_rows(identified_rows)
    pareto_rows = _client_pareto_rows(identified_rows, total_revenue)
    anonymous_visits = sum(row['visits'] for row in anonymous_rows)
    anonymous_revenue = sum(row['revenue'] for row in anonymous_rows)
    if anonymous_visits or anonymous_revenue:
        segment_rows.append({
            'segment': 'Без клиента',
            'clients': 0,
            'visits': anonymous_visits,
            'revenue': anonymous_revenue,
            'avg_revenue_per_client': 0.0,
            'avg_visits_per_client': 0.0,
        })
        frequency_rows.append({
            'bucket': 'Без клиента',
            'clients': 0,
            'revenue': anonymous_revenue,
            'clients_pct': 0.0,
        })
        pareto_rows.append({
            'bucket': 'Без клиента',
            'clients': 0,
            'clients_pct': 0.0,
            'revenue': anonymous_revenue,
            'revenue_pct': 100.0 * anonymous_revenue / total_revenue if total_revenue else 0.0,
            'avg_revenue_per_client': 0.0,
        })
    base['cards'] = [
        _card('Клиентов', len(identified_rows), NUMBER_FORMAT),
        _card('Визитов', total_visits, NUMBER_FORMAT),
        _card('Выручка клиентов', total_revenue, MONEY_FORMAT),
        _card('Средний доход на клиента', avg_revenue, MONEY_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'client_segments',
            'Сегменты клиентов',
            'doughnut',
            [row['segment'] for row in segment_rows],
            [{'label': 'Клиентов', 'data': [row['clients'] for row in segment_rows], 'format': NUMBER_FORMAT}],
        ),
        _chart(
            'client_pareto',
            'Концентрация выручки по клиентским бакетам',
            'bar',
            [row['bucket'] for row in pareto_rows],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in pareto_rows], 'format': MONEY_FORMAT}],
        ),
    ]
    base['tables'] = [
        _table(
            'client_segments',
            'Сегменты клиентов',
            [
                ('segment', 'Сегмент', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('visits', 'Визиты', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('avg_revenue_per_client', 'Доход на клиента', MONEY_FORMAT),
                ('avg_visits_per_client', 'Визитов на клиента', DECIMAL_FORMAT),
            ],
            segment_rows,
        ),
        _table(
            'client_pareto',
            'Pareto-бакеты клиентов',
            [
                ('bucket', 'Бакет', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('clients_pct', 'Доля клиентов', PERCENT_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('revenue_pct', 'Доля выручки', PERCENT_FORMAT),
                ('avg_revenue_per_client', 'Доход на клиента', MONEY_FORMAT),
            ],
            pareto_rows,
        ),
        _table(
            'visit_frequency',
            'Частотность визитов',
            [
                ('bucket', 'Частотность', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('clients_pct', 'Доля клиентов', PERCENT_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
            ],
            frequency_rows,
        ),
    ]
    base['notes'].append({
        'kind': 'formula',
        'title': 'Выручка услуг без персональных данных',
        'text': (
            'Клиентская выручка считается по дате физической оплаты услуг, как в Обзоре '
            'и План/факт; отчет показывает только агрегированные сегменты.'
        ),
    })
    base['raw'] = {
        'segments': segment_rows,
        'pareto': pareto_rows,
        'visit_frequency': frequency_rows,
        'anonymous_residual': {
            'visits': anonymous_visits,
            'revenue': anonymous_revenue,
        },
        'revenue_scope': 'services',
    }
    return base


async def _last_staff_by_client(
    db: AsyncSession,
    client_ids: list[int],
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[int, dict[str, Any]]:
    if not client_ids:
        return {}
    conditions = [
        Appointment.client_id.in_(client_ids),
        Appointment.attendance == COMPLETED_ATTENDANCE,
        _appointment_factual_at_condition(factual_at),
        # This query has no date floor of its own, so it guards itself: today's callers
        # only pass clients that already have a reportable visit, but nothing enforces it.
        reporting_start_clause(Appointment.company_id, Appointment.date),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        conditions.append(scope)
    if staff_id is not None:
        conditions.append(Appointment.staff_id == staff_id)
    stmt = (
        select(Appointment.client_id, Appointment.staff_id, Staff.name, Appointment.date)
        .outerjoin(Staff, Staff.id == Appointment.staff_id)
        .where(and_(*conditions))
        .order_by(Appointment.client_id.asc(), Appointment.date.asc(), Appointment.id.asc())
    )
    out: dict[int, dict[str, Any]] = {}
    for row in (await db.execute(stmt)).all():
        out[row.client_id] = {'staff_id': row.staff_id, 'staff_name': row.name, 'date': row.date}
    return out


async def _churn_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    clients = await _clients_rows(
        db,
        date(2000, 1, 1),
        end,
        company_id,
        staff_id,
        allowed_company_ids,
        factual_at,
    )
    risk_rows = [
        row for row in clients
        if (
            row.get('client_id') is not None
            and row.get('days_since_last_visit') is not None
            and int(row['days_since_last_visit']) >= 60
        )
    ]
    client_ids = [int(row['client_id']) for row in risk_rows if row.get('client_id') is not None]
    last_staff = await _last_staff_by_client(
        db,
        client_ids,
        company_id,
        staff_id,
        allowed_company_ids,
        factual_at,
    )
    staff_losses: dict[str, dict[str, Any]] = defaultdict(lambda: {'staff_name': 'Без мастера', 'clients': 0, 'revenue': 0.0})
    segment_rows_by_name: dict[str, dict[str, Any]] = defaultdict(lambda: {
        'segment': '',
        'clients': 0,
        'visits': 0,
        'revenue': 0.0,
    })
    for row in risk_rows:
        days = int(row['days_since_last_visit'])
        row['segment'] = 'Под риском' if days < 90 else 'Спящие' if days < 180 else 'Потерянные'
        info = last_staff.get(int(row['client_id'] or 0), {})
        row['last_staff'] = info.get('staff_name') or 'Без мастера'
        bucket = staff_losses[row['last_staff']]
        bucket['staff_name'] = row['last_staff']
        bucket['clients'] += 1
        bucket['revenue'] += float(row.get('revenue') or 0)
        segment_bucket = segment_rows_by_name[row['segment']]
        segment_bucket['segment'] = row['segment']
        segment_bucket['clients'] += 1
        segment_bucket['visits'] += int(row.get('visits') or 0)
        segment_bucket['revenue'] += float(row.get('revenue') or 0)
    staff_rows = sorted(staff_losses.values(), key=lambda item: item['revenue'], reverse=True)
    segment_rows = []
    for item in segment_rows_by_name.values():
        clients_count = int(item['clients'] or 0)
        item['avg_revenue_per_client'] = float(item['revenue'] or 0) / clients_count if clients_count else 0.0
        item['avg_visits_per_client'] = float(item['visits'] or 0) / clients_count if clients_count else 0.0
        segment_rows.append(item)
    segment_rows.sort(key=lambda item: item['clients'], reverse=True)
    at_risk = sum(1 for row in risk_rows if row['segment'] == 'Под риском')
    sleeping = sum(1 for row in risk_rows if row['segment'] == 'Спящие')
    lost = sum(1 for row in risk_rows if row['segment'] == 'Потерянные')
    revenue_at_risk = sum(float(row.get('revenue') or 0) for row in risk_rows)
    base['cards'] = [
        _card('Под риском', at_risk, NUMBER_FORMAT),
        _card('Спящие', sleeping, NUMBER_FORMAT),
        _card('Потерянные', lost, NUMBER_FORMAT),
        _card('Выручка под риском', revenue_at_risk, MONEY_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'churn_segments',
            'Клиенты по сегментам оттока',
            'doughnut',
            ['Под риском', 'Спящие', 'Потерянные'],
            [{'label': 'Клиентов', 'data': [at_risk, sleeping, lost], 'format': NUMBER_FORMAT}],
        ),
        _chart(
            'losses_by_staff',
            'Потери по последнему мастеру',
            'bar',
            [row['staff_name'] for row in staff_rows[:12]],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in staff_rows[:12]], 'format': MONEY_FORMAT}],
        ),
    ]
    base['tables'] = [
        _table(
            'risk_segments',
            'Сегменты оттока',
            [
                ('segment', 'Сегмент', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('visits', 'Визиты', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('avg_revenue_per_client', 'Доход на клиента', MONEY_FORMAT),
                ('avg_visits_per_client', 'Визитов на клиента', DECIMAL_FORMAT),
            ],
            segment_rows,
        ),
        _table(
            'losses_by_staff',
            'Потери по мастерам',
            [
                ('staff_name', 'Мастер', 'text'),
                ('clients', 'Клиентов', NUMBER_FORMAT),
                ('revenue', 'Выручка под риском', MONEY_FORMAT),
            ],
            staff_rows,
        ),
    ]
    base['notes'].append({
        'kind': 'formula',
        'title': 'Обезличенный отток',
        'text': 'Отчет считает клиентов по сегментам оттока и мастерам без раскрытия клиентских карточек.',
    })
    base['raw'] = {'segments': segment_rows, 'staff': staff_rows}
    return base


async def _goods_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    granularity: str,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    conditions = [
        GoodTransaction.type_id == GOODS_SALE_TYPE_ID,
        func.date(GoodTransaction.date) >= start,
        func.date(GoodTransaction.date) <= end,
        GoodTransaction.date <= factual_at,
        _business_staff_id_condition(GoodTransaction.master_id),
        reporting_start_clause(GoodTransaction.company_id, func.date(GoodTransaction.date)),
    ]
    scope = _company_scope_clause(GoodTransaction.company_id, company_id, allowed_company_ids)
    if scope is not None:
        conditions.append(scope)
    if staff_id is not None:
        conditions.append(GoodTransaction.master_id == staff_id)
    stmt = (
        select(
            GoodTransaction.company_id,
            GoodTransaction.good_id,
            GoodTransaction.good_title,
            GoodTransaction.amount,
            GoodTransaction.date,
            GoodTransaction.master_id,
            Staff.name.label('staff_name'),
        )
        .outerjoin(Staff, Staff.id == GoodTransaction.master_id)
        .where(and_(*conditions))
    )
    inventory_rows = (await db.execute(stmt)).all()
    inventory_title_by_key = {
        (row.company_id, row.good_id): row.good_title
        for row in inventory_rows
        if row.good_title
    }
    paid_rows = await fetch_paid_goods_rows(
        db,
        start,
        end,
        company_id,
        staff_id,
        allowed_company_ids,
        factual_at,
    )

    goods: dict[str, dict[str, Any]] = defaultdict(lambda: {'good_title': 'Товар', 'sales_count': 0, 'units': 0.0, 'revenue': 0.0})
    by_staff: dict[str, dict[str, Any]] = defaultdict(lambda: {'staff_name': 'Без мастера', 'sales_count': 0, 'revenue': 0.0})
    by_period: dict[str, dict[str, Any]] = defaultdict(lambda: {'period': '', 'sales_count': 0, 'units': 0.0, 'revenue': 0.0})
    for row in inventory_rows:
        key = f'{row.company_id}:{row.good_id or row.good_title or "unknown"}'
        title = row.good_title or f"Товар {row.good_id or '—'}"
        units = abs(float(row.amount or 0))
        goods[key]['good_title'] = title
        goods[key]['units'] += units
        period = _period_key(row.date, granularity)
        if period:
            by_period[period]['period'] = period
            by_period[period]['units'] += units

    for row in paid_rows:
        good_id = row.get('good_id')
        item_company_id = row.get('company_id')
        key = f'{item_company_id}:{good_id or row.get("good_title") or "unknown"}'
        title = (
            row.get('good_title')
            or inventory_title_by_key.get((item_company_id, good_id))
            or f"Товар {good_id or '—'}"
        )
        revenue = float(row.get('amount') or 0)
        goods[key]['good_title'] = title
        goods[key]['sales_count'] += 1
        goods[key]['revenue'] += revenue
        staff_key = str(row.get('master_id') or 'none')
        by_staff[staff_key]['staff_name'] = row.get('staff_name') or 'Без мастера'
        by_staff[staff_key]['sales_count'] += 1
        by_staff[staff_key]['revenue'] += revenue
        period = _period_key(row.get('date'), granularity)
        if period:
            by_period[period]['period'] = period
            by_period[period]['sales_count'] += 1
            by_period[period]['revenue'] += revenue
    goods_rows = sorted(goods.values(), key=lambda item: item['revenue'], reverse=True)
    staff_rows = sorted(by_staff.values(), key=lambda item: item['revenue'], reverse=True)
    period_rows = [by_period[key] for key in sorted(by_period)]
    total_revenue = sum(row['revenue'] for row in goods_rows)
    total_units = sum(row['units'] for row in goods_rows)
    base['cards'] = [
        _card('Выручка товаров', total_revenue, MONEY_FORMAT),
        _card('Единиц продано', total_units, NUMBER_FORMAT),
        _card('Уникальных товаров', len(goods_rows), NUMBER_FORMAT),
        _card('Мастеров с продажами', len(staff_rows), NUMBER_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'goods_revenue',
            'Товары по выручке',
            'bar',
            [row['good_title'] for row in goods_rows[:12]],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in goods_rows[:12]], 'format': MONEY_FORMAT}],
        ),
        _chart(
            'goods_dynamics',
            'Динамика продаж товаров',
            'line',
            [row['period'] for row in period_rows],
            [
                {'label': 'Выручка', 'data': [row['revenue'] for row in period_rows], 'format': MONEY_FORMAT},
                {'label': 'Единиц', 'data': [row['units'] for row in period_rows], 'format': NUMBER_FORMAT, 'axis': 'y1'},
            ],
        ),
    ]
    base['tables'] = [
        _table(
            'goods',
            'Товары',
            [
                ('good_title', 'Товар', 'text'),
                ('sales_count', 'Продаж', NUMBER_FORMAT),
                ('units', 'Единиц', DECIMAL_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
            ],
            goods_rows,
        ),
        _table(
            'goods_by_staff',
            'Продажи по мастерам',
            [
                ('staff_name', 'Мастер', 'text'),
                ('sales_count', 'Продаж', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
            ],
            staff_rows,
        ),
    ]
    base['raw'] = {'goods': goods_rows, 'by_staff': staff_rows, 'by_period': period_rows}
    return base


async def _operations_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    granularity: str,
    allowed_company_ids: list[int] | None,
    factual_at: datetime,
) -> dict[str, Any]:
    stmt = (
        select(Appointment.id, Appointment.date, Appointment.datetime, Appointment.attendance, Appointment.staff_id, Staff.name)
        .outerjoin(Staff, Staff.id == Appointment.staff_id)
        .where(and_(*_appointment_conditions(
            start,
            end,
            company_id,
            staff_id,
            allowed_company_ids=allowed_company_ids,
            factual_at=factual_at,
        )))
    )
    by_hour = defaultdict(lambda: {'hour': 0, 'records': 0, 'completed': 0, 'cancelled': 0})
    by_period = defaultdict(lambda: {'period': '', 'records': 0, 'completed': 0, 'cancelled': 0})
    by_staff = defaultdict(lambda: {'staff_name': 'Без мастера', 'records': 0, 'completed': 0, 'cancelled': 0})
    for row in (await db.execute(stmt)).all():
        hour = row.datetime.hour if row.datetime else 0
        by_hour[hour]['hour'] = hour
        by_hour[hour]['records'] += 1
        period = _period_key(row.date, granularity)
        by_period[period]['period'] = period
        by_period[period]['records'] += 1
        staff_key = str(row.staff_id or 'none')
        by_staff[staff_key]['staff_name'] = row.name or 'Без мастера'
        by_staff[staff_key]['records'] += 1
        if row.attendance == COMPLETED_ATTENDANCE:
            by_hour[hour]['completed'] += 1
            by_period[period]['completed'] += 1
            by_staff[staff_key]['completed'] += 1
        elif row.attendance and row.attendance < 0:
            by_hour[hour]['cancelled'] += 1
            by_period[period]['cancelled'] += 1
            by_staff[staff_key]['cancelled'] += 1
    hour_rows = [by_hour[key] for key in sorted(by_hour)]
    period_rows = [by_period[key] for key in sorted(by_period)]
    staff_rows = sorted(by_staff.values(), key=lambda item: item['records'], reverse=True)
    local_totals = {
        'available_records': sum(row['records'] for row in period_rows),
        'completed': sum(row['completed'] for row in period_rows),
        'no_show': sum(row['cancelled'] for row in period_rows),
    }
    exact = await fetch_appointments_breakdown(
        db,
        start,
        end,
        company_id,
        staff_id,
        allowed_company_ids=allowed_company_ids,
        factual_at=factual_at,
    )
    if exact['source_status'] in {'ready', 'local'}:
        base['cards'] = [
            _card('Всего записей', exact['total'], NUMBER_FORMAT),
            _card('Завершено', exact['completed'], NUMBER_FORMAT),
            _card('Отменено', exact['cancelled'], NUMBER_FORMAT),
            _card('Незавершено', exact['incomplete'], NUMBER_FORMAT),
        ]
    else:
        base['source_status'] = 'partial'
        base['cards'] = [
            _card('Всего записей', None, NUMBER_FORMAT),
            _card('Завершено', None, NUMBER_FORMAT),
            _card('Отменено', None, NUMBER_FORMAT),
            _card('Незавершено', None, NUMBER_FORMAT),
        ]
        base['notes'].append({
            'kind': 'warning',
            'title': 'Точные агрегаты недоступны',
            'text': 'YCLIENTS не вернул record_stats для выбранного периода.',
        })
    base['notes'].append({
        'kind': 'info',
        'title': 'Состав детализации',
        'text': (
            'Карточки рассчитаны по точным record_stats YCLIENTS. '
            'Графики и таблица содержат только записи, доступные в локальной базе; '
            'удаленные отмены невозможно распределить по часу и сотруднику.'
        ),
    })
    base['charts'] = [
        _chart(
            'records_by_period',
            'Записи по периодам',
            'line',
            [row['period'] for row in period_rows],
            [
                {'label': 'Доступные записи', 'data': [row['records'] for row in period_rows], 'format': NUMBER_FORMAT},
                {'label': 'Завершено', 'data': [row['completed'] for row in period_rows], 'format': NUMBER_FORMAT},
                {'label': 'Неявки', 'data': [row['cancelled'] for row in period_rows], 'format': NUMBER_FORMAT},
            ],
        ),
        _chart(
            'records_by_hour',
            'Загрузка по часам',
            'bar',
            [f"{row['hour']}:00" for row in hour_rows],
            [{'label': 'Записи', 'data': [row['records'] for row in hour_rows], 'format': NUMBER_FORMAT}],
        ),
    ]
    base['tables'] = [
        _table(
            'staff_records',
            'Записи по мастерам',
            [
                ('staff_name', 'Мастер', 'text'),
                ('records', 'Доступные записи', NUMBER_FORMAT),
                ('completed', 'Завершено', NUMBER_FORMAT),
                ('cancelled', 'Неявки', NUMBER_FORMAT),
            ],
            staff_rows,
        )
    ]
    base['raw'] = {
        'exact_aggregates': exact,
        'local_available_aggregates': local_totals,
        'by_period': period_rows,
        'by_hour': hour_rows,
        'by_staff': staff_rows,
    }
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
    base['cards'] = [
        _card('Топ выручка мастера', revenue_barber[0]['value'] if revenue_barber else 0, MONEY_FORMAT),
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
                'Топ по выручке — админы',
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
            'Топ по услугам с меткой «Доп. услуга»',
            [staff_col, branch_col, qty_col, ('sum', 'Сумма', MONEY_FORMAT), ('pct', 'Доп. услуги, %', PERCENT_FORMAT), extra_share_col],
            boards.get('extra_services_rankings', {}),
            'pct',
            [('qty', 'По количеству'), ('sum', 'По сумме'), ('pct', 'По проценту')],
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
            'Топ по ОПЗ — админы',
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
            'Топ по выручке — админы',
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


async def _nps_payload(
    db: AsyncSession,
    base: dict[str, Any],
    definition: ReportDefinition,
    start: date,
    end: date,
    company_id: int | None,
    allowed_company_ids: list[int] | None,
) -> dict[str, Any]:
    base['missing_sources'] = ['telegram_nps']
    conditions = [
        func.date(Comment.date) >= start,
        func.date(Comment.date) <= end,
        reporting_start_clause(Comment.company_id, func.date(Comment.date)),
    ]
    scope = _company_scope_clause(Comment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        conditions.append(scope)
    stmt = (
        select(Comment.rating, Comment.text, Comment.date, Comment.master_id, Staff.name.label('staff_name'))
        .outerjoin(Staff, Staff.id == Comment.master_id)
        .where(and_(*conditions))
    )
    ratings = []
    negative_rows = []
    distribution = defaultdict(int)
    for row in (await db.execute(stmt)).all():
        rating = float(row.rating or 0)
        if rating > 0:
            ratings.append(rating)
            distribution[str(int(round(rating)))] += 1
        if rating and rating <= 3:
            negative_rows.append({
                'rating': rating,
                'comment': row.text,
                'date': row.date.isoformat() if row.date else None,
                'staff_name': row.staff_name,
            })
    avg_rating = sum(ratings) / len(ratings) if ratings else None
    base['cards'] = [
        _card('Отзывы YClients', len(ratings), NUMBER_FORMAT),
        _card('Средний рейтинг', avg_rating or 0, DECIMAL_FORMAT),
        _card('Низкие оценки', len(negative_rows), NUMBER_FORMAT),
        _card('NPS Telegram', None, 'text'),
    ]
    base['charts'] = [
        _chart(
            'ratings',
            'Распределение оценок',
            'bar',
            [str(i) for i in range(1, 6)],
            [{'label': 'Отзывов', 'data': [distribution[str(i)] for i in range(1, 6)], 'format': NUMBER_FORMAT}],
        )
    ]
    base['tables'] = [
        _table(
            'negative_reviews',
            'Низкие оценки',
            [
                ('date', 'Дата', 'date'),
                ('staff_name', 'Мастер', 'text'),
                ('rating', 'Оценка', DECIMAL_FORMAT),
                ('comment', 'Комментарий', 'text'),
            ],
            negative_rows,
        )
    ]
    base['notes'].append({
        'kind': 'partial',
        'title': 'NPS-опросы не подключены',
        'text': 'Показаны отзывы и оценки из YClients. Для NPS нужен отдельный источник telegram_nps.',
    })
    base['raw'] = {'required_sources': list(definition.required_sources)}
    return base
