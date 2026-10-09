"""financial_overview, goods_dynamics and nps_dashboard builders."""

from datetime import date, datetime

import pytest

import dashboard_reports
import dashboard_service
from models import (
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Group,
    Staff,
)

JAN = (date(2025, 1, 1), date(2025, 1, 31))


async def _report(session, report_id, window=JAN, **kwargs):
    kwargs.setdefault('allowed_company_ids', [1, 2])
    return await dashboard_reports.fetch_report_data(session, report_id, *window, **kwargs)


def _cards(report):
    return {card['label']: card['value'] for card in report['cards']}


def _table(report, table_id):
    return next(table for table in report['tables'] if table['id'] == table_id)


def _service_payment(payment_id, company_id, appointment_id, amount, moment):
    return FinancialTransaction(
        id=payment_id,
        company_id=company_id,
        record_id=appointment_id,
        sold_item_type='service',
        master_id=None,
        amount=amount,
        date=moment,
    )


def _goods_payment(payment_id, company_id, goods_transaction_id, appointment_id, amount, moment):
    return FinancialTransaction(
        id=payment_id,
        company_id=company_id,
        record_id=appointment_id,
        sold_item_id=goods_transaction_id,
        sold_item_type='goods_transaction',
        master_id=None,
        amount=amount,
        date=moment,
    )


def _stock_sale(sale_id, company_id, good_id, title, units, seller_id, moment):
    return GoodTransaction(
        id=sale_id,
        company_id=company_id,
        type_id=1,
        good_id=good_id,
        good_title=title,
        amount=-units,
        master_id=seller_id,
        date=moment,
    )


async def _seed(session):
    """Two branches, three completed January visits (two bought goods), one no-show, one February visit.

    January goods: lipstick paid on two visits (one title spelled with other case and a trailing space in the
    second branch), a hedgehog gel sold over the counter in both branches (no visit behind the payment).
    """
    session.add_all([
        Group(id=1, title='G'),
        Company(id=1, title='North', group_id=1),
        Company(id=2, title='South', group_id=1),
    ])
    await session.flush()
    session.add_all([
        Staff(id=1, name='Anna', position='Барбер', company_id=1),
        Staff(id=2, name='Boris', position='Барбер', company_id=2),
        Client(id=1, name='C1', company_id=1),
        Client(id=2, name='C2', company_id=2),
        Client(id=3, name='C3', company_id=1),
    ])
    await session.flush()
    session.add_all([
        Appointment(id=1, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 10), attendance=1),
        Appointment(id=2, company_id=2, staff_id=2, client_id=2, date=date(2025, 1, 11), attendance=1),
        Appointment(id=3, company_id=1, staff_id=1, client_id=3, date=date(2025, 1, 12), attendance=1),
        Appointment(id=4, company_id=1, staff_id=1, client_id=1, date=date(2025, 1, 13), attendance=-1),
        Appointment(id=5, company_id=1, staff_id=1, client_id=1, date=date(2025, 2, 5), attendance=1),
    ])
    await session.flush()
    jan = datetime(2025, 1, 10, 12)
    session.add_all([
        _service_payment(1, 1, 1, 1000.0, jan),
        _service_payment(2, 2, 2, 2000.0, datetime(2025, 1, 11, 12)),
        _service_payment(3, 1, 3, 800.0, datetime(2025, 1, 12, 12)),
        _service_payment(4, 1, 5, 1000.0, datetime(2025, 2, 5, 12)),
        _stock_sale(1, 1, 10, 'Помада', 2, 1, jan),
        _stock_sale(2, 2, 77, 'ПОМАДА ', 1, 2, datetime(2025, 1, 11, 12)),
        _stock_sale(3, 1, 11, 'Гель Ёж', 1, 1, datetime(2025, 1, 20, 12)),
        _stock_sale(4, 2, 78, 'гель еж', 1, 2, datetime(2025, 1, 21, 12)),
        _goods_payment(5, 1, 1, 1, 500.0, jan),
        _goods_payment(6, 2, 2, 2, 300.0, datetime(2025, 1, 11, 12)),
        _goods_payment(7, 1, 3, None, 200.0, datetime(2025, 1, 20, 12)),
        _goods_payment(8, 2, 4, None, 100.0, datetime(2025, 1, 21, 12)),
    ])
    await session.commit()


@pytest.mark.asyncio
async def test_financial_cards_equal_the_overview(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'financial_overview', (JAN[0], date(2025, 2, 28)), granularity='month')
    overview = await dashboard_service.fetch_summary(
        async_session, JAN[0], date(2025, 2, 28), allowed_company_ids=[1, 2], include_appointments_breakdown=False
    )

    cards = _cards(report)
    assert cards['Выручка'] == overview['revenue']['total'] == 5900.0
    assert cards['Услуги'] == overview['revenue']['service_revenue'] == 4800.0
    assert cards['Товары'] == overview['revenue']['goods_revenue'] == 1100.0
    assert cards['Пополнения'] == overview['revenue']['topup_revenue'] == 0.0
    assert cards['Завершённые визиты'] == overview['revenue']['appointments'] == 4
    assert cards['Средний чек'] == overview['average_check']['total'] == 5900.0 / 4
    assert cards['Уникальные клиенты'] == overview['revenue']['unique_clients'] == 3


@pytest.mark.asyncio
async def test_financial_periods_carry_the_cards_average_check(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'financial_overview', (JAN[0], date(2025, 2, 28)), granularity='month')

    rows = _table(report, 'periods')['rows']
    assert [row['period'] for row in rows] == ['2025-01-01', '2025-02-01']
    assert [row['average_check'] for row in rows] == [4900.0 / 3, 1000.0]
    # The card is the visit-weighted mean of the periods: the table cannot contradict it.
    weighted = sum(row['revenue'] for row in rows) / sum(row['appointments'] for row in rows)
    assert weighted == pytest.approx(_cards(report)['Средний чек'])
    chart = next(item for item in report['charts'] if item['id'] == 'avg_check_periods')
    assert chart['x_kind'] == 'time'
    assert chart['datasets'][0]['data'] == [row['average_check'] for row in rows]
    assert _table(report, 'periods')['row_key'] == 'period'


@pytest.mark.asyncio
async def test_financial_period_without_visits_has_no_average_check(async_session):
    await _seed(async_session)
    async_session.add(_goods_payment(9, 1, 3, None, 50.0, datetime(2025, 3, 3, 12)))
    await async_session.commit()

    report = await _report(
        async_session, 'financial_overview', (date(2025, 3, 1), date(2025, 3, 31)), granularity='month'
    )

    row = _table(report, 'periods')['rows'][0]
    assert row['revenue'] == 50.0 and row['appointments'] == 0
    assert row['average_check'] is None
    assert _cards(report)['Средний чек'] == 0.0


@pytest.mark.asyncio
async def test_financial_and_goods_periods_keep_quiet_days_on_the_axis(async_session):
    """Comparison pairs periods by position, so a day without activity must be a zero row, not a missing one."""
    await _seed(async_session)
    window = (date(2025, 1, 9), date(2025, 1, 13))

    financial = await _report(async_session, 'financial_overview', window, granularity='day')
    goods = await _report(async_session, 'goods_dynamics', window, granularity='day')

    expected = ['2025-01-09', '2025-01-10', '2025-01-11', '2025-01-12', '2025-01-13']
    rows = _table(financial, 'periods')['rows']
    assert [row['period'] for row in rows] == expected
    assert [row['revenue'] for row in rows] == [0.0, 1500.0, 2300.0, 800.0, 0.0]
    assert [row['average_check'] for row in rows] == [None, 1500.0, 2300.0, 800.0, None]
    chart = next(item for item in financial['charts'] if item['id'] == 'revenue_periods')
    assert chart['labels'] == expected
    goods_rows = _table(goods, 'periods')['rows']
    assert [row['period'] for row in goods_rows] == expected
    assert [row['revenue'] for row in goods_rows] == [0.0, 500.0, 300.0, 0.0, 0.0]
    assert [row['goods_share'] for row in goods_rows] == [None, 100.0, 100.0, 0.0, None]


@pytest.mark.asyncio
async def test_financial_payload_ships_neither_top_services_nor_the_daily_series(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'financial_overview', granularity='day')

    assert [item['id'] for item in report['charts']] == ['revenue_periods', 'avg_check_periods']
    assert [item['id'] for item in report['tables']] == ['periods']
    assert set(report['raw']) == {'average_check'}
    assert report['notes'][0]['kind'] == 'formula'


@pytest.mark.asyncio
async def test_goods_merge_titles_across_branches(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'goods_dynamics', granularity='month')

    goods = _table(report, 'goods')
    assert goods['row_key'] == 'title'
    assert [column['key'] for column in goods['columns']] == ['title', 'branches', 'sales_count', 'units', 'revenue']
    by_title = {row['title'].lower().replace('ё', 'е'): row for row in goods['rows']}
    assert len(goods['rows']) == 2
    assert by_title['помада']['branches'] == 2
    assert by_title['помада']['units'] == 3.0
    assert by_title['помада']['revenue'] == 800.0
    assert by_title['помада']['sales_count'] == 2
    assert by_title['гель еж']['branches'] == 2
    assert by_title['гель еж']['revenue'] == 300.0
    # Highest revenue first.
    assert [row['revenue'] for row in goods['rows']] == [800.0, 300.0]


@pytest.mark.asyncio
async def test_goods_branch_column_is_hidden_for_one_branch(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'goods_dynamics', company_id=1, allowed_company_ids=[1])

    assert 'branches' not in [column['key'] for column in _table(report, 'goods')['columns']]


@pytest.mark.asyncio
async def test_goods_cards_reconcile_with_the_overview(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'goods_dynamics')
    overview = await dashboard_service.fetch_summary(
        async_session, *JAN, allowed_company_ids=[1, 2], include_appointments_breakdown=False
    )

    cards = _cards(report)
    assert cards['Выручка товаров'] == overview['revenue']['goods_revenue'] == 1100.0
    assert cards['Единиц продано'] == overview['revenue']['goods_count'] == 5.0
    assert report['raw']['completed_visits'] == overview['revenue']['appointments'] == 3


@pytest.mark.asyncio
async def test_goods_share_counts_distinct_completed_visits_with_a_goods_payment(async_session):
    await _seed(async_session)
    # A second goods payment on visit 1 and one on the no-show visit change nothing.
    async_session.add_all([
        _goods_payment(20, 1, 1, 1, 10.0, datetime(2025, 1, 10, 13)),
        _goods_payment(21, 1, 1, 4, 10.0, datetime(2025, 1, 13, 12)),
    ])
    await async_session.commit()

    report = await _report(async_session, 'goods_dynamics', granularity='month')

    cards = _cards(report)
    assert cards['Визитов с покупкой товара'] == 2
    assert cards['Доля визитов с покупкой товара'] == pytest.approx(66.67)
    row = _table(report, 'periods')['rows'][0]
    assert (row['goods_visits'], row['completed_visits'], row['goods_share']) == (2, 3, 66.67)
    assert row['revenue'] == 1120.0


@pytest.mark.asyncio
async def test_goods_note_names_sales_without_a_visit(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'goods_dynamics')

    assert report['raw']['unlinked_sales'] == {'count': 2, 'amount': 300.0}
    note = next(item for item in report['notes'] if item['title'] == 'Продажи без визита')
    assert note['text'].startswith('2 продаж товаров на 300 ₽')


@pytest.mark.asyncio
async def test_goods_staff_filter_picks_visits_of_the_master(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'goods_dynamics', staff_id=1, allowed_company_ids=None)

    cards = _cards(report)
    # Anna's visits are 1 and 3; only visit 1 carries goods.
    assert report['raw']['completed_visits'] == 2
    assert cards['Визитов с покупкой товара'] == 1
    assert cards['Доля визитов с покупкой товара'] == 50.0
    assert cards['Выручка товаров'] == 700.0
    assert any(item['title'] == 'Фильтр по сотруднику' for item in report['notes'])


@pytest.mark.asyncio
async def test_goods_sales_without_a_visit_follow_the_seller_filter(async_session):
    """A personal role is clamped to its own staff row: branch-wide over-the-counter sales are not its figure."""
    await _seed(async_session)

    report = await _report(async_session, 'goods_dynamics', staff_id=1, allowed_company_ids=None)

    # Anna sold the 200 gel; Boris's 100 gel in the other branch is not hers.
    assert report['raw']['unlinked_sales'] == {'count': 1, 'amount': 200.0}
    note = next(item for item in report['notes'] if item['title'] == 'Продажи без визита')
    assert note['text'].startswith('1 продаж товаров на 200 ₽')
    assert '300' not in note['text']


@pytest.mark.asyncio
async def test_goods_sellers_are_keyed_by_person_and_branch(async_session):
    await _seed(async_session)
    # Sale without a seller in each branch: two rows, not one merged "no seller".
    async_session.add_all([
        _stock_sale(5, 1, 10, 'Помада', 1, None, datetime(2025, 1, 25, 12)),
        _stock_sale(6, 2, 77, 'Помада', 1, None, datetime(2025, 1, 25, 12)),
        _goods_payment(30, 1, 5, None, 40.0, datetime(2025, 1, 25, 12)),
        _goods_payment(31, 2, 6, None, 60.0, datetime(2025, 1, 25, 12)),
    ])
    await async_session.commit()

    report = await _report(async_session, 'goods_dynamics')

    sellers = _table(report, 'goods_by_staff')
    assert sellers['row_key'] == 'seller_key'
    keys = [row['seller_key'] for row in sellers['rows']]
    assert len(keys) == len(set(keys)) == 4
    by_key = {row['seller_key']: row for row in sellers['rows']}
    assert by_key['1:1']['staff_id'] == 1 and by_key['1:1']['company_title'] == 'North'
    assert by_key['2:2']['staff_name'] == 'Boris'
    assert by_key['1:none']['staff_name'] == 'Без продавца' and by_key['1:none']['revenue'] == 40.0
    assert by_key['2:none']['company_title'] == 'South' and by_key['2:none']['revenue'] == 60.0


@pytest.mark.asyncio
async def test_goods_report_has_no_count_style_cards(async_session):
    await _seed(async_session)

    report = await _report(async_session, 'goods_dynamics')

    assert [card['label'] for card in report['cards']] == [
        'Выручка товаров',
        'Единиц продано',
        'Визитов с покупкой товара',
        'Доля визитов с покупкой товара',
    ]
    assert all(item['x_kind'] == 'time' for item in report['charts'] if item['id'] != 'goods_revenue')


async def _seed_reviews(session):
    session.add_all([
        Group(id=1, title='G'),
        Company(id=1, title='North', group_id=1),
        Company(id=2, title='South', group_id=1),
    ])
    await session.flush()
    session.add_all([
        Staff(id=1, name='Anna', position='Барбер', company_id=1),
        Staff(id=2, name='Boris', position='Барбер', company_id=2),
    ])
    await session.flush()
    jan_5 = datetime(2025, 1, 5, 10)
    jan_10 = datetime(2025, 1, 10, 10)
    session.add_all([
        Comment(id=1, company_id=1, master_id=1, record_id=1, rating=5, text='ok', date=jan_5),
        # The same review again, now without a master: the duplicate is dropped.
        Comment(id=2, company_id=1, master_id=None, record_id=1, rating=5, text='ok', date=jan_5),
        Comment(id=3, company_id=1, master_id=1, record_id=3, rating=2, text='bad', date=jan_10),
        Comment(id=4, company_id=2, master_id=2, record_id=4, rating=3, text=None, date=jan_10),
        Comment(id=5, company_id=1, master_id=None, record_id=None, rating=4, text='  ', date=datetime(2025, 1, 20)),
        # A branch review with no twin stays, master or not.
        Comment(id=6, company_id=1, master_id=None, record_id=6, rating=1, text='awful', date=datetime(2025, 1, 22)),
        # No rating: not a review of anything.
        Comment(id=7, company_id=1, master_id=1, record_id=7, rating=0, text='?', date=jan_10),
    ])
    await session.commit()


@pytest.mark.asyncio
async def test_reviews_cards_and_dedupe(async_session):
    await _seed_reviews(async_session)

    report = await _report(async_session, 'nps_dashboard')

    assert report['source_status'] == 'ready'
    assert report['missing_sources'] == []
    cards = _cards(report)
    assert list(cards) == ['Отзывов', 'Средняя оценка', 'Доля оценок ниже 5', 'Отзывов с текстом']
    assert cards['Отзывов'] == 5
    assert cards['Средняя оценка'] == 3.0
    assert cards['Доля оценок ниже 5'] == 80.0
    assert cards['Отзывов с текстом'] == 3
    distribution = next(item for item in report['charts'] if item['id'] == 'ratings')
    assert distribution['datasets'][0]['data'] == [1, 1, 1, 1, 1]
    assert not any('telegram' in str(item).lower() for item in (report['cards'], report['notes']))


@pytest.mark.asyncio
async def test_reviews_negative_table_is_newest_first_with_a_tiebreaker(async_session):
    await _seed_reviews(async_session)

    report = await _report(async_session, 'nps_dashboard')

    negative = _table(report, 'negative_reviews')
    # A list of events: its rows have no counterpart in another window, so the table is not comparable.
    assert 'row_key' not in negative
    # Ids 3 and 4 share a date: the higher id goes first.
    assert [row['id'] for row in negative['rows']] == [6, 4, 3]
    assert negative['rows'][0]['staff_name'] == 'Без мастера'
    assert 'company_title' in [column['key'] for column in negative['columns']]


@pytest.mark.asyncio
async def test_reviews_respect_the_staff_filter(async_session):
    await _seed_reviews(async_session)

    report = await _report(async_session, 'nps_dashboard', staff_id=1, allowed_company_ids=None)

    cards = _cards(report)
    assert cards['Отзывов'] == 2
    assert cards['Средняя оценка'] == 3.5
    assert cards['Отзывов с текстом'] == 2
    assert [row['id'] for row in _table(report, 'negative_reviews')['rows']] == [3]
    assert report['calculation_scope']['kind'] == 'staff'
    assert report['calculation_scope']['mode'] == 'personal'


@pytest.mark.asyncio
async def test_reviews_without_data_give_no_average(async_session):
    await _seed_reviews(async_session)

    report = await _report(async_session, 'nps_dashboard', (date(2024, 1, 1), date(2024, 1, 31)))

    cards = _cards(report)
    assert cards['Отзывов'] == 0
    assert cards['Средняя оценка'] is None
    assert cards['Доля оценок ниже 5'] is None
    assert _table(report, 'negative_reviews')['rows'] == []
