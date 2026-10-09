import json
from datetime import date, datetime

import pytest

import dashboard_reports
import dashboard_service
from models import Appointment, Company, Group, Staff

JUNE = (date(2026, 6, 1), date(2026, 6, 30))


@pytest.fixture(autouse=True)
def local_record_counts(monkeypatch):
    """No YClients credentials in tests: the breakdown falls back to the local copy, as it does for real branches."""

    async def unavailable(*args, **kwargs):
        raise dashboard_service.yclients_analytics.YClientsAnalyticsError('no upstream in tests')

    monkeypatch.setattr(dashboard_service.yclients_analytics, 'fetch_record_stats', unavailable)


async def seed(session, appointments, *, timezones=(None, None)):
    """Two branches, each with a master called 'Anna' (ids 1 and 2) and a master 'Boris' (id 3, branch 1)."""
    session.add(Group(id=1, title='G'))
    session.add(Company(id=1, title='Alpha', group_id=1, timezone=timezones[0]))
    session.add(Company(id=2, title='Beta', group_id=1, timezone=timezones[1]))
    await session.flush()
    session.add_all([
        Staff(id=1, name='Anna', company_id=1, fired=0),
        Staff(id=2, name='Anna', company_id=2, fired=0),
        Staff(id=3, name='Boris', company_id=1, fired=0),
    ])
    session.add_all(Appointment(id=index, **fields) for index, fields in enumerate(appointments, start=1))
    await session.commit()


def visit(day, company_id=1, staff_id=1, attendance=1, created_user_id=0, moment=None, seance_length=3600):
    return {
        'company_id': company_id,
        'staff_id': staff_id,
        'date': day,
        'datetime': moment or datetime(day.year, day.month, day.day, 7, 0),
        'attendance': attendance,
        'created_user_id': created_user_id,
        'seance_length': seance_length,
    }


async def report(session, report_id, window=JUNE, **kwargs):
    return await dashboard_reports.fetch_report_data(session, report_id, *window, **kwargs)


def cards(data):
    return {item['label']: item['value'] for item in data['cards']}


def table(data, table_id):
    return next(item for item in data['tables'] if item['id'] == table_id)


def chart(data, chart_id):
    return next(item for item in data['charts'] if item['id'] == chart_id)


# --- peak_load ---------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peak_load_shows_branch_local_hours_not_utc(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), moment=datetime(2026, 6, 1, 7, 0)),  # Monday 07:00 UTC -> 10:00 Moscow
        visit(date(2026, 6, 1), moment=datetime(2026, 6, 1, 22, 30)),  # Monday 22:30 UTC -> Tuesday 01:30 Moscow
        visit(date(2026, 6, 1), moment=datetime(2026, 6, 1, 10, 0), attendance=-1),  # not a visit
    ])

    data = await report(async_session, 'peak_load', company_id=1)

    rows = {row['hour']: row for row in table(data, 'weekday_hour')['rows']}
    assert rows['10:00']['mon'] == 1 and rows['10:00']['total'] == 1
    assert rows['01:00']['tue'] == 1 and rows['01:00']['mon'] == 0
    assert '07:00' not in rows
    assert table(data, 'weekday_hour')['row_key'] == 'hour'
    assert cards(data)['Визитов'] == 2


@pytest.mark.asyncio
async def test_peak_load_lists_only_hours_with_visits_and_reports_peaks(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 5), moment=datetime(2026, 6, 5, 7, 0), seance_length=3600),  # Friday 10:00
        visit(date(2026, 6, 5), moment=datetime(2026, 6, 5, 7, 30), seance_length=5400, staff_id=3),  # Friday 10:30
        visit(date(2026, 6, 6), moment=datetime(2026, 6, 6, 10, 0), seance_length=None),  # Saturday 13:00
    ])

    data = await report(async_session, 'peak_load', company_id=1)

    assert [row['hour'] for row in table(data, 'weekday_hour')['rows']] == ['10:00', '13:00']
    assert chart(data, 'visits_by_weekday')['labels'] == ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс']
    assert chart(data, 'visits_by_weekday')['datasets'][0]['data'] == [0, 0, 0, 0, 2, 1, 0]
    assert chart(data, 'visits_by_hour')['datasets'][0]['data'] == [2, 1]
    assert cards(data) == {
        'Визитов': 3,
        'Забронировано часов': 2.5,
        'Пиковый час': '10:00',
        'Пиковый день': 'Пятница',
    }


@pytest.mark.asyncio
async def test_peak_load_uses_each_branchs_own_timezone_on_network_scope(async_session):
    await seed(
        async_session,
        [
            visit(date(2026, 6, 1), company_id=1, staff_id=1, moment=datetime(2026, 6, 1, 7, 0)),
            visit(date(2026, 6, 1), company_id=2, staff_id=2, moment=datetime(2026, 6, 1, 7, 0)),
        ],
        timezones=(None, 'Asia/Yekaterinburg'),
    )

    data = await report(async_session, 'peak_load', allowed_company_ids=[1, 2])

    rows = {row['hour']: row['mon'] for row in table(data, 'weekday_hour')['rows']}
    assert rows['10:00'] == 1  # no timezone set: Moscow
    assert rows['12:00'] == 1  # UTC+5


@pytest.mark.asyncio
async def test_peak_load_respects_the_staff_filter_and_has_no_money(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), staff_id=1),
        visit(date(2026, 6, 1), staff_id=3, moment=datetime(2026, 6, 1, 8, 0)),
    ])

    data = await report(async_session, 'peak_load', company_id=1, staff_id=3)

    assert cards(data)['Визитов'] == 1
    assert [row['hour'] for row in table(data, 'weekday_hour')['rows']] == ['11:00']
    assert dashboard_reports.report_requires_financials('peak_load') is False


# --- period axis -------------------------------------------------------------------------------------------------


def test_period_axis_is_bounded_and_survives_the_end_of_the_calendar():
    assert len(dashboard_reports._period_starts(date(2018, 1, 1), date(2026, 10, 9), 'day')) == 3204
    with pytest.raises(ValueError, match='too long'):
        dashboard_reports._period_starts(date(1000, 1, 1), date(2026, 10, 9), 'day')
    # Nothing follows 9999-12-31, so the axis simply ends there instead of overflowing.
    assert dashboard_reports._period_starts(date(9999, 12, 1), date(9999, 12, 31), 'month') == ['9999-12-01']
    assert dashboard_reports._period_starts(date(9999, 12, 29), date(9999, 12, 31), 'day')[-1] == '9999-12-31'


@pytest.mark.asyncio
async def test_a_window_of_centuries_by_day_is_refused_before_any_work(async_session):
    await seed(async_session, [])
    for report_id in ('financial_overview', 'bookings_dynamics', 'booking_channels', 'goods_dynamics'):
        with pytest.raises(ValueError, match='too long'):
            await report(async_session, report_id, (date(1000, 1, 1), date(2026, 10, 9)), granularity='day')


# --- bookings_dynamics -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bookings_dynamics_zero_fills_periods_and_computes_no_show_share(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), attendance=1),
        visit(date(2026, 6, 1), attendance=-1),
        visit(date(2026, 6, 1), attendance=0),
        visit(date(2026, 6, 1), attendance=-1),
        visit(date(2026, 6, 4), attendance=1),
    ])

    data = await report(async_session, 'bookings_dynamics', (date(2026, 6, 1), date(2026, 6, 5)), company_id=1)

    periods = table(data, 'periods')
    assert periods['row_key'] == 'period'
    assert [row['period'] for row in periods['rows']] == [f'2026-06-0{day}' for day in range(1, 6)]
    first = periods['rows'][0]
    assert (first['records'], first['completed'], first['no_show'], first['no_show_share']) == (4, 1, 2, 50.0)
    assert periods['rows'][1]['records'] == 0 and periods['rows'][1]['no_show_share'] is None
    assert chart(data, 'records_by_period')['x_kind'] == 'time'
    assert chart(data, 'records_by_period')['labels'] == [row['period'] for row in periods['rows']]
    assert chart(data, 'records_by_period')['datasets'][2]['data'] == [2, 0, 0, 0, 0]
    assert chart(data, 'no_show_share_by_period')['datasets'][0]['data'] == [50.0, None, None, 0.0, None]
    assert [c['label'] for c in data['cards']] == ['Всего записей', 'Завершено', 'Неявки', 'Доля неявок']
    assert cards(data) == {'Всего записей': 5, 'Завершено': 2, 'Неявки': 2, 'Доля неявок': 40.0}
    assert 'records_by_hour' not in {item['id'] for item in data['charts']}


@pytest.mark.asyncio
async def test_bookings_dynamics_cards_reconcile_with_the_overview_breakdown(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 2), attendance=1),
        visit(date(2026, 6, 9), attendance=-1),
        visit(date(2026, 6, 17), attendance=2),
        visit(date(2026, 7, 1), attendance=1),
    ])

    data = await report(async_session, 'bookings_dynamics', granularity='week', company_id=1)

    exact = await dashboard_service.fetch_appointments_breakdown(async_session, *JUNE, company_id=1)
    assert cards(data)['Всего записей'] == exact['total'] == 3
    assert cards(data)['Неявки'] == exact['cancelled'] == 1
    assert sum(row['records'] for row in table(data, 'periods')['rows']) == exact['total']
    assert [row['period'] for row in table(data, 'periods')['rows']] == [
        '2026-06-01', '2026-06-08', '2026-06-15', '2026-06-22', '2026-06-29'
    ]
    assert 'локальной базе' in data['notes'][0]['text']
    assert 'record_stats' not in json.dumps(data, ensure_ascii=False)


@pytest.mark.asyncio
async def test_bookings_dynamics_staff_table_is_keyed_by_id_with_branch(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), company_id=1, staff_id=1, attendance=1),
        visit(date(2026, 6, 1), company_id=1, staff_id=1, attendance=-1),
        visit(date(2026, 6, 1), company_id=2, staff_id=2, attendance=1),
        visit(date(2026, 6, 1), company_id=1, staff_id=None, attendance=-1),
    ])

    data = await report(async_session, 'bookings_dynamics', allowed_company_ids=[1, 2])

    staff = table(data, 'staff_records')
    assert staff['row_key'] == 'staff_id'
    by_id = {row['staff_id']: row for row in staff['rows']}
    assert set(by_id) == {1, 2, None}
    assert (by_id[1]['staff_name'], by_id[1]['company_title']) == ('Anna', 'Alpha')
    assert (by_id[2]['staff_name'], by_id[2]['company_title']) == ('Anna', 'Beta')
    assert (by_id[1]['records'], by_id[1]['no_show'], by_id[1]['no_show_share']) == (2, 1, 50.0)
    assert (by_id[None]['staff_name'], by_id[None]['company_title']) == ('Без мастера', None)
    assert [row['staff_id'] for row in staff['rows']] == [1, 2, None]  # by records, ties by id


@pytest.mark.asyncio
async def test_bookings_dynamics_staff_filter_keeps_only_that_masters_records(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), staff_id=1),
        visit(date(2026, 6, 1), staff_id=3, attendance=-1),
    ])

    data = await report(async_session, 'bookings_dynamics', company_id=1, staff_id=3)

    assert cards(data)['Всего записей'] == 1
    assert cards(data)['Неявки'] == 1
    assert [row['staff_id'] for row in table(data, 'staff_records')['rows']] == [3]


# --- booking_channels --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_booking_channels_split_online_from_staff_created(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), created_user_id=0),
        visit(date(2026, 6, 1), created_user_id=0, attendance=-1),
        visit(date(2026, 6, 1), created_user_id=77),
        visit(date(2026, 6, 3), created_user_id=78, attendance=0),
    ])

    data = await report(async_session, 'booking_channels', (date(2026, 6, 1), date(2026, 6, 3)), company_id=1)

    assert cards(data) == {'Записей': 4, 'Онлайн': 2, 'Доля онлайн': 50.0, 'Через администратора': 2}
    rows = table(data, 'periods')['rows']
    assert [(row['period'], row['online'], row['by_staff']) for row in rows] == [
        ('2026-06-01', 2, 1), ('2026-06-02', 0, 0), ('2026-06-03', 0, 1)
    ]
    assert rows[1]['online_share'] is None
    assert table(data, 'periods')['row_key'] == 'period'
    assert 'unknown' not in [column['key'] for column in table(data, 'periods')['columns']]
    assert chart(data, 'channels_by_period')['x_kind'] == 'time'
    assert chart(data, 'channels_by_period')['stacked'] is True
    assert 'источник записи не синхронизируется' in json.dumps(data['notes'], ensure_ascii=False)


@pytest.mark.asyncio
async def test_booking_channels_flags_records_with_unknown_author(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), created_user_id=0),
        visit(date(2026, 6, 1), created_user_id=None),
    ])

    data = await report(async_session, 'booking_channels', company_id=1)

    assert cards(data)['Записей'] == 2
    assert cards(data)['Доля онлайн'] == 100.0
    assert 'unknown' in [column['key'] for column in table(data, 'periods')['columns']]
    assert any(note['kind'] == 'warning' for note in data['notes'])


@pytest.mark.asyncio
async def test_booking_channels_staff_filter_and_no_money(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), staff_id=1, created_user_id=0),
        visit(date(2026, 6, 1), staff_id=3, created_user_id=5),
    ])

    data = await report(async_session, 'booking_channels', company_id=1, staff_id=3)

    assert cards(data)['Записей'] == 1
    assert cards(data)['Через администратора'] == 1
    assert dashboard_reports.report_requires_financials('booking_channels') is False
    text = json.dumps(data, ensure_ascii=False).lower()
    assert 'revenue' not in text and 'money' not in text and '₽' not in text


# --- staff_efficiency --------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staff_efficiency_keeps_same_name_staff_apart_and_labels_unassigned(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), company_id=1, staff_id=1),
        visit(date(2026, 6, 1), company_id=2, staff_id=2),
        visit(date(2026, 6, 2), company_id=2, staff_id=2),
        visit(date(2026, 6, 1), company_id=1, staff_id=None),
        # The only visit of the unassigned bucket sits in branch Beta ("Beta" > "Alpha"): the old min(title)
        # would have picked an arbitrary branch for the row.
        visit(date(2026, 6, 3), company_id=2, staff_id=None),
    ])

    data = await report(async_session, 'staff_efficiency', allowed_company_ids=[1, 2])

    staff = table(data, 'staff')
    assert staff['row_key'] == 'staff_id'
    by_id = {row['staff_id']: row for row in staff['rows']}
    assert set(by_id) == {1, 2, None}
    assert (by_id[1]['staff_name'], by_id[1]['company_title'], by_id[1]['completed']) == ('Anna', 'Alpha', 1)
    assert (by_id[2]['staff_name'], by_id[2]['company_title'], by_id[2]['completed']) == ('Anna', 'Beta', 2)
    assert (by_id[None]['staff_name'], by_id[None]['company_title'], by_id[None]['completed']) == ('Без мастера', None, 2)
    assert cards(data)['Сотрудников в отчете'] == 2
    labels = chart(data, 'staff_completed')['labels']
    assert len(set(labels)) == len(labels)


@pytest.mark.asyncio
async def test_staff_efficiency_counts_completed_visits_and_people_only(async_session):
    await seed(async_session, [
        visit(date(2026, 6, 1), staff_id=1),
        visit(date(2026, 6, 2), staff_id=3),
        visit(date(2026, 6, 2), staff_id=3, attendance=-1),
        visit(date(2026, 6, 3), staff_id=None),
    ])

    data = await report(async_session, 'staff_efficiency', company_id=1)

    now_cards = cards(data)
    assert now_cards['Завершено записей'] == 3
    assert now_cards['Выручка услуг'] == 0
    # The unassigned bucket is a row of the table, not an employee.
    assert now_cards['Сотрудников в отчете'] == 2
    rows = {row['staff_id']: row for row in table(data, 'staff')['rows']}
    assert {key: (row['completed'], row['appointments']) for key, row in rows.items()} == {
        1: (1, 1),
        3: (1, 2),
        None: (1, 1),
    }


@pytest.mark.asyncio
async def test_staff_efficiency_has_no_revenue_per_visit_without_completed_visits(async_session):
    await seed(async_session, [visit(date(2026, 6, 2), staff_id=3, attendance=-1)])

    data = await report(async_session, 'staff_efficiency', company_id=1)

    assert cards(data)['Завершено записей'] == 0
    assert cards(data)['Выручка услуг / завершенная запись'] is None
    (row,) = table(data, 'staff')['rows']
    assert row['completed'] == 0 and row['avg_check'] is None
