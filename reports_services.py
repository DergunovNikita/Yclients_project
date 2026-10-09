"""Builders for the service reports."""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, case, func, select

from dashboard_service import (
    _appointment_company_ids,
    _appt_revenue_filters,
    _service_group_key,
    _transaction_service_catalog_join,
    fetch_extra_services,
    fetch_top_services,
)
from models import Appointment, ServiceCatalog, Transaction
from report_payload import MONEY_FORMAT, NUMBER_FORMAT, PERCENT_FORMAT, ReportRequest, card, chart, table, top_rows

NO_STAFF_LABEL = 'Без мастера'
CHART_ROWS = 12
# Pairs of ~200 services are tens of thousands of rows; a table nobody can read helps nobody.
COMBO_TABLE_LIMIT = 50


def _service_title(title: str | None, service_id: int | None) -> str:
    """Name a service for display; a bare id means YClients gave us neither a visit line nor a catalog entry."""
    title = (title or '').strip()
    if not title or title == str(service_id):
        return f'Услуга №{service_id}' if service_id is not None else 'Услуга'
    return title


def _ratio(numerator: float, denominator: float, scale: float = 1) -> float | None:
    return numerator / denominator * scale if denominator else None


async def build_avg_check_by_service(req: ReportRequest) -> dict[str, Any]:
    common = dict(
        company_id=req.company_id,
        limit=None,
        staff_id=req.staff_id,
        allowed_company_ids=req.allowed_company_ids,
        factual_at=req.factual_at,
    )
    services = await fetch_top_services(req.db, req.start, req.end, **common)
    extra = await fetch_extra_services(req.db, req.start, req.end, **common)
    total_revenue = sum(float(row['revenue']) for row in services)
    total_sold = sum(int(row['sold']) for row in services)

    def service_row(row: dict[str, Any], with_share: bool) -> dict[str, Any]:
        revenue = float(row['revenue'])
        sold = int(row['sold'])
        item = {
            'title': _service_title(row['title'], row['service_id']),
            'sold': sold,
            'revenue': revenue,
            'avg_price': _ratio(revenue, sold),
        }
        if with_share:
            item['revenue_share'] = _ratio(revenue, total_revenue, 100)
        return item

    rows = [service_row(row, True) for row in services]
    extra_rows = [service_row(row, False) for row in extra]
    req.base['notes'].append({
        'kind': 'formula',
        'title': 'Средняя цена услуги',
        'text': (
            'Выручка услуги по дате оплаты, делённая на число оказанных услуг. Услуги, включённые в пакет '
            'и не оплаченные отдельно, входят в количество и снижают среднюю цену.'
        ),
    })
    req.base['cards'] = [
        card('Услуг оказано', total_sold, NUMBER_FORMAT),
        card('Выручка услуг', total_revenue, MONEY_FORMAT),
        card('Средняя цена услуги', _ratio(total_revenue, total_sold), MONEY_FORMAT),
    ]
    top = top_rows(req, rows, CHART_ROWS)
    req.base['charts'] = [
        chart(
            'service_revenue',
            'Услуги по выручке',
            'bar',
            [row['title'] for row in top],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in top], 'format': MONEY_FORMAT}],
        )
    ]
    if extra_rows:
        top_extra = top_rows(req, extra_rows, CHART_ROWS)
        req.base['charts'].append(
            chart(
                'extra_services',
                'Доп. услуги',
                'bar',
                [row['title'] for row in top_extra],
                [{'label': 'Оказано', 'data': [row['sold'] for row in top_extra], 'format': NUMBER_FORMAT}],
            )
        )
    req.base['tables'] = [
        table(
            'services',
            'Услуги',
            [
                ('title', 'Услуга', 'text'),
                ('sold', 'Кол-во', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('avg_price', 'Средняя цена', MONEY_FORMAT),
                ('revenue_share', 'Доля выручки', PERCENT_FORMAT),
            ],
            rows,
            row_key='title',
        ),
        table(
            'extra_services',
            'Дополнительные услуги',
            [
                ('title', 'Услуга', 'text'),
                ('sold', 'Кол-во', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('avg_price', 'Средняя цена', MONEY_FORMAT),
            ],
            extra_rows,
            row_key='title',
        ),
    ]
    req.base['raw'] = {
        'services': rows,
        'extra_services': extra_rows,
        'service_attribution': {'mode': 'master', 'source_status': 'ready', 'missing_sources': []},
    }
    return req.base


async def build_service_staff_profit(req: ReportRequest) -> dict[str, Any]:
    services = await fetch_top_services(
        req.db,
        req.start,
        req.end,
        req.company_id,
        None,
        req.staff_id,
        allowed_company_ids=req.allowed_company_ids,
        factual_at=req.factual_at,
        group_by_staff=True,
    )
    rows = []
    for row in services:
        revenue = float(row['revenue'])
        sold = int(row['sold'])
        staff_id = row['staff_id']
        title = _service_title(row['title'], row['service_id'])
        rows.append({
            # Staff are told apart by id, never by name: two people can share one.
            'key': f'{staff_id}|{title}',
            'staff_id': staff_id,
            'staff_name': row['staff_name'] or NO_STAFF_LABEL,
            'company_title': row['company_title'],
            'title': title,
            'sold': sold,
            'revenue': revenue,
            'avg_price': _ratio(revenue, sold),
        })
    rows.sort(key=lambda row: (
        row['staff_id'] is None,
        row['staff_name'].casefold(),
        row['company_title'] or '',
        row['staff_id'] if row['staff_id'] is not None else -1,
        -row['revenue'],
        -row['sold'],
        row['title'],
    ))
    req.base['notes'].append({
        'kind': 'formula',
        'title': 'Выручка услуг по мастерам',
        'text': (
            'Услуга относится к мастеру визита, как в Обзоре и в отчёте «Эффективность сотрудников»; '
            'сумма по всем мастерам равна выручке услуг в Обзоре.'
        ),
    })
    req.base['cards'] = [
        card('Мастеров', len({row['staff_id'] for row in rows if row['staff_id'] is not None}), NUMBER_FORMAT),
        card('Услуг оказано', sum(row['sold'] for row in rows), NUMBER_FORMAT),
        card('Выручка услуг', sum(row['revenue'] for row in rows), MONEY_FORMAT),
    ]
    req.base['tables'] = [
        table(
            'staff_services',
            'Услуги по мастерам',
            [
                ('staff_name', 'Мастер', 'text'),
                ('company_title', 'Филиал', 'text'),
                ('title', 'Услуга', 'text'),
                ('sold', 'Кол-во', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('avg_price', 'Средняя цена', MONEY_FORMAT),
            ],
            rows,
            row_key='key',
        )
    ]
    # No raw copy: the long table already carries the rows, and a second one doubles a ~3 MB payload.
    return req.base


async def build_service_combos(req: ReportRequest) -> dict[str, Any]:
    allowed_company_ids = await _appointment_company_ids(
        req.db, req.company_id, req.staff_id, req.allowed_company_ids
    )
    title_expr = func.trim(func.coalesce(func.nullif(Transaction.service_title, ''), ServiceCatalog.title, ''))
    group_key = _service_group_key(title_expr, Transaction.service_id)
    # One row per (visit, service): the visit lines repeat a service, and a pair must count a visit once.
    # A CTE because the pair self-join reads it twice and PostgreSQL then materializes it once.
    visit_services = (
        select(
            Transaction.appointment_id.label('visit'),
            group_key.label('service_key'),
            func.min(title_expr).label('title'),
            func.min(Transaction.service_id).label('service_id'),
        )
        .select_from(Transaction)
        .join(Appointment, Appointment.id == Transaction.appointment_id)
        .outerjoin(ServiceCatalog, _transaction_service_catalog_join())
        .where(
            _appt_revenue_filters(
                req.start,
                req.end,
                req.company_id,
                req.staff_id,
                allowed_company_ids=allowed_company_ids,
                factual_at=req.factual_at,
            ),
            func.coalesce(Transaction.amount, 0) > 0,
            group_key.is_not(None),
        )
        .group_by(Transaction.appointment_id, group_key)
        .cte('visit_services')
    )
    per_visit = (
        select(visit_services.c.visit, func.count().label('services'))
        .group_by(visit_services.c.visit)
        .subquery()
    )
    totals = (
        await req.db.execute(
            select(
                func.count().label('visits'),
                func.coalesce(func.sum(case((per_visit.c.services >= 2, 1), else_=0)), 0).label('multi_visits'),
            ).select_from(per_visit)
        )
    ).one()
    first, second = visit_services.alias('first'), visit_services.alias('second')
    visits_count = func.count()
    pair_rows = (
        await req.db.execute(
            select(
                func.min(first.c.title).label('first_title'),
                func.min(first.c.service_id).label('first_id'),
                func.min(second.c.title).label('second_title'),
                func.min(second.c.service_id).label('second_id'),
                visits_count.label('visits'),
            )
            .select_from(
                first.join(
                    second,
                    and_(first.c.visit == second.c.visit, first.c.service_key < second.c.service_key),
                )
            )
            .group_by(first.c.service_key, second.c.service_key)
            .order_by(visits_count.desc(), first.c.service_key, second.c.service_key)
            .limit(COMBO_TABLE_LIMIT + 1)
        )
    ).all()
    visits = int(totals.visits or 0)
    multi_visits = int(totals.multi_visits or 0)
    rows = [
        {
            'pair': f"{_service_title(row.first_title, row.first_id)} + "
                    f"{_service_title(row.second_title, row.second_id)}",
            'visits': int(row.visits),
            'share': _ratio(int(row.visits), multi_visits, 100),
        }
        for row in pair_rows[:COMBO_TABLE_LIMIT]
    ]
    req.base['notes'].append({
        'kind': 'formula',
        'title': 'Как считаются пары',
        'text': (
            'Пара — две разные услуги в одном завершённом визите; визит учитывается в паре один раз, '
            'сколько бы раз ни повторялась услуга. Доля — от визитов с двумя и более услугами.'
        ),
    })
    if len(pair_rows) > COMBO_TABLE_LIMIT:
        req.base['notes'].append({
            'kind': 'info',
            'title': 'Показаны самые частые пары',
            'text': f'В таблице {COMBO_TABLE_LIMIT} пар с наибольшим числом визитов; остальные встречаются реже.',
        })
    req.base['cards'] = [
        card('Визитов с 2+ услугами', multi_visits, NUMBER_FORMAT),
        card('Доля таких визитов', _ratio(multi_visits, visits, 100), PERCENT_FORMAT),
    ]
    top = top_rows(req, rows, CHART_ROWS)
    req.base['charts'] = [
        chart(
            'combo_visits',
            'Самые частые пары услуг',
            'bar',
            [row['pair'] for row in top],
            [{'label': 'Визитов', 'data': [row['visits'] for row in top], 'format': NUMBER_FORMAT}],
        )
    ]
    req.base['tables'] = [
        table(
            'combos',
            'Комбинации услуг',
            [
                ('pair', 'Пара услуг', 'text'),
                ('visits', 'Визитов', NUMBER_FORMAT),
                ('share', 'Доля от визитов с 2+ услугами', PERCENT_FORMAT),
            ],
            rows,
            row_key='pair',
        )
    ]
    req.base['raw'] = {
        'pairs': rows,
        'visits_with_services': visits,
        'multi_service_visits': multi_visits,
    }
    return req.base
