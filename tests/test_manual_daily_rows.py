"""Manual reviews and OPZ top-ups are stored per day; whole-month rows are the legacy shape.

A daily row counts for any window containing its day, a month row only for a window that covers
the month. Every screen reads them through the same rule, so the screens must agree.
"""
from datetime import date, datetime

import pytest

import dashboard_service
from models import Company, ManualFactMetric, PlanMetric, Staff

NOW = datetime(2026, 11, 20, 12, 0)


async def _seed(async_session, **company):
    async_session.add_all([
        Company(id=1, title='Salon', timezone='UTC', **company),
        Staff(id=1, name='Barber', position='Барбер', company_id=1),
        Staff(id=2, name='Admin', position='Администратор', company_id=1),
        Staff(id=3, name='Not an admin', position='Барбер', company_id=1),
    ])
    # The plan makes the administrator row visible and fixes who was an administrator in the month.
    async_session.add_all([
        PlanMetric(
            period_start=date(2026, month, 1),
            period_end=date(2026, month, 28),
            company_id=1,
            staff_id=2,
            staff_category='administrator',
            metric_code='reviews_qty',
            value=10.0,
            updated_at=datetime(2026, 1, 1),
        )
        for month in (9, 10, 11)
    ])
    await async_session.commit()


async def _add(async_session, code, day, value, staff_id=2, month_row=False):
    start = day.replace(day=1)
    end = date(day.year, day.month, 28) if month_row else day
    async_session.add(ManualFactMetric(
        period_start=start if month_row else day, period_end=end, company_id=1, staff_id=staff_id,
        metric_code=code, value=value, updated_at=datetime(2026, 1, 1),
    ))
    await async_session.commit()


async def _plan_fact(async_session, start, end):
    """(administrator row, branch row) facts of reviews and OPZ."""
    data = await dashboard_service.fetch_plan_fact(async_session, start, end, company_id=1, factual_at=NOW)
    admin = next(group for group in data['groups'] if group['staff_id'] == 2)

    def facts(group):
        return {cell['code']: cell['fact'] for cell in group['metrics'] if cell['code'] in ('reviews_qty', 'opz_qty')}

    return facts(admin), facts(data['parent_group'])


@pytest.mark.asyncio
async def test_a_daily_row_shows_for_its_day_and_not_for_the_next(async_session):
    await _seed(async_session)
    day = date(2026, 11, 4)
    await _add(async_session, 'reviews_qty', day, 3.0)
    await _add(async_session, 'opz_qty', day, 2.0)
    await _add(async_session, 'reviews_qty', date(2026, 11, 5), 5.0)

    admin, branch = await _plan_fact(async_session, day, day)
    assert admin == branch == {'reviews_qty': 3.0, 'opz_qty': 2.0}


@pytest.mark.asyncio
async def test_a_week_sums_daily_rows_across_months_and_takes_only_covered_legacy_months(async_session):
    await _seed(async_session)
    await _add(async_session, 'reviews_qty', date(2026, 10, 29), 1.0)
    await _add(async_session, 'reviews_qty', date(2026, 11, 2), 2.0)
    await _add(async_session, 'opz_qty', date(2026, 10, 29), 4.0)
    await _add(async_session, 'opz_qty', date(2026, 11, 2), 5.0)
    await _add(async_session, 'reviews_qty', date(2026, 9, 1), 10.0, month_row=True)
    await _add(async_session, 'opz_qty', date(2026, 9, 1), 20.0, month_row=True)

    week, _ = await _plan_fact(async_session, date(2026, 10, 28), date(2026, 11, 3))
    assert week == {'reviews_qty': 3.0, 'opz_qty': 9.0}

    part_of_september, _ = await _plan_fact(async_session, date(2026, 9, 10), date(2026, 9, 30))
    assert part_of_september == {'reviews_qty': 0.0, 'opz_qty': 0.0}

    september, _ = await _plan_fact(async_session, date(2026, 9, 1), date(2026, 9, 30))
    assert september == {'reviews_qty': 10.0, 'opz_qty': 20.0}


@pytest.mark.asyncio
async def test_a_legacy_reviews_month_is_not_split_across_its_days(async_session):
    await _seed(async_session)
    await _add(async_session, 'reviews_qty', date(2026, 10, 1), 7.0, month_row=True)

    one_day, branch = await _plan_fact(async_session, date(2026, 10, 5), date(2026, 10, 5))
    assert one_day['reviews_qty'] == branch['reviews_qty'] == 0.0

    whole, branch = await _plan_fact(async_session, date(2026, 10, 1), date(2026, 10, 31))
    assert whole['reviews_qty'] == branch['reviews_qty'] == 7.0


@pytest.mark.asyncio
async def test_a_daily_row_of_someone_who_was_not_an_administrator_does_not_count(async_session):
    await _seed(async_session)
    day = date(2026, 11, 4)
    await _add(async_session, 'reviews_qty', day, 3.0)
    await _add(async_session, 'reviews_qty', day, 9.0, staff_id=3)
    await _add(async_session, 'opz_qty', day, 9.0, staff_id=3)

    admin, branch = await _plan_fact(async_session, day, day)
    assert admin['reviews_qty'] == branch['reviews_qty'] == 3.0
    assert branch['opz_qty'] == 0.0


@pytest.mark.asyncio
async def test_the_reporting_window_end_day_counts_and_the_day_after_does_not(async_session):
    await _seed(async_session, reporting_end_date=date(2026, 11, 10))
    await _add(async_session, 'reviews_qty', date(2026, 11, 10), 3.0)
    await _add(async_session, 'reviews_qty', date(2026, 11, 11), 5.0)

    _, both_days = await _plan_fact(async_session, date(2026, 11, 10), date(2026, 11, 11))
    assert both_days['reviews_qty'] == 3.0


@pytest.mark.asyncio
async def test_overview_plan_fact_and_year_over_year_agree_on_daily_opz(async_session):
    await _seed(async_session)
    await _add(async_session, 'opz_qty', date(2026, 11, 3), 2.0)
    await _add(async_session, 'opz_qty', date(2026, 11, 5), 3.0)
    await _add(async_session, 'opz_qty', date(2026, 12, 1), 7.0)
    start, end = date(2026, 11, 1), date(2026, 11, 30)

    summary = await dashboard_service.fetch_summary(async_session, start, end, company_id=1, factual_at=NOW)
    _, branch = await _plan_fact(async_session, start, end)
    yoy = await dashboard_service.fetch_opz_year_facts(
        async_session, start, end, company_id=1, staff_id=None, factual_at=NOW
    )
    assert summary['visit_metrics']['opz_qty'] == branch['opz_qty'] == yoy['monthly_counts'][2026][11] == 5.0

    filtered = await dashboard_service.fetch_summary(
        async_session, start, end, company_id=1, staff_id=1, factual_at=NOW
    )
    assert filtered['visit_metrics']['opz_qty'] == 0.0


@pytest.mark.asyncio
async def test_the_overview_delta_compares_daily_rows_of_both_windows(async_session):
    await _seed(async_session)
    await _add(async_session, 'opz_qty', date(2026, 10, 26), 2.0)
    await _add(async_session, 'opz_qty', date(2026, 11, 3), 4.0)

    summary = await dashboard_service.fetch_summary(
        async_session, date(2026, 11, 1), date(2026, 11, 7), company_id=1, factual_at=NOW
    )
    assert summary['visit_metrics']['opz_qty'] == 4.0
    assert summary['visit_metrics']['opz_qty_change_pct'] == 100.0


@pytest.mark.asyncio
async def test_a_selected_administrators_overview_block_and_ratings_count_the_same_daily_rows(async_session):
    await _seed(async_session)
    day, other_day = date(2026, 11, 4), date(2026, 11, 5)
    for code, value in (('reviews_qty', 2.0), ('opz_qty', 3.0)):
        await _add(async_session, code, day, value)
        await _add(async_session, code, other_day, 10.0)

    summary = await dashboard_service.fetch_summary(
        async_session, day, day, company_id=1, staff_id=2, factual_at=NOW
    )
    assert summary['service_attribution']['mode'] == 'administrator_schedule'
    metrics = summary['administrator_personal']['metrics']
    assert metrics['reviews_qty']['value'] == 2.0
    assert metrics['opz_qty']['value'] == summary['administrator_personal']['opz_manual_qty'] == 3.0
    assert summary['visit_metrics']['opz_qty'] == 3.0

    async_session.info.clear()
    ratings = await dashboard_service.fetch_plan_fact(
        async_session, day, day, company_id=1, factual_at=NOW, for_ratings=True
    )
    assert {row['staff_id']: row['value'] for row in ratings['staff_leaderboards']['reviews_admin']} == {2: 2.0}
