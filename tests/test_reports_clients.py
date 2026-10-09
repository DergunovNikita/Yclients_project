"""Client reports: concentration, retention of new clients, churn."""

import json
from calendar import monthrange
from datetime import date, datetime

import pytest

import dashboard_reports
import dashboard_service
from dashboard_service import DateRange
from models import Appointment, Company, FinancialTransaction, Group, Staff
from report_payload import ReportRequest
from reports_clients import (
    build_retention_3_6_12,
    build_revenue_at_risk,
    build_top_clients_pareto,
)

# Branch "today" is 2026-01-15, so the churn cutoff (90 days) is 2025-10-17.
NOW = datetime(2026, 1, 15, 9, 0, 0)


class Seed:
    """Collects appointments and payments with auto ids so each test reads as a list of visits."""

    def __init__(self):
        self.rows: list = []
        self._next = 1

    def visit(self, client, day, staff=1, company=1, attendance=1, paid=None, paid_on=None):
        appointment_id = self._next
        self._next += 1
        self.rows.append(Appointment(
            id=appointment_id, company_id=company, staff_id=staff, client_id=client, date=day, attendance=attendance,
        ))
        if paid is not None:
            self.rows.append(FinancialTransaction(
                id=appointment_id,
                date=datetime.combine(paid_on or day, datetime.min.time()).replace(hour=12),
                amount=float(paid),
                record_id=appointment_id,
                sold_item_type='service',
                master_id=staff,
                company_id=company,
            ))
        return appointment_id


async def _base(session, staff=((1, 'Anna'), (2, 'Anna'), (3, 'Boris')), window_end=None):
    session.add(Group(id=1, title='G'))
    session.add(Company(id=1, title='Salon', group_id=1, reporting_end_date=window_end))
    session.add_all([Staff(id=sid, name=name, position='Барбер', company_id=1) for sid, name in staff])
    await session.flush()


def request(session, start, end, staff_id=None, factual_at=NOW):
    return ReportRequest(
        db=session,
        base={'cards': [], 'charts': [], 'tables': [], 'notes': [], 'raw': {}},
        start=start,
        end=end,
        company_id=None,
        staff_id=staff_id,
        granularity='day',
        allowed_company_ids=[1],
        factual_at=factual_at,
    )


def cards(payload):
    return {item['label']: item['value'] for item in payload['cards']}


def table_rows(payload, table_id):
    return next(item for item in payload['tables'] if item['id'] == table_id)['rows']


# --- retention ---------------------------------------------------------------------------------------


async def _seed_cohorts(session):
    await _base(session)
    seed = Seed()
    # January 2025 cohort: 5 clients.
    seed.visit(1, date(2025, 1, 10))
    seed.visit(1, date(2025, 2, 5))    # back in under a month
    seed.visit(2, date(2025, 1, 10))
    seed.visit(2, date(2025, 3, 12))   # two months and two days later: within 3, not within 1
    seed.visit(3, date(2025, 1, 10))
    seed.visit(3, date(2025, 1, 10))   # a second visit the same day is not a return
    seed.visit(4, date(2025, 1, 31))
    seed.visit(4, date(2025, 4, 30))   # two whole months and 30 days: still within 3
    seed.visit(5, date(2025, 1, 20), staff=3)
    # February 2025 cohort: client 6 had history before, so is not new; client 7 is new.
    seed.visit(6, date(2024, 12, 1))
    seed.visit(6, date(2025, 2, 3))
    seed.visit(7, date(2025, 2, 3), staff=2)
    # December 2025 cohort, too recent for any horizon but 1 month (ends 31.12, +1 month = 31.01 > today).
    seed.visit(8, date(2025, 12, 5))
    # A no-show and an anonymous visit never create a cohort member.
    seed.visit(9, date(2025, 1, 15), attendance=0)
    seed.visit(None, date(2025, 1, 15))
    session.add_all(seed.rows)
    await session.commit()


@pytest.mark.asyncio
async def test_cohort_size_equals_overview_new_clients_for_each_month(async_session):
    await _seed_cohorts(async_session)
    payload = await build_retention_3_6_12(request(async_session, date(2025, 1, 1), date(2025, 3, 31)))
    sizes = {row['cohort_month']: row['size'] for row in payload['raw']['cohorts']}
    assert sizes == {'2025-01': 5, '2025-02': 1, '2025-03': 0}
    for month in (1, 2, 3):
        overview = await dashboard_service._client_recency_block(
            async_session,
            DateRange(date(2025, month, 1), date(2025, month, monthrange(2025, month)[1])),
            None,
            None,
            [1],
            NOW,
        )
        assert sizes[f'2025-{month:02d}'] == overview['new_clients']


@pytest.mark.asyncio
async def test_returns_within_horizons_and_immature_cells_are_none(async_session):
    await _seed_cohorts(async_session)
    payload = await build_retention_3_6_12(request(async_session, date(2025, 1, 1), date(2026, 1, 31)))
    rows = {row['cohort_month']: row for row in payload['raw']['cohorts']}
    january = rows['2025-01']
    assert january['returned_1'] == 1
    assert january['pct_1'] == pytest.approx(20.0)
    assert january['pct_3'] == pytest.approx(60.0)
    assert january['pct_6'] == pytest.approx(60.0)
    # 31.01.2025 + 12 months is 31.01.2026, after today (15.01.2026).
    assert january['pct_12'] is None and january['returned_12'] is None
    # Whole-month rows exist for every month, immature ones included.
    assert rows['2025-12']['size'] == 1
    assert rows['2025-12']['pct_1'] is None and rows['2025-12']['pct_3'] is None
    assert rows['2026-01']['size'] == 0 and rows['2026-01']['pct_1'] is None
    # A cohort of one that did not return is a mature 0 %, not an unknown.
    assert rows['2025-02']['pct_3'] == 0.0
    # Cards weigh only mature cohorts: January and February at 3 months, 3 of 6 clients came back.
    values = cards(payload)
    assert values['Вернулись в течение 3 мес'] == pytest.approx(100.0 * 3 / 6)
    assert values['Новых клиентов (когорты периода)'] == 7
    chart_data = payload['charts'][0]['datasets'][0]['data']
    assert chart_data[0] == pytest.approx(60.0) and chart_data[-1] is None
    assert payload['charts'][0]['x_kind'] == 'time'
    assert next(item for item in payload['tables'] if item['id'] == 'retention_cohorts')['row_key'] == 'cohort'


@pytest.mark.asyncio
async def test_a_horizon_ending_today_is_not_mature_until_the_day_is_over(async_session):
    """A client first seen on the last day of the month has until the same day a month later, inclusive."""
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 12, 31))
    async_session.add_all(seed.rows)
    await async_session.commit()
    window = (date(2025, 12, 1), date(2025, 12, 31))

    on_the_horizon = await build_retention_3_6_12(request(
        async_session, *window, factual_at=datetime(2026, 1, 31, 9, 0, 0)
    ))
    assert on_the_horizon['raw']['cohorts'][0]['pct_1'] is None

    day_after = await build_retention_3_6_12(request(
        async_session, *window, factual_at=datetime(2026, 2, 1, 9, 0, 0)
    ))
    assert day_after['raw']['cohorts'][0]['pct_1'] == 0.0


@pytest.mark.asyncio
async def test_retention_staff_filter_selects_cohort_by_first_master_but_counts_any_return(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 1, 10), staff=1)
    seed.visit(1, date(2025, 1, 25), staff=2)   # came back, to a different master
    seed.visit(2, date(2025, 1, 12), staff=2)   # first visit with master 2
    seed.visit(2, date(2025, 3, 1), staff=1)
    async_session.add_all(seed.rows)
    await async_session.commit()

    payload = await build_retention_3_6_12(request(async_session, date(2025, 1, 1), date(2025, 1, 31), staff_id=1))
    january = payload['raw']['cohorts'][0]
    assert january['size'] == 1 and january['pct_1'] == pytest.approx(100.0)
    other = await build_retention_3_6_12(request(async_session, date(2025, 1, 1), date(2025, 1, 31), staff_id=2))
    assert other['raw']['cohorts'][0]['size'] == 1
    assert other['raw']['cohorts'][0]['pct_1'] == 0.0 and other['raw']['cohorts'][0]['pct_3'] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_retention_payload_carries_no_money(async_session):
    await _seed_cohorts(async_session)
    seed = Seed()
    seed._next = 1000
    seed.visit(20, date(2025, 1, 5), paid=5000)
    async_session.add_all(seed.rows)
    await async_session.commit()
    payload = await build_retention_3_6_12(request(async_session, date(2025, 1, 1), date(2025, 12, 31)))
    assert dashboard_reports.report_requires_financials('client_cohorts') is False
    assert dashboard_reports.REPORT_REGISTRY['retention_3_6_12'].requires_financials is False
    serialized = json.dumps(payload)
    assert '"format": "money"' not in serialized
    assert 'revenue' not in serialized and '5000' not in serialized


@pytest.mark.asyncio
async def test_retention_waits_for_a_branch_that_left_only_until_its_window_end(async_session):
    await _base(async_session, window_end=date(2025, 6, 30))
    seed = Seed()
    seed.visit(1, date(2025, 1, 10))
    seed.visit(1, date(2025, 8, 1))   # after the handover: outside the window, never seen
    async_session.add_all(seed.rows)
    await async_session.commit()
    payload = await build_retention_3_6_12(request(async_session, date(2025, 1, 1), date(2025, 1, 31)))
    january = payload['raw']['cohorts'][0]
    # Six months after January is past the window end, so nothing is known about month 6 and beyond.
    assert january['pct_3'] == 0.0
    assert january['pct_6'] is None


# --- churn -------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_only_mature_clients_with_no_return_are_churned(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 10, 17))  # exactly 90 days before today: mature
    seed.visit(2, date(2025, 10, 18))  # 89 days: not yet
    seed.visit(3, date(2025, 10, 5))
    seed.visit(3, date(2025, 11, 20))  # came back: the last visit is in November, not October
    seed.visit(4, date(2025, 10, 5), attendance=0)  # a no-show is not a visit
    async_session.add_all(seed.rows)
    await async_session.commit()

    payload = await build_revenue_at_risk(request(async_session, date(2025, 10, 1), date(2025, 10, 31)))
    values = cards(payload)
    assert values['Ушедших клиентов'] == 1
    # The share's base is the clients old enough to have left: 1 and 3. Client 2 (89 days) is neither
    # churned nor part of the base, so a recent window is not diluted by people who could not have left yet.
    assert values['Доля от клиентов периода'] == pytest.approx(50.0)
    assert payload['raw']['share_base_clients'] == 2
    notes = [note['title'] for note in payload['notes']]
    assert 'Недавние клиенты пока не учтены' in notes
    months = table_rows(payload, 'churn_by_month')
    assert [row['month_iso'] for row in months] == ['2025-10']
    assert months[0]['clients'] == 1

    november = await build_revenue_at_risk(request(async_session, date(2025, 11, 1), date(2025, 11, 30)))
    assert cards(november)['Ушедших клиентов'] == 0
    assert table_rows(november, 'churn_by_month') == []

    old_period = await build_revenue_at_risk(request(async_session, date(2025, 9, 1), date(2025, 9, 30)))
    assert 'Недавние клиенты пока не учтены' not in [note['title'] for note in old_period['notes']]


@pytest.mark.asyncio
async def test_churn_window_opened_at_the_dawn_of_the_calendar_does_not_overflow(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 3, 1))
    async_session.add_all(seed.rows)
    await async_session.commit()

    payload = await build_revenue_at_risk(request(async_session, date(1, 1, 1), date(1, 12, 31)))
    assert cards(payload)['Ушедших клиентов'] == 0
    assert len(table_rows(payload, 'churn_by_month')) == 12


@pytest.mark.asyncio
async def test_monthly_tables_refuse_a_window_of_centuries(async_session):
    await _base(async_session)
    for build in (build_retention_3_6_12, build_revenue_at_risk):
        with pytest.raises(ValueError, match='too long'):
            await build(request(async_session, date(1, 1, 1), date(2025, 12, 31)))


@pytest.mark.asyncio
async def test_revenue_at_risk_is_a_twelfth_of_the_year_of_service_payments(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2024, 8, 10), paid=300)                    # 365 days before the last visit: outside
    seed.visit(1, date(2024, 8, 11), paid=600)                    # 364 days before: inside
    seed.visit(1, date(2025, 3, 3), paid=400)
    seed.visit(1, date(2025, 8, 10), paid=200, paid_on=date(2025, 8, 20))  # paid after the last visit: outside
    seed.visit(1, date(2025, 8, 10), paid=200)                    # a second visit that day, paid on the day
    seed.visit(2, date(2025, 8, 12), paid=0)                      # nothing paid
    async_session.add_all(seed.rows)
    await async_session.commit()

    payload = await build_revenue_at_risk(request(async_session, date(2025, 8, 1), date(2025, 8, 31)))
    values = cards(payload)
    assert values['Ушедших клиентов'] == 2
    assert values['Ежемесячная выручка под риском'] == pytest.approx((600 + 400 + 200) / 12)
    assert values['Доля от клиентов периода'] == pytest.approx(100.0)
    assert table_rows(payload, 'churn_by_month')[0]['revenue'] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_losses_are_attributed_to_the_last_master_by_id_not_by_name(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 7, 1), staff=1)
    seed.visit(1, date(2025, 8, 1), staff=2)   # same name as staff 1, but the last master is 2
    seed.visit(2, date(2025, 8, 3), staff=1)
    seed.visit(3, date(2025, 8, 4), staff=2)
    seed.visit(4, date(2025, 8, 5), staff=None)
    seed.visit(5, date(2025, 8, 6), staff=3)
    async_session.add_all(seed.rows)
    await async_session.commit()

    payload = await build_revenue_at_risk(request(async_session, date(2025, 8, 1), date(2025, 8, 31)))
    staff_table = next(item for item in payload['tables'] if item['id'] == 'churn_by_staff')
    assert staff_table['row_key'] == 'staff_key'
    by_key = {row['staff_key']: row for row in staff_table['rows']}
    assert set(by_key) == {'1', '2', '3', 'none:1'}
    assert by_key['1']['clients'] == 1 and by_key['2']['clients'] == 2
    assert by_key['1']['staff_name'] == by_key['2']['staff_name'] == 'Anna'
    assert by_key['1']['company_title'] == 'Salon'
    assert by_key['none:1']['staff_name'] == 'Без мастера'
    assert sum(row['clients'] for row in staff_table['rows']) == cards(payload)['Ушедших клиентов'] == 5


@pytest.mark.asyncio
async def test_churn_staff_filter_selects_clients_by_last_master(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 8, 1), staff=1)
    seed.visit(1, date(2025, 8, 20), staff=2)  # last master is 2: not master 1's loss
    seed.visit(2, date(2025, 8, 3), staff=1)
    seed.visit(3, date(2025, 8, 4), staff=2)
    async_session.add_all(seed.rows)
    await async_session.commit()

    payload = await build_revenue_at_risk(request(async_session, date(2025, 8, 1), date(2025, 8, 31), staff_id=1))
    values = cards(payload)
    assert values['Ушедших клиентов'] == 1
    # Master 1 served clients 1 and 2 in the period.
    assert values['Доля от клиентов периода'] == pytest.approx(50.0)
    assert [row['staff_key'] for row in table_rows(payload, 'churn_by_staff')] == ['1']
    assert 'Фильтр по мастеру' in [note['title'] for note in payload['notes']]


@pytest.mark.asyncio
async def test_churn_waits_for_the_window_end_of_a_branch_that_left(async_session):
    await _base(async_session, window_end=date(2025, 8, 31))
    seed = Seed()
    seed.visit(1, date(2025, 5, 30))   # 31.08 - 90 days = 02.06.2025: mature
    seed.visit(2, date(2025, 7, 15))   # 47 days before the handover: unknown, not churned
    async_session.add_all(seed.rows)
    await async_session.commit()
    payload = await build_revenue_at_risk(request(async_session, date(2025, 5, 1), date(2025, 7, 31)))
    assert cards(payload)['Ушедших клиентов'] == 1


# --- concentration -----------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pareto_frequency_matches_overview_and_drops_degenerate_blocks(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 3, 5), paid=1000)                              # one visit ever
    seed.visit(2, date(2024, 11, 4))
    seed.visit(2, date(2025, 3, 6), paid=500)                               # two visits
    for offset in range(3):
        seed.visit(3, date(2024, 8, 1 + offset))
    seed.visit(3, date(2025, 3, 7), paid=300)                               # four visits
    seed.visit(4, date(2025, 2, 20))
    seed.visit(4, date(2025, 2, 25), paid=900, paid_on=date(2025, 3, 2))    # paid in March for a February visit
    seed.visit(None, date(2025, 3, 8), paid=200)                            # no client card
    async_session.add_all(seed.rows)
    await async_session.commit()

    start, end = date(2025, 3, 1), date(2025, 3, 31)
    payload = await build_top_clients_pareto(request(async_session, start, end))
    overview = await dashboard_service._client_visit_frequency_block(
        async_session, DateRange(start, end), None, None, [1], NOW
    )
    frequency = {row['bucket']: row for row in table_rows(payload, 'visit_frequency')}
    assert frequency['1 визит']['clients'] == overview['one_visit']['count'] == 1
    assert frequency['2-3 визита']['clients'] == overview['two_to_three_visits']['count'] == 1
    assert frequency['4+ визита']['clients'] == overview['four_plus_visits']['count'] == 1
    assert frequency['1 визит']['clients_pct'] == pytest.approx(overview['one_visit']['pct'])
    assert '0 визитов' not in frequency
    # The February visit paid in March: a paying client with no visit in the period, shown on its own row.
    assert frequency['Оплатили визиты прошлых периодов']['clients'] == 1
    assert frequency['Оплатили визиты прошлых периодов']['revenue'] == 900.0

    assert {item['id'] for item in payload['tables']} == {'client_pareto', 'visit_frequency'}
    assert all(item['row_key'] == 'bucket' for item in payload['tables'])
    pareto = {row['bucket']: row for row in table_rows(payload, 'client_pareto')}
    assert 'Без клиента' not in pareto
    anonymous = pareto['Оплаты без клиента']
    assert anonymous['clients'] is None and anonymous['revenue'] == 200.0
    assert sum(row['revenue_pct'] for row in pareto.values()) == pytest.approx(100.0)
    values = cards(payload)
    assert values['Клиентов'] == 4
    # Clients' 2700 plus the 200 paid without a client card: the Overview's service revenue.
    assert values['Выручка клиентов'] == 2900.0
    assert values['Средний доход на клиента'] == pytest.approx(675.0)


@pytest.mark.asyncio
async def test_pareto_shows_no_anonymous_row_when_every_payment_has_a_client(async_session):
    await _base(async_session)
    seed = Seed()
    seed.visit(1, date(2025, 3, 5), paid=1000)
    async_session.add_all(seed.rows)
    await async_session.commit()
    payload = await build_top_clients_pareto(request(async_session, date(2025, 3, 1), date(2025, 3, 31)))
    assert [row['bucket'] for row in table_rows(payload, 'client_pareto')] == [
        'Топ 10% клиентов', 'Следующие 40%', 'Остальные 50%',
    ]
    assert [row['bucket'] for row in table_rows(payload, 'visit_frequency')] == ['1 визит', '2-3 визита', '4+ визита']
