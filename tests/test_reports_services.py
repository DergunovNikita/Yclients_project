"""Service reports: services table, services by master, service combinations."""

import json
from datetime import date, datetime

import pytest
import pytest_asyncio

import dashboard_reports
import dashboard_service
import reports_services
from models import Appointment, Company, FinancialTransaction, Group, ServiceCatalog, Staff, Transaction

NOW = datetime(2026, 9, 15, 12, 0)
DAY = date(2026, 8, 10)
WINDOW = (date(2026, 8, 1), date(2026, 8, 31))


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(dashboard_reports, '_report_now', lambda: NOW)
    monkeypatch.setattr(dashboard_service, 'factual_now', lambda: NOW)


def _visit(visit_id, staff_id, attendance=1, external_id=None, day=DAY):
    return Appointment(
        id=visit_id,
        external_id=external_id if external_id is not None else 5000 + visit_id,
        company_id=1,
        staff_id=staff_id,
        date=day,
        datetime=datetime.combine(day, datetime.min.time()).replace(hour=12),
        attendance=attendance,
    )


def _line(line_id, visit_id, service_id, title, amount=1):
    return Transaction(
        id=line_id, appointment_id=visit_id, service_id=service_id, service_title=title, amount=amount, company_id=1
    )


def _payment(payment_id, visit_id, service_id, amount, day=DAY):
    # record_id is the visit's YClients (external) id, exactly as the sync stores it.
    return FinancialTransaction(
        id=payment_id,
        company_id=1,
        date=datetime.combine(day, datetime.min.time()).replace(hour=12),
        amount=amount,
        record_id=5000 + visit_id,
        sold_item_id=service_id,
        sold_item_type='service',
        master_id=1,
    )


@pytest_asyncio.fixture
async def seeded(async_session):
    """Visits (completed unless noted):
    1 Anna:   Cut x2 lines (duplicate), Beard, Wax (title only on the visit line, no catalog row)
    2 Boris:  Cut, Beard
    3 no master: Cut, Wax
    4 Anna:   Cut only
    5 Boris:  Cut, not completed (must not count anywhere)
    """
    async_session.add_all([
        Group(id=1, title='G'),
        Company(id=1, title='Salon', group_id=1),
        Staff(id=1, name='Anna', position='Барбер', company_id=1),
        Staff(id=2, name='Boris', position='Барбер', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all([
        ServiceCatalog(company_id=1, service_id=10, title='Cut', is_active=True, updated_at=NOW),
        ServiceCatalog(company_id=1, service_id=11, title='Beard', is_active=True, updated_at=NOW),
        _visit(1, 1),
        _visit(2, 2),
        _visit(3, None),
        _visit(4, 1),
        _visit(5, 2, attendance=0),
    ])
    await async_session.flush()
    async_session.add_all([
        _line(1, 1, 10, 'Cut'),
        _line(2, 1, 10, 'Cut'),
        _line(3, 1, 11, 'Beard'),
        _line(4, 1, 12, 'Wax'),
        _line(5, 2, 10, 'Cut'),
        _line(6, 2, 11, 'Beard'),
        _line(7, 3, 10, 'Cut'),
        _line(8, 3, 12, 'Wax'),
        _line(9, 4, 10, 'Cut'),
        _line(10, 5, 10, 'Cut'),
        _payment(1, 1, 10, 1000.0),
        _payment(2, 1, 11, 500.0),
        _payment(3, 1, 12, 700.0),
        _payment(4, 2, 10, 900.0),
        _payment(5, 2, 11, 400.0),
        _payment(6, 3, 10, 800.0),
        _payment(7, 3, 12, 600.0),
        _payment(8, 4, 10, 1100.0),
        _payment(9, 5, 10, 5000.0),
    ])
    await async_session.commit()
    return async_session


async def _report(db, report_id, staff_id=None):
    return await dashboard_reports.fetch_report_data(db, report_id, *WINDOW, company_id=1, staff_id=staff_id)


def _cards(data):
    return {item['label']: item['value'] for item in data['cards']}


def _table(data, table_id):
    return next(item for item in data['tables'] if item['id'] == table_id)


@pytest.mark.asyncio
async def test_services_table_lists_every_service_with_price_and_share(seeded):
    data = await _report(seeded, 'avg_check_by_service')
    rows = {row['title']: row for row in _table(data, 'services')['rows']}

    assert set(rows) == {'Cut', 'Beard', 'Wax'}
    assert rows['Cut']['sold'] == 5 and rows['Cut']['revenue'] == 3800.0
    assert rows['Cut']['avg_price'] == pytest.approx(760.0)
    assert rows['Beard']['sold'] == 2 and rows['Beard']['revenue'] == 900.0
    assert rows['Wax']['sold'] == 2 and rows['Wax']['revenue'] == 1300.0
    assert sum(row['revenue_share'] for row in rows.values()) == pytest.approx(100.0)
    assert rows['Cut']['revenue_share'] == pytest.approx(3800 / 6000 * 100)
    assert _table(data, 'services')['row_key'] == 'title'
    assert _table(data, 'extra_services')['row_key'] == 'title'
    assert [row['title'] for row in _table(data, 'services')['rows']] == ['Cut', 'Wax', 'Beard']
    assert data['charts'][0]['labels'] == ['Cut', 'Wax', 'Beard']
    assert _cards(data) == {'Услуг оказано': 9, 'Выручка услуг': 6000.0, 'Средняя цена услуги': pytest.approx(6000 / 9)}


@pytest.mark.asyncio
async def test_services_table_is_not_truncated(async_session):
    async_session.add_all([Group(id=1, title='G'), Company(id=1, title='Salon', group_id=1)])
    await async_session.flush()
    async_session.add_all([_visit(1, None)])
    await async_session.flush()
    for index in range(40):
        async_session.add(_line(index + 1, 1, 100 + index, f'Service {index:02}'))
        async_session.add(_payment(index + 1, 1, 100 + index, float(index + 1)))
    await async_session.commit()

    data = await _report(async_session, 'avg_check_by_service')

    assert len(_table(data, 'services')['rows']) == 40
    assert len(data['charts'][0]['labels']) == 12
    assert 'Уникальных услуг' not in _cards(data)


@pytest.mark.asyncio
async def test_comparison_chart_carries_every_service_so_a_riser_still_has_a_previous_point(async_session):
    """A service in this window's top 12 that ranked 20th last window must still get its previous value."""
    async_session.add_all([Group(id=1, title='G'), Company(id=1, title='Salon', group_id=1)])
    await async_session.flush()
    july = date(2026, 7, 10)
    for index in range(20):
        # August: Service 19 earns most. July: it earned least.
        for visit_id, day, revenue in ((index + 1, DAY, index + 1), (index + 101, july, 20 - index)):
            async_session.add(_visit(visit_id, None, day=day))
            await async_session.flush()
            async_session.add(_line(visit_id, visit_id, 100 + index, f'Service {index:02}'))
            async_session.add(_payment(visit_id, visit_id, 100 + index, float(revenue), day=day))
    await async_session.commit()

    data = await dashboard_reports.fetch_report_data(
        async_session, 'avg_check_by_service', *WINDOW, company_id=1,
        compare_start=date(2026, 7, 1), compare_end=date(2026, 7, 31),
    )

    current = next(item for item in data['charts'] if item['id'] == 'service_revenue')
    previous = next(item for item in data['comparison']['charts'] if item['id'] == 'service_revenue')
    assert len(current['labels']) == 12 and current['labels'][0] == 'Service 19'
    assert len(previous['labels']) == 20
    assert previous['datasets'][0]['data'][previous['labels'].index('Service 19')] == 1.0


@pytest.mark.asyncio
async def test_payment_titled_only_by_visit_line_lands_on_that_title(seeded):
    """Wax has no catalog row and its payments carry the visit's external id: the title must come from the line."""
    top = await dashboard_service.fetch_top_services(
        seeded, *WINDOW, company_id=1, limit=None, factual_at=NOW
    )
    by_title = {row['title']: row for row in top}

    assert set(by_title) == {'Cut', 'Beard', 'Wax'}
    assert by_title['Wax']['revenue'] == 1300.0
    assert by_title['Wax']['sold'] == 2
    assert all(row['sold'] > 0 for row in top)


@pytest.mark.asyncio
async def test_services_reconcile_with_the_overview(seeded):
    overview = await dashboard_service.fetch_summary(
        seeded, *WINDOW, company_id=1, include_appointments_breakdown=False, factual_at=NOW
    )
    for report_id, revenue_card in (('avg_check_by_service', 'Выручка услуг'), ('service_staff_profit', 'Выручка услуг')):
        data = await _report(seeded, report_id)
        cards = _cards(data)
        assert cards[revenue_card] == overview['average_check']['service_revenue'] == 6000.0
        assert cards['Услуг оказано'] == overview['revenue']['service_count'] == 9.0


@pytest.mark.asyncio
async def test_services_report_respects_the_staff_filter(seeded):
    data = await _report(seeded, 'avg_check_by_service', staff_id=1)
    rows = {row['title']: row for row in _table(data, 'services')['rows']}

    assert set(rows) == {'Cut', 'Beard', 'Wax'}
    assert rows['Cut']['sold'] == 3 and rows['Cut']['revenue'] == 2100.0
    assert _cards(data)['Выручка услуг'] == 2100.0 + 500.0 + 700.0


@pytest.mark.asyncio
async def test_staff_services_table_is_a_long_table_with_unique_keys(seeded):
    data = await _report(seeded, 'service_staff_profit')
    staff_table = _table(data, 'staff_services')
    rows = staff_table['rows']

    assert staff_table['row_key'] == 'key'
    assert len({row['key'] for row in rows}) == len(rows) == 7
    assert [(row['staff_name'], row['title']) for row in rows] == [
        ('Anna', 'Cut'),
        ('Anna', 'Wax'),
        ('Anna', 'Beard'),
        ('Boris', 'Cut'),
        ('Boris', 'Beard'),
        ('Без мастера', 'Cut'),
        ('Без мастера', 'Wax'),
    ]
    anna_cut = rows[0]
    assert anna_cut['company_title'] == 'Salon'
    assert anna_cut['sold'] == 3 and anna_cut['revenue'] == 2100.0
    assert anna_cut['avg_price'] == pytest.approx(700.0)
    assert sum(row['revenue'] for row in rows) == 6000.0
    assert _cards(data) == {'Мастеров': 2, 'Услуг оказано': 9, 'Выручка услуг': 6000.0}


@pytest.mark.asyncio
async def test_staff_services_filter_keeps_only_that_master(seeded):
    data = await _report(seeded, 'service_staff_profit', staff_id=2)
    rows = _table(data, 'staff_services')['rows']

    assert {row['staff_name'] for row in rows} == {'Boris'}
    assert _cards(data)['Выручка услуг'] == 1300.0


@pytest.mark.asyncio
async def test_staff_grouping_does_not_change_the_totals(seeded):
    plain = await dashboard_service.fetch_top_services(seeded, *WINDOW, company_id=1, limit=None, factual_at=NOW)
    split = await dashboard_service.fetch_top_services(
        seeded, *WINDOW, company_id=1, limit=None, factual_at=NOW, group_by_staff=True
    )

    assert sum(row['revenue'] for row in plain) == sum(row['revenue'] for row in split)
    assert sum(row['sold'] for row in plain) == sum(row['sold'] for row in split)


@pytest.mark.asyncio
async def test_combos_count_a_visit_once_per_pair(seeded):
    data = await _report(seeded, 'service_combos')
    rows = {row['pair']: row for row in _table(data, 'combos')['rows']}

    # Visit 1 repeats Cut on two lines: it still forms one Beard+Cut and one Cut+Wax pair.
    assert rows == {
        'Beard + Cut': {'pair': 'Beard + Cut', 'visits': 2, 'share': pytest.approx(2 / 3 * 100)},
        'Cut + Wax': {'pair': 'Cut + Wax', 'visits': 2, 'share': pytest.approx(2 / 3 * 100)},
        'Beard + Wax': {'pair': 'Beard + Wax', 'visits': 1, 'share': pytest.approx(1 / 3 * 100)},
    }
    assert _table(data, 'combos')['row_key'] == 'pair'
    # Visits 1, 2, 3 have two or more services; visit 4 has one; visit 5 is not completed.
    assert _cards(data) == {'Визитов с 2+ услугами': 3, 'Доля таких визитов': pytest.approx(3 / 4 * 100)}


@pytest.mark.asyncio
async def test_combos_respect_the_staff_filter(seeded):
    data = await _report(seeded, 'service_combos', staff_id=1)

    assert [row['pair'] for row in _table(data, 'combos')['rows']] == ['Beard + Cut', 'Beard + Wax', 'Cut + Wax']
    assert _cards(data) == {'Визитов с 2+ услугами': 1, 'Доля таких визитов': pytest.approx(50.0)}


@pytest.mark.asyncio
async def test_combos_table_is_capped_and_says_so(async_session):
    async_session.add_all([Group(id=1, title='G'), Company(id=1, title='Salon', group_id=1)])
    await async_session.flush()
    async_session.add(_visit(1, None))
    await async_session.flush()
    for index in range(12):
        async_session.add(_line(index + 1, 1, 100 + index, f'Service {index:02}'))
    await async_session.commit()

    data = await _report(async_session, 'service_combos')

    assert len(_table(data, 'combos')['rows']) == reports_services.COMBO_TABLE_LIMIT
    assert any(note['title'] == 'Показаны самые частые пары' for note in data['notes'])


@pytest.mark.asyncio
async def test_combos_report_is_counts_only(seeded):
    assert dashboard_reports.report_requires_financials('service_combos') is False
    assert dashboard_reports.REPORT_REGISTRY['service_combos'].requires_financials is False
    data = await _report(seeded, 'service_combos')

    assert all(item['format'] != 'money' for item in data['cards'])
    assert all(
        column['format'] != 'money' for table in data['tables'] for column in table['columns']
    )
    assert all(
        dataset.get('format') != 'money' for item in data['charts'] for dataset in item['datasets']
    )
    # No money-ish key or amount anywhere in the payload, raw included.
    payload = json.dumps(data, ensure_ascii=False)
    for forbidden in ('revenue', 'amount', 'money', 'price'):
        assert forbidden not in payload
    for amount in ('1000', '5000'):
        assert amount not in json.dumps(data['raw'])
