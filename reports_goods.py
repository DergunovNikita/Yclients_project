"""Builders for the goods reports."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date
from typing import Any

from sqlalchemy import and_, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from dashboard_reports import _period_key, _period_starts
from dashboard_service import (
    _appt_revenue_filters,
    _coerce_date,
    _financial_appointment_branches_union,
    _goods_paid_filters,
    _goods_revenue_filters,
    _physical_account_condition,
    fetch_paid_goods_rows,
    financial_appointment_match_condition,
)
from models import AccountCatalog, Appointment, Company, FinancialTransaction, GoodTransaction
from report_payload import (
    DECIMAL_FORMAT,
    MONEY_FORMAT,
    NUMBER_FORMAT,
    PERCENT_FORMAT,
    ReportRequest,
    card,
    chart,
    table,
    top_rows,
)

NO_SELLER = 'Без продавца'
TOP_GOODS_IN_CHART = 12


def _title_key(title: str) -> str:
    """Same normalization as the service group key: case and «ё» do not split one product."""
    return title.strip().lower().replace('ё', 'е')


def _percent(part: float, whole: float) -> float | None:
    return round(part / whole * 100, 2) if whole else None


def _goods_payment_conditions(req: ReportRequest, *, by_seller: bool) -> list[Any]:
    """The goods payments the Overview sums as goods revenue.

    The staff filter of this report picks visits for the conversion, so the visit-linked payments are
    read without a seller filter and the visit side carries it (`by_seller=False`). Payments that belong
    to no visit have no such side: they follow the seller, like revenue and units (`fetch_paid_goods_rows`).
    """
    return [
        _goods_paid_filters(
            req.start,
            req.end,
            req.company_id,
            req.staff_id if by_seller else None,
            req.allowed_company_ids,
            req.factual_at,
        ),
        _physical_account_condition(),
    ]


def _visit_conditions(req: ReportRequest, *, with_staff: bool) -> Any:
    """Completed visits exactly as the Overview counts them."""
    return _appt_revenue_filters(
        req.start,
        req.end,
        req.company_id,
        req.staff_id if with_staff else None,
        allowed_company_ids=req.allowed_company_ids,
        factual_at=req.factual_at,
    )


async def _completed_visits_by_day(req: ReportRequest) -> dict[date, int]:
    stmt = (
        select(Appointment.date.label('day'), func.count(func.distinct(Appointment.id)).label('visits'))
        .where(_visit_conditions(req, with_staff=True))
        .group_by(Appointment.date)
    )
    return {_coerce_date(row.day): int(row.visits) for row in (await req.db.execute(stmt)).all()}


async def _goods_visits_by_day(req: ReportRequest) -> dict[date, int]:
    """Completed visits that carry a goods payment, per visit day.

    The payments come from a separate IN-subquery: put straight into the visit join, the goods filter's own
    «linked visit» EXISTS would correlate with the joined Appointment instead of its own.
    """
    goods_payment_ids = (
        select(FinancialTransaction.id)
        .select_from(FinancialTransaction)
        .outerjoin(
            AccountCatalog,
            and_(
                AccountCatalog.company_id == FinancialTransaction.company_id,
                AccountCatalog.account_id == FinancialTransaction.account_id,
            ),
        )
        .where(*_goods_payment_conditions(req, by_seller=False))
        .correlate(None)
    )
    branches = _financial_appointment_branches_union(
        lambda join_condition: (
            select(Appointment.id.label('appointment_id'), Appointment.date.label('day'))
            .select_from(FinancialTransaction)
            .join(Appointment, join_condition)
            .where(
                FinancialTransaction.id.in_(goods_payment_ids),
                _visit_conditions(req, with_staff=True),
            )
        )
    )
    stmt = select(branches.c.day, func.count(func.distinct(branches.c.appointment_id)).label('visits')).group_by(
        branches.c.day
    )
    return {_coerce_date(row.day): int(row.visits) for row in (await req.db.execute(stmt)).all()}


async def _unlinked_goods_sales(req: ReportRequest) -> tuple[int, float]:
    """Goods payments of the period with no completed visit in it: retail over the counter or a late payment."""
    linked_visit = (
        exists(
            select(1)
            .select_from(Appointment)
            .where(financial_appointment_match_condition(), _visit_conditions(req, with_staff=False))
        )
        .correlate(FinancialTransaction)
    )
    stmt = (
        select(func.count(FinancialTransaction.id), func.coalesce(func.sum(FinancialTransaction.amount), 0.0))
        .select_from(FinancialTransaction)
        .outerjoin(
            AccountCatalog,
            and_(
                AccountCatalog.company_id == FinancialTransaction.company_id,
                AccountCatalog.account_id == FinancialTransaction.account_id,
            ),
        )
        .where(*_goods_payment_conditions(req, by_seller=True), ~linked_visit)
    )
    count, amount = (await req.db.execute(stmt)).one()
    return int(count or 0), float(amount or 0)


async def _sold_units(req: ReportRequest) -> list[Any]:
    day = func.date(GoodTransaction.date)
    stmt = (
        select(
            GoodTransaction.company_id,
            GoodTransaction.good_id,
            GoodTransaction.good_title,
            day.label('day'),
            func.coalesce(func.sum(func.abs(func.coalesce(GoodTransaction.amount, 0.0))), 0.0).label('units'),
        )
        .where(
            _goods_revenue_filters(
                req.start, req.end, req.company_id, req.staff_id, req.allowed_company_ids, req.factual_at
            )
        )
        .group_by(GoodTransaction.company_id, GoodTransaction.good_id, GoodTransaction.good_title, day)
    )
    return (await req.db.execute(stmt)).all()


async def _company_titles(db: AsyncSession) -> dict[int, str]:
    return {int(row.id): row.title for row in (await db.execute(select(Company.id, Company.title))).all()}


def _merged_goods(
    unit_rows: list[Any], paid_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """One row per normalized title; units come from stock movements, revenue from payments."""
    stock_titles = {(row.company_id, row.good_id): row.good_title for row in unit_rows if row.good_title}
    goods: dict[str, dict[str, Any]] = defaultdict(
        lambda: {'variants': Counter(), 'branches': set(), 'sales_count': 0, 'units': 0.0, 'revenue': 0.0}
    )

    def entry(title: str, company_id: Any) -> dict[str, Any]:
        item = goods[_title_key(title)]
        item['variants'][title.strip()] += 1
        item['branches'].add(company_id)
        return item

    for row in unit_rows:
        item = entry(row.good_title or f"Товар {row.good_id or '—'}", row.company_id)
        item['units'] += float(row.units or 0)
    for row in paid_rows:
        title = (
            row['good_title']
            or stock_titles.get((row['company_id'], row['good_id']))
            or f"Товар {row['good_id'] or '—'}"
        )
        item = entry(title, row['company_id'])
        item['sales_count'] += 1
        item['revenue'] += row['amount']

    rows = [
        {
            # The most frequent spelling is shown; ties go to the alphabetically first.
            'title': min(item['variants'], key=lambda variant: (-item['variants'][variant], variant)),
            'branches': len(item['branches']),
            'sales_count': item['sales_count'],
            'units': item['units'],
            'revenue': item['revenue'],
        }
        for item in goods.values()
    ]
    return sorted(rows, key=lambda row: (-row['revenue'], row['title']))


def _seller_rows(paid_rows: list[dict[str, Any]], branch_titles: dict[int, str]) -> list[dict[str, Any]]:
    """Sellers keyed by person and branch; sales without a seller get one row per branch."""
    sellers: dict[str, dict[str, Any]] = {}
    for row in paid_rows:
        seller_key = f"{row['company_id']}:{row['master_id'] or 'none'}"
        item = sellers.setdefault(
            seller_key,
            {
                'seller_key': seller_key,
                'staff_id': row['master_id'],
                'staff_name': row['staff_name'] or NO_SELLER,
                'company_title': branch_titles.get(row['company_id'], '—'),
                'sales_count': 0,
                'revenue': 0.0,
            },
        )
        item['sales_count'] += 1
        item['revenue'] += row['amount']
    return sorted(sellers.values(), key=lambda item: (-item['revenue'], item['staff_name'], item['seller_key']))


def _period_rows(
    unit_rows: list[Any],
    paid_rows: list[dict[str, Any]],
    visits: dict[date, int],
    goods_visits: dict[date, int],
    start: date,
    end: date,
    granularity: str,
) -> list[dict[str, Any]]:
    """Every period of the window, quiet ones as zeros: comparison pairs periods by position."""
    periods: dict[str, dict[str, Any]] = defaultdict(
        lambda: {'revenue': 0.0, 'units': 0.0, 'sales_count': 0, 'completed_visits': 0, 'goods_visits': 0}
    )
    for key in _period_starts(start, end, granularity):
        periods[key]
    for row in unit_rows:
        periods[_period_key(_coerce_date(row.day), granularity)]['units'] += float(row.units or 0)
    for row in paid_rows:
        item = periods[_period_key(row['date'], granularity)]
        item['revenue'] += row['amount']
        item['sales_count'] += 1
    for day, count in visits.items():
        periods[_period_key(day, granularity)]['completed_visits'] += count
    for day, count in goods_visits.items():
        periods[_period_key(day, granularity)]['goods_visits'] += count
    return [
        {'period': period, **item, 'goods_share': _percent(item['goods_visits'], item['completed_visits'])}
        for period, item in sorted(periods.items())
    ]


async def build_goods_dynamics(req: ReportRequest) -> dict[str, Any]:
    """Goods sales: revenue, units and the share of visits that include a goods purchase.

    Revenue is `fetch_paid_goods_rows` (the Overview's goods revenue), units are the Overview's goods
    count, and the share's denominator is the Overview's completed visits, so all three reconcile with it.
    """
    base = req.base
    unit_rows = await _sold_units(req)
    paid_rows = await fetch_paid_goods_rows(
        req.db,
        req.start,
        req.end,
        req.company_id,
        req.staff_id,
        req.allowed_company_ids,
        req.factual_at,
    )
    visits = await _completed_visits_by_day(req)
    goods_visits = await _goods_visits_by_day(req)
    unlinked_count, unlinked_amount = await _unlinked_goods_sales(req)

    goods_rows = _merged_goods(unit_rows, paid_rows)
    seller_rows = _seller_rows(paid_rows, await _company_titles(req.db))
    period_rows = _period_rows(unit_rows, paid_rows, visits, goods_visits, req.start, req.end, req.granularity)

    total_revenue = sum(row['revenue'] for row in goods_rows)
    total_units = sum(row['units'] for row in goods_rows)
    completed_visits = sum(visits.values())
    visits_with_goods = sum(goods_visits.values())
    show_branches = len(req.allowed_company_ids or []) > 1

    base['cards'] = [
        card('Выручка товаров', total_revenue, MONEY_FORMAT),
        card('Единиц продано', total_units, NUMBER_FORMAT),
        card('Визитов с покупкой товара', visits_with_goods, NUMBER_FORMAT),
        card('Доля визитов с покупкой товара', _percent(visits_with_goods, completed_visits), PERCENT_FORMAT),
    ]
    top_goods = top_rows(req, goods_rows, TOP_GOODS_IN_CHART)
    labels = [row['period'] for row in period_rows]
    base['charts'] = [
        chart(
            'goods_revenue',
            'Товары по выручке',
            'bar',
            [row['title'] for row in top_goods],
            [{'label': 'Выручка', 'data': [row['revenue'] for row in top_goods], 'format': MONEY_FORMAT}],
        ),
        chart(
            'goods_dynamics',
            'Динамика продаж товаров',
            'line',
            labels,
            [
                {'label': 'Выручка', 'data': [row['revenue'] for row in period_rows], 'format': MONEY_FORMAT},
                {
                    'label': 'Единиц',
                    'data': [row['units'] for row in period_rows],
                    'format': NUMBER_FORMAT,
                    'axis': 'y1',
                },
            ],
            x_kind='time',
        ),
        chart(
            'goods_share_periods',
            'Доля визитов с покупкой товара',
            'line',
            labels,
            [
                {
                    'label': 'Доля визитов с покупкой товара',
                    'data': [row['goods_share'] for row in period_rows],
                    'format': PERCENT_FORMAT,
                }
            ],
            x_kind='time',
        ),
    ]
    goods_columns = [('title', 'Товар', 'text')]
    if show_branches:
        goods_columns.append(('branches', 'Филиалов', NUMBER_FORMAT))
    goods_columns += [
        ('sales_count', 'Продаж', NUMBER_FORMAT),
        ('units', 'Единиц', DECIMAL_FORMAT),
        ('revenue', 'Выручка', MONEY_FORMAT),
    ]
    base['tables'] = [
        table(
            'periods',
            'Динамика по периодам',
            [
                ('period', 'Период', 'text'),
                ('revenue', 'Выручка', MONEY_FORMAT),
                ('units', 'Единиц', DECIMAL_FORMAT),
                ('goods_visits', 'Визитов с товаром', NUMBER_FORMAT),
                ('completed_visits', 'Завершённые визиты', NUMBER_FORMAT),
                ('goods_share', 'Доля визитов с товаром', PERCENT_FORMAT),
            ],
            period_rows,
            row_key='period',
            x_kind='time',
        ),
        table('goods', 'Товары', goods_columns, goods_rows, row_key='title'),
        table(
            'goods_by_staff',
            'Продажи по сотрудникам',
            [
                ('staff_name', 'Продавец', 'text'),
                ('company_title', 'Филиал', 'text'),
                ('sales_count', 'Продаж', NUMBER_FORMAT),
                ('revenue', 'Выручка', MONEY_FORMAT),
            ],
            seller_rows,
            row_key='seller_key',
        ),
    ]
    base['notes'].append({
        'kind': 'info',
        'title': 'Как считаются периоды',
        'text': (
            'Выручка и единицы отнесены к дню продажи, доля визитов с товаром — ко дню визита. '
            'Товар, проданный в нескольких филиалах под одним названием, показан одной строкой.'
        ),
    })
    if req.staff_id is not None:
        base['notes'].append({
            'kind': 'info',
            'title': 'Фильтр по сотруднику',
            'text': (
                'Выручка и единицы — продажи этого сотрудника как продавца; доля визитов — завершённые визиты '
                'этого мастера, в которых что-то купили из товаров, кто бы ни продавал.'
            ),
        })
    if unlinked_count:
        base['notes'].append({
            'kind': 'info',
            'title': 'Продажи без визита',
            'text': (
                f'{unlinked_count} продаж товаров на {unlinked_amount:,.0f}'.replace(',', ' ')
                + ' ₽ не связаны с завершённым визитом периода: розничные продажи без записи '
                'или оплата визита вне периода. В долю визитов с покупкой они не входят.'
            ),
        })
    base['raw'] = {
        'completed_visits': completed_visits,
        'visits_with_goods': visits_with_goods,
        'unlinked_sales': {'count': unlinked_count, 'amount': unlinked_amount},
    }
    return base
