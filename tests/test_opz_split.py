"""OPZ counts one event per rebooked visit and splits it by that visit.

The visit is either the client's first in the branch or a repeat one.
"""
from datetime import date, datetime

import pytest

import dashboard_reports
import dashboard_service
from models import Appointment, Company, ManualFactMetric, Staff
from report_helpers import table_rows

NOW = datetime(2025, 12, 31, 12, 0)
MARCH = (date(2025, 3, 1), date(2025, 3, 31))


def _visit(id, client_id, day, *, staff_id=1, created=None, attendance=1):
    return Appointment(
        id=id, company_id=1, client_id=client_id, staff_id=staff_id, date=day,
        datetime=datetime.combine(day, datetime.min.time()).replace(hour=12),
        attendance=attendance, create_date=created or datetime(2024, 1, 1, 9),
    )


async def _seed(async_session, appointments, **company):
    async_session.add_all([
        Company(id=1, title='Salon', timezone='UTC', **company),
        Company(id=2, title='Second salon', timezone='UTC'),
        Staff(id=1, name='Barber one', position='Барбер', company_id=1),
        Staff(id=2, name='Barber two', position='Барбер', company_id=1),
        Staff(id=3, name='Admin', position='Администратор', company_id=1),
    ])
    await async_session.flush()
    async_session.add_all(appointments)
    await async_session.commit()


async def _event_ids(async_session, start, end):
    events = await dashboard_service._opz_events(async_session, start, end, 1, factual_at=NOW)
    return [event.event_id for event in events]


async def _opz_qty(async_session, start, end):
    summary = await dashboard_service.fetch_summary(async_session, start, end, company_id=1, factual_at=NOW)
    return summary['visit_metrics']['opz_qty']


@pytest.mark.asyncio
async def test_a_visit_rebooked_on_the_eve_of_a_period_counts_in_one_period_only(async_session):
    await _seed(async_session, [
        _visit(1, 7, date(2025, 2, 28)),
        # Two bookings on the visit's day and one the morning after: the visit was rebooked once.
        _visit(2, 7, date(2025, 3, 15), attendance=0, created=datetime(2025, 2, 28, 13)),
        _visit(3, 7, date(2025, 3, 16), attendance=0, created=datetime(2025, 2, 28, 14)),
        _visit(4, 7, date(2025, 3, 20), attendance=0, created=datetime(2025, 3, 1, 9)),
    ])

    assert await _opz_qty(async_session, date(2025, 2, 1), date(2025, 2, 28)) == 1.0
    assert await _opz_qty(async_session, *MARCH) == 0.0
    assert await _opz_qty(async_session, date(2025, 2, 1), date(2025, 3, 31)) == 1.0


def _march_history():
    return [
        # Client 1: first visit on the 3rd, rebooked; the rebooked visit on the 20th, rebooked again.
        _visit(1, 1, date(2025, 3, 3)),
        _visit(2, 1, date(2025, 3, 20), staff_id=2, created=datetime(2025, 3, 3, 13)),
        _visit(3, 1, date(2025, 4, 10), attendance=0, created=datetime(2025, 3, 20, 13)),
        # Client 2: a repeat visit with no rebooking; client 3: a first visit with none.
        _visit(4, 2, date(2025, 1, 10)),
        _visit(5, 2, date(2025, 3, 5)),
        _visit(6, 3, date(2025, 3, 7)),
    ]


@pytest.mark.asyncio
async def test_overview_splits_opz_by_the_rebooked_visit_each_over_its_own_visits(async_session):
    await _seed(async_session, _march_history())
    async_session.add(ManualFactMetric(
        period_start=date(2025, 3, 1), period_end=date(2025, 3, 31), company_id=1, staff_id=3,
        metric_code='opz_qty', value=3.0, updated_at=datetime(2025, 3, 31, 10),
    ))
    await async_session.commit()

    metrics = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1))['visit_metrics']

    # Client 1 counts twice: once for each rebooked visit. The top-up joins only the total.
    assert metrics['opz_qty'] == 2.0 + 3.0
    assert metrics['opz_pct'] == pytest.approx(100.0 * 5 / 4)
    assert (metrics['opz_first_visit_qty'], metrics['first_visits']) == (1.0, 2)
    assert (metrics['opz_repeat_visit_qty'], metrics['repeat_visits']) == (1.0, 2)
    assert metrics['opz_first_visit_pct'] == metrics['opz_repeat_visit_pct'] == 50.0

    # A staff filter keeps the events anchored on that barber's visits and divides by those visits.
    barber = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1, staff_id=2))['visit_metrics']
    assert barber['opz_qty'] == 1.0
    # No first visit to divide by: no rate, not a zero one.
    assert (barber['opz_first_visit_qty'], barber['first_visits'], barber['opz_first_visit_pct']) == (0.0, 0, None)
    assert (barber['opz_repeat_visit_qty'], barber['repeat_visits'], barber['opz_repeat_visit_pct']) == (1.0, 1, 100.0)


@pytest.mark.asyncio
async def test_new_and_repeat_clients_report_shows_the_overview_split(async_session):
    await _seed(async_session, _march_history())

    report = await dashboard_reports.fetch_report_data(
        async_session, 'new_vs_returning_cross', *MARCH, allowed_company_ids=[1],
    )
    overview = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1))['visit_metrics']

    rows = {row['segment']: row for row in table_rows(report, 'opz_by_visit')}
    assert rows['После первого визита']['visits'] == overview['first_visits']
    assert rows['После первого визита']['opz_qty'] == overview['opz_first_visit_qty']
    assert rows['После повторного визита']['opz_pct'] == overview['opz_repeat_visit_pct']
    cards = {card['label']: card['value'] for card in report['cards']}
    assert cards['ОПЗ % после первого визита'] == overview['opz_first_visit_pct']
    assert cards['ОПЗ % после повторного визита'] == overview['opz_repeat_visit_pct']


@pytest.mark.asyncio
async def test_a_visit_before_the_reporting_start_does_not_make_the_next_one_a_repeat(async_session):
    await _seed(async_session, [
        # The previous point on the same branch id: outside the window, so not this branch's history.
        _visit(1, 1, date(2025, 1, 15)),
        _visit(2, 1, date(2025, 3, 10)),
        _visit(3, 1, date(2025, 4, 1), attendance=0, created=datetime(2025, 3, 10, 13)),
    ], reporting_start_date=date(2025, 2, 1))

    metrics = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1))['visit_metrics']

    assert (metrics['first_visits'], metrics['opz_first_visit_qty']) == (1, 1.0)


@pytest.mark.asyncio
async def test_two_visits_on_the_first_day_are_both_first_visits(async_session):
    await _seed(async_session, [
        _visit(1, 1, date(2025, 3, 3)),
        _visit(2, 1, date(2025, 3, 3)),
        _visit(3, 1, date(2025, 3, 20), attendance=0, created=datetime(2025, 3, 3, 13)),
        _visit(4, 1, date(2025, 3, 21), attendance=1, created=datetime(2025, 3, 4, 9)),
    ])

    metrics = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1))['visit_metrics']

    # Whole days, like the new-client count: the later visit of the first day is no repeat one.
    assert (metrics['first_visits'], metrics['repeat_visits']) == (2, 1)
    assert (metrics['opz_first_visit_qty'], metrics['opz_repeat_visit_qty']) == (1.0, 0.0)


@pytest.mark.asyncio
async def test_a_window_at_the_start_of_the_calendar_does_not_overflow_the_day_before(async_session):
    # The zone furthest ahead of UTC in year 1 (local mean time, +15:13): its local midnight is the UTC
    # day before, the other half of the overflow.
    async_session.add(Company(id=1, title='Salon', timezone='America/Metlakatla'))
    await async_session.commit()

    assert await _event_ids(async_session, date(1, 1, 2), date(1, 1, 10)) == []


@pytest.mark.asyncio
async def test_a_visit_on_a_service_staff_row_is_not_history(async_session):
    await _seed(async_session, [
        # Booked on the waitlist row: not a business visit, so not the client's history.
        _visit(1, 1, date(2025, 2, 10), staff_id=9),
        _visit(2, 1, date(2025, 3, 10)),
        _visit(3, 1, date(2025, 4, 1), attendance=0, created=datetime(2025, 3, 10, 13)),
    ])
    async_session.add(Staff(id=9, name='Лист ожидания', company_id=1))
    await async_session.commit()

    metrics = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1))['visit_metrics']

    assert (metrics['first_visits'], metrics['repeat_visits']) == (1, 0)
    assert (metrics['opz_first_visit_qty'], metrics['opz_repeat_visit_qty']) == (1.0, 0.0)


@pytest.mark.asyncio
async def test_the_split_compares_with_the_previous_period_and_a_zero_base_has_no_change(async_session):
    await _seed(async_session, [
        # February: a repeat visit of client 2 (first seen in January), rebooked. No first visit at all.
        _visit(102, 2, date(2025, 2, 10)),
        _visit(103, 2, date(2025, 3, 25), attendance=0, created=datetime(2025, 2, 10, 13)),
        *_march_history(),
    ])

    metrics = (
        await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1, period_preset='month')
    )['visit_metrics']

    # March: repeat 1 of 2 visits; February: repeat 1 of 1, so the rate halved.
    assert metrics['opz_repeat_visit_pct'] == 50.0
    assert metrics['opz_repeat_visit_pct_change_pct'] == -50.0
    # February had no first visit: no base to compare with, not a made-up growth.
    assert metrics['opz_first_visit_pct_change_pct'] is None


@pytest.mark.asyncio
async def test_year_over_year_first_visits_follow_the_staff_filter(async_session):
    await _seed(async_session, _march_history())

    facts = await dashboard_service.fetch_year_over_year_facts(
        async_session, date(2025, 1, 1), date(2025, 12, 31), 1, 2, [1],
    )

    # Barber 2 did only client 1's second visit, which is not a first one.
    assert facts['monthly'][2025][3]['appointments'] == 1
    assert facts['monthly'][2025][3]['first_visits'] == 0
    # The same base the Overview divides by, with and without the filter.
    for staff_id in (None, 2):
        facts = await dashboard_service.fetch_year_over_year_facts(
            async_session, date(2025, 1, 1), date(2025, 12, 31), 1, staff_id, [1],
        )
        overview = (
            await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1, staff_id=staff_id)
        )['visit_metrics']
        assert facts['monthly'][2025][3]['first_visits'] == overview['first_visits']


@pytest.mark.asyncio
async def test_first_visits_are_per_branch_across_a_network_scope(async_session):
    await _seed(async_session, [
        *_march_history(),
        # Branch 2 reuses branch 1's client ids on purpose: history is per branch, so client 2's
        # March visit here is a first one although branch 1 saw the same id in January.
        Appointment(id=201, company_id=2, client_id=2, staff_id=1, date=date(2025, 3, 12),
                    datetime=datetime(2025, 3, 12, 12), attendance=1, create_date=datetime(2024, 1, 1, 9)),
        Appointment(id=202, company_id=2, client_id=1, staff_id=1, date=date(2025, 2, 1),
                    datetime=datetime(2025, 2, 1, 12), attendance=1, create_date=datetime(2024, 1, 1, 9)),
        Appointment(id=203, company_id=2, client_id=1, staff_id=1, date=date(2025, 3, 14),
                    datetime=datetime(2025, 3, 14, 12), attendance=1, create_date=datetime(2024, 1, 1, 9)),
    ])

    network = (
        await dashboard_service.fetch_summary(async_session, *MARCH, allowed_company_ids=[1, 2])
    )['visit_metrics']

    # Branch 1 has two first and two repeat visits in March, branch 2 one of each.
    assert (network['first_visits'], network['repeat_visits']) == (3, 3)


@pytest.mark.asyncio
async def test_the_report_comparison_carries_the_split_table(async_session):
    await _seed(async_session, _march_history())

    report = await dashboard_reports.fetch_report_data(
        async_session, 'new_vs_returning_cross', *MARCH, allowed_company_ids=[1],
        compare_previous=True, period_preset='month',
    )

    table = next(table for table in report['comparison']['tables'] if table['id'] == 'opz_by_visit')
    rows = {row['segment']: row for row in table['rows']}
    assert set(rows) == {'После первого визита', 'После повторного визита'}


@pytest.mark.asyncio
async def test_first_visits_are_the_new_clients_visits(async_session):
    """The split and the new-client count share one meaning of "first": with no client visiting
    twice on their first day, first visits and new clients are the same number."""
    await _seed(async_session, _march_history())

    metrics = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1))['visit_metrics']

    assert metrics['first_visits'] == metrics['new_clients'] == 2


@pytest.mark.asyncio
async def test_the_report_split_matches_the_overview_under_a_staff_filter_and_preset(async_session):
    await _seed(async_session, [
        # February: barber 1 does a first visit (client 5) and a repeat one (client 2), both rebooked.
        _visit(301, 5, date(2025, 2, 12)),
        _visit(302, 5, date(2025, 3, 28), attendance=0, created=datetime(2025, 2, 12, 13)),
        _visit(303, 2, date(2025, 2, 14)),
        _visit(304, 2, date(2025, 3, 26), attendance=0, created=datetime(2025, 2, 14, 13)),
        *_march_history(),
    ])

    for staff_id in (None, 1):
        overview = (await dashboard_service.fetch_summary(
            async_session, *MARCH, company_id=1, staff_id=staff_id, period_preset='month',
        ))['visit_metrics']
        mix = await dashboard_service.fetch_client_mix_metrics(
            async_session, *MARCH, 1, staff_id, period_preset='month',
        )
        split_keys = [
            key for key in overview
            if 'first_visit' in key or 'repeat_visit' in key or key.endswith('_visits')
        ]
        assert {
            'first_visits', 'repeat_visits', 'opz_first_visit_qty', 'opz_repeat_visit_qty',
            'opz_first_visit_pct', 'opz_repeat_visit_pct',
        } <= set(split_keys)
        assert {key: mix[key] for key in split_keys} == {key: overview[key] for key in split_keys}
        # February is the base and has first visits of its own, so the change is a real number.
        assert overview['opz_first_visit_pct_change_pct'] is not None


@pytest.mark.asyncio
async def test_an_anchor_on_a_service_staff_row_with_no_business_history_is_a_first_visit(async_session):
    await _seed(async_session, [
        _visit(1, 1, date(2025, 3, 10), staff_id=9),
        _visit(2, 1, date(2025, 4, 1), attendance=0, created=datetime(2025, 3, 10, 13)),
    ])
    async_session.add(Staff(id=9, name='Лист ожидания', company_id=1))
    await async_session.commit()

    metrics = (await dashboard_service.fetch_summary(async_session, *MARCH, company_id=1))['visit_metrics']

    # The event counts and is first, but its visit is in no base: the documented edge of reading anchors
    # without the business-staff filter.
    assert (metrics['opz_first_visit_qty'], metrics['first_visits'], metrics['opz_first_visit_pct']) == (1.0, 0, None)


def _plan_fact_row(plan_fact, staff_id):
    group = next(group for group in plan_fact['groups'] if group.get('staff_id') == staff_id)
    return {metric['code']: metric['fact'] for metric in group['metrics']}


async def _seed_administrator_march(async_session):
    await _seed(async_session, [
        # February: a repeat visit of client 2 (first seen in January), rebooked; the base of the deltas.
        _visit(102, 2, date(2025, 2, 10)),
        _visit(103, 2, date(2025, 3, 25), attendance=0, created=datetime(2025, 2, 10, 13)),
        *_march_history(),
    ])
    async_session.add(ManualFactMetric(
        period_start=date(2025, 3, 1), period_end=date(2025, 3, 31), company_id=1, staff_id=3,
        metric_code='opz_qty', value=3.0, updated_at=datetime(2025, 3, 31, 10),
    ))
    await async_session.commit()


@pytest.mark.asyncio
async def test_an_administrators_overview_opz_is_what_plan_fact_credits_to_them(async_session):
    await _seed_administrator_march(async_session)

    summary = await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, staff_id=3, factual_at=NOW, period_preset='month',
    )
    metrics = summary['visit_metrics']

    assert summary['service_attribution']['mode'] == 'administrator_schedule'
    # Both March events are the lone administrator's, over the four visits credited to them, plus the manual 3.
    assert (metrics['opz_qty'], metrics['opz_pct']) == (5.0, 125.0)
    first = (metrics['opz_first_visit_qty'], metrics['first_visits'], metrics['opz_first_visit_pct'])
    repeat = (metrics['opz_repeat_visit_qty'], metrics['repeat_visits'], metrics['opz_repeat_visit_pct'])
    assert first == (1.0, 2, 50.0)
    assert repeat == (1.0, 2, 50.0)
    # The split is the calculated part: the manual top-up belongs to neither half.
    assert metrics['opz_first_visit_qty'] + metrics['opz_repeat_visit_qty'] == metrics['opz_qty'] - 3.0
    assert metrics['first_visits'] + metrics['repeat_visits'] == 4

    # The same row of Plan/fact, manual top-up included.
    async_session.info.clear()
    row = _plan_fact_row(
        await dashboard_service.fetch_plan_fact(async_session, *MARCH, company_id=1, staff_id=3, factual_at=NOW), 3,
    )
    assert row['opz_qty'] == metrics['opz_qty']
    assert row['clients'] == metrics['first_visits'] + metrics['repeat_visits']
    assert row['opz_pct'] == pytest.approx(100.0 * row['opz_qty'] / row['clients'], abs=0.01)


@pytest.mark.asyncio
async def test_an_administrators_opz_compares_with_the_previous_period_the_same_way(async_session):
    await _seed_administrator_march(async_session)

    metrics = (await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, staff_id=3, factual_at=NOW, period_preset='month',
    ))['visit_metrics']

    # February: one repeat event over one record, no first visit at all, and no manual top-up (March's is its own).
    assert metrics['opz_qty_change_pct'] == 400.0
    assert metrics['opz_pct_change_pct'] == 25.0
    assert metrics['opz_repeat_visit_pct_change_pct'] == -50.0
    assert metrics['opz_repeat_visit_qty_change_pct'] == 0.0
    assert metrics['opz_first_visit_pct_change_pct'] is None
    assert metrics['opz_first_visit_qty_change_pct'] is None


@pytest.mark.asyncio
async def test_the_report_split_matches_the_overview_for_an_administrator(async_session):
    await _seed_administrator_march(async_session)

    overview = (await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, staff_id=3, factual_at=NOW, period_preset='month',
    ))['visit_metrics']
    mix = await dashboard_service.fetch_client_mix_metrics(
        async_session, *MARCH, 1, 3, factual_at=NOW, period_preset='month',
    )

    split_keys = [key for key in overview if 'first_visit' in key or 'repeat_visit' in key or key.endswith('_visits')]
    assert {
        'first_visits', 'repeat_visits', 'opz_first_visit_qty', 'opz_repeat_visit_qty',
        'opz_first_visit_pct', 'opz_repeat_visit_pct',
    } <= set(split_keys)
    assert {key: mix[key] for key in split_keys} == {key: overview[key] for key in split_keys}


@pytest.mark.asyncio
async def test_an_administrators_events_are_shared_between_administrators_once(async_session):
    await _seed_administrator_march(async_session)
    async_session.add(Staff(id=4, name='Second admin', position='Администратор', company_id=1))
    await async_session.commit()

    credited = []
    for staff_id in (3, 4):
        async_session.info.clear()
        metrics = (await dashboard_service.fetch_summary(
            async_session, *MARCH, company_id=1, staff_id=staff_id, factual_at=NOW,
        ))['visit_metrics']
        # The calculated events: the manual top-up is the first administrator's alone.
        credited.append((
            metrics['opz_first_visit_qty'] + metrics['opz_repeat_visit_qty'],
            metrics['first_visits'] + metrics['repeat_visits'],
        ))

    # Every event and every record goes to exactly one of them, like the Plan/fact rows.
    assert sum(qty for qty, _ in credited) == 2.0
    assert sum(records for _, records in credited) == 4
