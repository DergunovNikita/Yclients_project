"""Builders for the finance reports."""

from __future__ import annotations

from typing import Any

from dashboard_reports import _aggregate_daily, _period_starts
from dashboard_service import DateRange, _average_check_block, fetch_revenue_daily
from report_payload import MONEY_FORMAT, NUMBER_FORMAT, ReportRequest, card, chart, table


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
        for key in _period_starts(req.start, req.end, req.granularity)
    ]
    for row in periods:
        # A day with payments but no completed visits has no average check, not a zero one.
        row['average_check'] = row['revenue'] / row['appointments'] if row['appointments'] else None

    base['average_check_source_status'] = avg['source_status']
    base['missing_sources'] = sorted(avg['missing_components'] or [])
    base['notes'].append({'kind': 'formula', 'title': 'Средний чек общий', 'text': avg['formula']})
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
                ('period', 'Период', 'text'),
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
    base['raw'] = {'average_check': avg}
    return base
