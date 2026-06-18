"""Report catalog and report-data builders for the product dashboard SPA."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dashboard_service import (
    COMPLETED_ATTENDANCE,
    fetch_extra_services,
    fetch_appointments_breakdown,
    fetch_plan_fact,
    fetch_revenue_daily,
    fetch_summary,
    fetch_top_services,
)
from models import (
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Staff,
    Transaction,
)

REPORT_GRANULARITIES = {'day', 'week', 'month'}
MONEY_FORMAT = 'money'
NUMBER_FORMAT = 'number'
PERCENT_FORMAT = 'percent'
DECIMAL_FORMAT = 'decimal'

REPORT_ORDER = (
    'admin_performance',
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
    'master_avg_check',
    'master_motivation',
    'masters_rating',
    'milena_dynamics',
    'milena_lost',
    'milena_pl',
    'milena_ranking',
    'milena_salary',
    'milena_upsell',
    'mind_index',
    'month_return',
    'new_vs_returning_cross',
    'nps_dashboard',
    'opz_report',
    'peak_hours_site_vs_salon',
    'peak_load',
    'plan_execution',
    'plan_fact_master',
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
    'staff_salary',
    'staff_services',
    'staff_time_heatmap',
    'top_clients_pareto',
    'top_goods_revenue',
    'traffic_source_roi',
    'visit_forecast',
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
    'master_avg_check',
    'masters_rating',
    'peak_load',
    'plan_execution',
    'plan_fact_master',
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
    'staff_services',
    'staff_time_heatmap',
    'top_clients_pareto',
    'top_goods_revenue',
}

SOURCE_MISSING_REPORTS = {
    'conversion_funnel',
    'demographics_check',
    'devices_vs_booking',
    'goal_conversions_report',
    'market_benchmarks',
    'new_vs_returning_cross',
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
}
BOOKING_REPORTS = {'bookings_dynamics', 'cancellation_analysis', 'peak_load'}
STAFF_REPORTS = {
    'master_avg_check',
    'masters_rating',
    'staff_efficiency',
    'staff_services',
    'staff_time_heatmap',
}
SERVICE_REPORTS = {'avg_check_by_service', 'service_combos', 'service_staff_profit', 'service_trends'}
CLIENT_REPORTS = {
    'client_cohorts',
    'client_journey',
    'client_labels',
    'retention_3_6_12',
    'rfm_analysis',
    'top_clients_pareto',
}
CHURN_REPORTS = {'losses_by_staff', 'lost_clients_list', 'return_priorities', 'revenue_at_risk'}
GOODS_REPORTS = {'goods_by_staff', 'goods_conversion', 'goods_dynamics', 'top_goods_revenue'}
PLAN_REPORTS = {'plan_execution', 'plan_fact_master'}
MILENA_REPORTS = {
    'milena_dynamics',
    'milena_lost',
    'milena_pl',
    'milena_ranking',
    'milena_salary',
    'milena_upsell',
}

TITLE_OVERRIDES = {
    'admin_performance': 'Эффективность администраторов',
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
    'master_avg_check': 'Средний чек мастеров',
    'master_motivation': 'Мотивация мастеров',
    'masters_rating': 'Рейтинг мастеров',
    'mind_index': 'MInd индекс мастеров',
    'month_return': 'Возвратность месяц к месяцу',
    'new_vs_returning_cross': 'Новые и повторные: сайт и CRM',
    'nps_dashboard': 'NPS и отзывы',
    'opz_report': 'ОПЗ',
    'peak_hours_site_vs_salon': 'Пиковые часы: сайт и салон',
    'peak_load': 'Пиковая загрузка',
    'plan_execution': 'Выполнение плана',
    'plan_fact_master': 'План/факт мастеров',
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
    'staff_salary': 'Зарплата сотрудников',
    'staff_services': 'Услуги по мастерам',
    'staff_time_heatmap': 'Тепловая карта мастеров',
    'top_clients_pareto': 'Топ клиентов и Парето',
    'top_goods_revenue': 'Топ товаров по выручке',
    'traffic_source_roi': 'ROI источников трафика',
    'visit_forecast': 'Прогноз визитов',
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
    if report_id in PLAN_REPORTS:
        return 'plans'
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
    if report_id in MILENA_REPORTS:
        return 'milena'
    if report_id in {'data_audit'}:
        return 'diagnostics'
    return 'advanced'


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


def _filters_for(report_id: str, group: str, status: str) -> dict[str, bool]:
    return {
        'date_range': True,
        'branch': True,
        'staff': group in {'team', 'services', 'clients', 'churn', 'goods', 'operations'} or report_id in READY_REPORTS,
        'granularity': report_id in {
            'avg_check_dynamics',
            'booking_channels',
            'bookings_dynamics',
            'financial_overview',
            'goods_conversion',
            'goods_dynamics',
            'revenue_decomposition',
            'revenue_dynamics',
            'service_trends',
        },
        'compare': status == 'ready',
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
    if report_id in MILENA_REPORTS:
        return ('yclients', 'milena_methodology_settings')
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
    return 'Отчет строится на текущих данных YClients в PostgreSQL.'


REPORT_REGISTRY = _build_registry()


def fetch_report_registry() -> list[dict[str, Any]]:
    return [REPORT_REGISTRY[report_id].to_payload() for report_id in REPORT_ORDER]


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
) -> dict[str, Any]:
    if report_id not in REPORT_REGISTRY:
        raise ValueError(f'unknown report_id: {report_id}')
    if granularity not in REPORT_GRANULARITIES:
        raise ValueError('granularity must be one of day, week, month')
    if start > end:
        raise ValueError('start_date must be <= end_date')
    if compare_start and compare_end and compare_start > compare_end:
        raise ValueError('compare_start_date must be <= compare_end_date')

    data = await _fetch_report_payload(db, report_id, start, end, company_id, staff_id, granularity)
    if data['source_status'] == 'ready' and ((compare_start and compare_end) or compare_staff_id):
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
        )
        data['comparison'] = {
            'period': compare_data['period'],
            'staff_id': cmp_staff_id,
            'source_status': compare_data['source_status'],
            'cards': compare_data.get('cards', []),
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
    if report_id == 'nps_dashboard':
        return await _nps_payload(db, base, definition, start, end, company_id)
    if report_id in PLAN_REPORTS:
        return await _plan_payload(db, base, start, end, company_id, staff_id)
    if report_id in GOODS_REPORTS:
        return await _goods_payload(db, base, start, end, company_id, staff_id, granularity)
    if report_id in STAFF_REPORTS:
        return await _staff_payload(db, base, start, end, company_id, staff_id)
    if report_id in SERVICE_REPORTS:
        return await _services_payload(db, base, start, end, company_id, staff_id, granularity)
    if report_id in CLIENT_REPORTS:
        return await _clients_payload(db, base, start, end, company_id, staff_id)
    if report_id in CHURN_REPORTS:
        return await _churn_payload(db, base, start, end, company_id, staff_id)
    if report_id in BOOKING_REPORTS:
        return await _operations_payload(db, base, start, end, company_id, staff_id, granularity)
    return await _financial_payload(db, base, start, end, company_id, staff_id, granularity)


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
) -> dict[str, Any]:
    return {
        'id': table_id,
        'title': title,
        'columns': [{'key': key, 'label': label, 'format': fmt} for key, label, fmt in columns],
        'rows': rows,
    }


def _appointment_conditions(
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    *,
    attended_only: bool = False,
) -> list[Any]:
    conditions = [Appointment.date >= start, Appointment.date <= end]
    if attended_only:
        conditions.append(Appointment.attendance == COMPLETED_ATTENDANCE)
    if company_id is not None:
        conditions.append(Appointment.company_id == company_id)
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
        'appointments': 0.0,
    })
    for row in rows:
        key = _period_key(date.fromisoformat(row['date']), granularity)
        grouped[key]['revenue'] += float(row.get('revenue') or 0)
        grouped[key]['service_revenue'] += float(row.get('service_revenue') or 0)
        grouped[key]['goods_revenue'] += float(row.get('goods_revenue') or 0)
        grouped[key]['appointments'] += float(row.get('appointments') or 0)
    return [{'period': key, **values} for key, values in sorted(grouped.items())]


async def _financial_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    granularity: str,
) -> dict[str, Any]:
    summary = await fetch_summary(db, start, end, company_id, staff_id)
    daily = _aggregate_daily(await fetch_revenue_daily(db, start, end, company_id, staff_id), granularity)
    services = await fetch_top_services(db, start, end, company_id, 15, staff_id)
    revenue = summary.get('revenue', {})
    avg = summary.get('average_check', {})
    visits = summary.get('visit_metrics', {})
    base['cards'] = [
        _card('Выручка', revenue.get('total', 0), MONEY_FORMAT),
        _card('Услуги', revenue.get('service_revenue', 0), MONEY_FORMAT),
        _card('Товары', revenue.get('goods_revenue', 0), MONEY_FORMAT),
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
            ],
            daily,
        ),
        _services_table('top_services', 'Услуги', services),
    ]
    base['raw'] = {'summary': summary, 'daily': daily, 'top_services': services}
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
) -> dict[str, Any]:
    services = await fetch_top_services(db, start, end, company_id, 25, staff_id)
    extra = await fetch_extra_services(db, start, end, company_id, 25, staff_id)
    total_revenue = sum(float(row.get('revenue') or 0) for row in services)
    total_sold = sum(float(row.get('sold') or 0) for row in services)
    base['cards'] = [
        _card('Услуг оказано', total_sold, NUMBER_FORMAT),
        _card('Выручка услуг', total_revenue, MONEY_FORMAT),
        _card('Уникальных услуг', len(services), NUMBER_FORMAT),
        _card('Доп. услуг в списке', len(extra), NUMBER_FORMAT),
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
    base['raw'] = {'services': services, 'extra_services': extra, 'granularity': granularity}
    return base


async def _staff_rows(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
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
        .where(and_(*_appointment_conditions(start, end, company_id, staff_id)))
        .group_by(Appointment.staff_id)
    )
    rev_stmt = (
        select(
            Appointment.staff_id.label('staff_id'),
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
        )
        .join(FinancialTransaction, FinancialTransaction.record_id == Appointment.id)
        .where(and_(*_appointment_conditions(start, end, company_id, staff_id, attended_only=True)))
        .group_by(Appointment.staff_id)
    )
    appt_rows = (await db.execute(appt_stmt)).all()
    revenue_by_staff = {row.staff_id: float(row.revenue or 0) for row in (await db.execute(rev_stmt)).all()}
    rows = []
    for row in appt_rows:
        completed = int(row.completed or 0)
        revenue = revenue_by_staff.get(row.staff_id, 0.0)
        rows.append({
            'staff_id': row.staff_id,
            'staff_name': row.staff_name or f"staff {row.staff_id or '—'}",
            'company_title': row.company_title,
            'appointments': int(row.appointments or 0),
            'completed': completed,
            'not_completed': int(row.not_completed or 0),
            'clients': int(row.clients or 0),
            'revenue': revenue,
            'avg_check': revenue / completed if completed else 0.0,
        })
    rows.sort(key=lambda item: (item['revenue'], item['completed']), reverse=True)
    return rows


async def _staff_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
) -> dict[str, Any]:
    rows = await _staff_rows(db, start, end, company_id, staff_id)
    total_revenue = sum(row['revenue'] for row in rows)
    total_completed = sum(row['completed'] for row in rows)
    base['cards'] = [
        _card('Сотрудников в отчете', len(rows), NUMBER_FORMAT),
        _card('Завершено записей', total_completed, NUMBER_FORMAT),
        _card('Выручка', total_revenue, MONEY_FORMAT),
        _card('Средний чек', total_revenue / total_completed if total_completed else 0, MONEY_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'staff_revenue',
            'Выручка по сотрудникам',
            'bar',
            [row['staff_name'] for row in rows[:12]],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in rows[:12]], 'format': MONEY_FORMAT}],
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
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('avg_check', 'Средний чек', MONEY_FORMAT),
            ],
            rows,
        )
    ]
    base['raw'] = {'staff': rows}
    return base


async def _clients_rows(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
) -> list[dict[str, Any]]:
    stmt = (
        select(
            Appointment.client_id.label('client_id'),
            func.min(Client.name).label('client_name'),
            func.min(Client.phone).label('phone'),
            func.count(func.distinct(Appointment.id)).label('visits'),
            func.max(Appointment.date).label('last_visit'),
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
        )
        .outerjoin(Client, Client.id == Appointment.client_id)
        .outerjoin(FinancialTransaction, FinancialTransaction.record_id == Appointment.id)
        .where(and_(*_appointment_conditions(start, end, company_id, staff_id, attended_only=True)))
        .group_by(Appointment.client_id)
    )
    rows = []
    for row in (await db.execute(stmt)).all():
        revenue = float(row.revenue or 0)
        visits = int(row.visits or 0)
        last_visit = row.last_visit
        recency = (end - last_visit).days if last_visit else None
        rows.append({
            'client_id': row.client_id,
            'client_name': row.client_name or f"Клиент {row.client_id or '—'}",
            'phone': row.phone,
            'visits': visits,
            'last_visit': last_visit.isoformat() if last_visit else None,
            'days_since_last_visit': recency,
            'revenue': revenue,
            'avg_check': revenue / visits if visits else 0.0,
        })
    rows.sort(key=lambda item: item['revenue'], reverse=True)
    return rows


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


async def _clients_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
) -> dict[str, Any]:
    rows = await _clients_rows(db, start, end, company_id, staff_id)
    total_revenue = sum(row['revenue'] for row in rows)
    total_visits = sum(row['visits'] for row in rows)
    avg_revenue = total_revenue / len(rows) if rows else 0.0
    segment_counts: dict[str, int] = defaultdict(int)
    for row in rows:
        row['segment'] = _segment_client(row, avg_revenue)
        segment_counts[row['segment']] += 1
    base['cards'] = [
        _card('Клиентов', len(rows), NUMBER_FORMAT),
        _card('Визитов', total_visits, NUMBER_FORMAT),
        _card('Выручка клиентов', total_revenue, MONEY_FORMAT),
        _card('Средний доход на клиента', avg_revenue, MONEY_FORMAT),
    ]
    base['charts'] = [
        _chart(
            'client_segments',
            'Сегменты клиентов',
            'doughnut',
            list(segment_counts.keys()),
            [{'label': 'Клиентов', 'data': list(segment_counts.values()), 'format': NUMBER_FORMAT}],
        ),
        _chart(
            'top_clients',
            'Топ клиентов по выручке',
            'bar',
            [row['client_name'] for row in rows[:12]],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in rows[:12]], 'format': MONEY_FORMAT}],
        ),
    ]
    base['tables'] = [
        _table(
            'clients',
            'Клиенты',
            [
                ('client_name', 'Клиент', 'text'),
                ('phone', 'Телефон', 'text'),
                ('visits', 'Визиты', NUMBER_FORMAT),
                ('last_visit', 'Последний визит', 'date'),
                ('days_since_last_visit', 'Дней нет', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('avg_check', 'Средний чек', MONEY_FORMAT),
                ('segment', 'Сегмент', 'text'),
            ],
            rows[:100],
        )
    ]
    base['raw'] = {'clients': rows, 'segments': dict(segment_counts)}
    return base


async def _last_staff_by_client(
    db: AsyncSession,
    client_ids: list[int],
    company_id: int | None,
    staff_id: int | None,
) -> dict[int, dict[str, Any]]:
    if not client_ids:
        return {}
    conditions = [
        Appointment.client_id.in_(client_ids),
        Appointment.attendance == COMPLETED_ATTENDANCE,
    ]
    if company_id is not None:
        conditions.append(Appointment.company_id == company_id)
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
) -> dict[str, Any]:
    clients = await _clients_rows(db, date(2000, 1, 1), end, company_id, staff_id)
    risk_rows = [
        row for row in clients
        if row.get('days_since_last_visit') is not None and int(row['days_since_last_visit']) >= 60
    ]
    client_ids = [int(row['client_id']) for row in risk_rows if row.get('client_id') is not None]
    last_staff = await _last_staff_by_client(db, client_ids, company_id, staff_id)
    staff_losses: dict[str, dict[str, Any]] = defaultdict(lambda: {'staff_name': 'Без мастера', 'clients': 0, 'revenue': 0.0})
    for row in risk_rows:
        days = int(row['days_since_last_visit'])
        row['segment'] = 'Под риском' if days < 90 else 'Спящие' if days < 180 else 'Потерянные'
        info = last_staff.get(int(row['client_id'] or 0), {})
        row['last_staff'] = info.get('staff_name') or 'Без мастера'
        bucket = staff_losses[row['last_staff']]
        bucket['staff_name'] = row['last_staff']
        bucket['clients'] += 1
        bucket['revenue'] += float(row.get('revenue') or 0)
    staff_rows = sorted(staff_losses.values(), key=lambda item: item['revenue'], reverse=True)
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
            'risk_clients',
            'Клиенты для возврата',
            [
                ('client_name', 'Клиент', 'text'),
                ('phone', 'Телефон', 'text'),
                ('days_since_last_visit', 'Дней нет', NUMBER_FORMAT),
                ('last_staff', 'Последний мастер', 'text'),
                ('visits', 'Визиты', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('segment', 'Сегмент', 'text'),
            ],
            risk_rows[:100],
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
    base['raw'] = {'clients': risk_rows, 'staff': staff_rows}
    return base


async def _goods_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
    granularity: str,
) -> dict[str, Any]:
    conditions = [
        GoodTransaction.type_id == 1,
        func.date(GoodTransaction.date) >= start,
        func.date(GoodTransaction.date) <= end,
    ]
    if company_id is not None:
        conditions.append(GoodTransaction.company_id == company_id)
    if staff_id is not None:
        conditions.append(GoodTransaction.master_id == staff_id)
    stmt = (
        select(
            GoodTransaction.good_id,
            GoodTransaction.good_title,
            GoodTransaction.amount,
            GoodTransaction.cost,
            GoodTransaction.date,
            GoodTransaction.master_id,
            Staff.name.label('staff_name'),
        )
        .outerjoin(Staff, Staff.id == GoodTransaction.master_id)
        .where(and_(*conditions))
    )
    goods: dict[str, dict[str, Any]] = defaultdict(lambda: {'good_title': 'Товар', 'sales_count': 0, 'units': 0.0, 'revenue': 0.0})
    by_staff: dict[str, dict[str, Any]] = defaultdict(lambda: {'staff_name': 'Без мастера', 'sales_count': 0, 'revenue': 0.0})
    by_period: dict[str, dict[str, Any]] = defaultdict(lambda: {'period': '', 'sales_count': 0, 'units': 0.0, 'revenue': 0.0})
    for row in (await db.execute(stmt)).all():
        key = str(row.good_id or row.good_title or 'unknown')
        title = row.good_title or f"Товар {row.good_id or '—'}"
        units = abs(float(row.amount or 0))
        revenue = float(row.cost or 0)
        goods[key]['good_title'] = title
        goods[key]['sales_count'] += 1
        goods[key]['units'] += units
        goods[key]['revenue'] += revenue
        staff_key = str(row.master_id or 'none')
        by_staff[staff_key]['staff_name'] = row.staff_name or 'Без мастера'
        by_staff[staff_key]['sales_count'] += 1
        by_staff[staff_key]['revenue'] += revenue
        period = _period_key(row.date, granularity)
        by_period[period]['period'] = period
        by_period[period]['sales_count'] += 1
        by_period[period]['units'] += units
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
) -> dict[str, Any]:
    stmt = (
        select(Appointment.id, Appointment.date, Appointment.datetime, Appointment.attendance, Appointment.staff_id, Staff.name)
        .outerjoin(Staff, Staff.id == Appointment.staff_id)
        .where(and_(*_appointment_conditions(start, end, company_id, staff_id)))
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
    exact = await fetch_appointments_breakdown(db, start, end, company_id, staff_id)
    if exact['source_status'] == 'ready':
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


async def _plan_payload(
    db: AsyncSession,
    base: dict[str, Any],
    start: date,
    end: date,
    company_id: int | None,
    staff_id: int | None,
) -> dict[str, Any]:
    plan = await fetch_plan_fact(db, start, end, company_id, staff_id)
    rows = []
    for group in plan.get('groups', []):
        for metric in group.get('metrics', []):
            rows.append({
                'scope': group.get('title'),
                'metric': metric.get('label') or metric.get('code'),
                'plan': metric.get('plan'),
                'fact': metric.get('fact'),
                'remaining': metric.get('remaining'),
                'completion_pct': metric.get('completion_pct'),
                'status': metric.get('status'),
            })
    completion_values = [float(row['completion_pct']) for row in rows if row.get('completion_pct') is not None]
    base['cards'] = [
        _card('Строк плана', len(plan.get('groups', [])), NUMBER_FORMAT),
        _card('Метрик', len(rows), NUMBER_FORMAT),
        _card('Среднее выполнение', sum(completion_values) / len(completion_values) if completion_values else 0, PERCENT_FORMAT),
    ]
    base['tables'] = [
        _table(
            'plan_fact',
            'План/факт',
            [
                ('scope', 'Разрез', 'text'),
                ('metric', 'Метрика', 'text'),
                ('plan', 'План', DECIMAL_FORMAT),
                ('fact', 'Факт', DECIMAL_FORMAT),
                ('remaining', 'Осталось', DECIMAL_FORMAT),
                ('completion_pct', '% выполнения', PERCENT_FORMAT),
                ('status', 'Статус', 'text'),
            ],
            rows,
        )
    ]
    base['raw'] = {'plan_fact': plan}
    return base


async def _nps_payload(
    db: AsyncSession,
    base: dict[str, Any],
    definition: ReportDefinition,
    start: date,
    end: date,
    company_id: int | None,
) -> dict[str, Any]:
    base['missing_sources'] = ['telegram_nps']
    conditions = [
        func.date(Comment.date) >= start,
        func.date(Comment.date) <= end,
    ]
    if company_id is not None:
        conditions.append(Comment.company_id == company_id)
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
