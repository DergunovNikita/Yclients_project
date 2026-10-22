"""The SQL prefilter in `_opz_events` must be invisible: same events as the plain rule over all rows.

The reference below is the rule stated in Python with no SQL narrowing at all — every completed
visit of the client, the booking's local day from its own timezone. Timezones at both extremes
and bookings straddling UTC midnight are what would break a prefilter window that is too tight.
"""
import random
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import literal, select
from sqlalchemy.dialects import postgresql

import dashboard_service
from models import Appointment, Company

START, END = date(2025, 1, 1), date(2025, 6, 30)
NOW = datetime(2025, 12, 31, 12, 0)


def _reference(appointments, tz_name, start, end):
    tz = ZoneInfo(tz_name)
    visits = [a for a in appointments if a.attendance == 1 and a.client_id is not None and a.date is not None
              and a.date <= end]
    events, seen = [], set()
    for c in sorted((a for a in appointments if a.client_id is not None and a.date is not None
                     and a.create_date is not None and a.create_date <= NOW), key=lambda a: (a.create_date, a.id)):
        local = c.create_date.replace(tzinfo=UTC).astimezone(tz).date()
        if local > end:
            continue
        earlier = [v for v in visits if v.client_id == c.client_id and v.date <= local]
        if not earlier:
            continue
        last = max(earlier, key=lambda v: (v.date, v.datetime or datetime.combine(v.date, time.min), v.id))
        if c.date <= last.date or local not in {last.date, last.date + timedelta(days=1)}:
            continue
        # One event per anchor visit, its first booking, whenever that booking was made.
        if last.id in seen:
            continue
        seen.add(last.id)
        if local < start:
            continue
        first_visit = not any(v.client_id == c.client_id and v.date < last.date for v in visits)
        events.append((c.id, local, last.date, last.staff_id, first_visit))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'tz_name', ['Pacific/Kiritimati', 'Europe/Moscow', 'UTC', 'America/Los_Angeles', 'Pacific/Pago_Pago']
)
# 0 is the path taken past the limit: the visits read is not narrowed by candidate ids
@pytest.mark.parametrize('survivor_limit', [20000, 0])
async def test_opz_events_match_the_unfiltered_rule(async_session, monkeypatch, tz_name, survivor_limit):
    monkeypatch.setattr(dashboard_service, 'OPZ_SURVIVOR_ID_LIMIT', survivor_limit)
    rng = random.Random(7)
    async_session.add(Company(id=1, title='Branch', timezone=tz_name))
    rows, next_id = [], 1
    for client in range(1, 41):
        for _ in range(rng.randint(1, 8)):
            day = START + timedelta(days=rng.randint(-3, 190))
            moment = datetime.combine(day, time(rng.randint(5, 20), rng.choice([0, 30])))
            rows.append(Appointment(id=next_id, company_id=1, client_id=client, staff_id=rng.randint(1, 3), date=day,
                                    datetime=moment, attendance=rng.choice([1, 1, 1, 0, 2]),
                                    # created on the visit day or around it, hours chosen to straddle UTC midnight
                                    create_date=datetime.combine(day + timedelta(days=rng.randint(-3, 1)),
                                                                 time(rng.choice([0, 1, 9, 13, 14, 22, 23]), 30)),
                                    created_user_id=rng.randint(1, 2)))
            next_id += 1
    # Visits on the last day of a month, rebooked that day and again the next morning: the second
    # booking falls into the next month and must not count there a second time.
    for client, month in zip(range(41, 51), [1, 2, 3, 4, 5] * 2):
        day = date(2025, month + 1, 1) - timedelta(days=1)
        rows.append(Appointment(id=next_id, company_id=1, client_id=client, staff_id=1, date=day,
                                datetime=datetime.combine(day, time(10)), attendance=1,
                                create_date=datetime.combine(day - timedelta(days=5), time(9)), created_user_id=1))
        for created in (datetime.combine(day, time(rng.choice([5, 13, 20]), 30)),
                        datetime.combine(day + timedelta(days=1), time(rng.choice([1, 9, 15]), 30))):
            next_id += 1
            rows.append(Appointment(id=next_id, company_id=1, client_id=client, staff_id=1,
                                    date=day + timedelta(days=20), attendance=0, create_date=created,
                                    created_user_id=1))
        next_id += 1
    async_session.add_all(rows)
    await async_session.commit()

    got = await dashboard_service._opz_events(async_session, START, END, 1, factual_at=NOW)
    assert [(e.event_id, e.event_date, e.last_visit_date, e.barber_staff_id, e.first_visit) for e in got] == _reference(
        rows, tz_name, START, END)
    # a vacuous comparison would prove nothing
    assert {e.first_visit for e in got} == {True, False}

    # Months add up to the window: an anchor rebooked on the eve of a month belongs to one month only.
    month_starts = [date(2025, month, 1) for month in range(1, 8)]
    parts = [
        event
        for month_start, next_start in zip(month_starts, month_starts[1:])
        for event in await dashboard_service._opz_events(
            async_session, month_start, next_start - timedelta(days=1), 1, factual_at=NOW,
        )
    ]
    assert sorted((e.event_id, e.first_visit) for e in parts) == sorted((e.event_id, e.first_visit) for e in got)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ('tz_name', 'created_utc', 'visit_day'),
    [
        # local day is the UTC day + 1, and the anchor is that very local day
        ('Pacific/Kiritimati', datetime(2025, 3, 10, 23, 30), date(2025, 3, 11)),
        # local day is the UTC day - 1, and the anchor is the day before it: UTC day - 2
        ('Pacific/Pago_Pago', datetime(2025, 3, 11, 0, 30), date(2025, 3, 9)),
    ],
)
async def test_opz_event_is_found_at_the_edges_of_the_prefilter_window(async_session, tz_name, created_utc, visit_day):
    async_session.add(Company(id=1, title='Branch', timezone=tz_name))
    async_session.add_all([
        Appointment(id=1, company_id=1, client_id=7, staff_id=3, date=visit_day,
                    datetime=datetime.combine(visit_day, time(12)), attendance=1,
                    create_date=datetime(2025, 3, 1, 9)),
        Appointment(id=2, company_id=1, client_id=7, staff_id=3, date=date(2025, 3, 25),
                    datetime=datetime(2025, 3, 25, 12), attendance=0, create_date=created_utc),
    ])
    await async_session.commit()

    events = await dashboard_service._opz_events(async_session, date(2025, 3, 1), date(2025, 3, 31), 1, factual_at=NOW)

    assert [(e.event_id, e.last_visit_date) for e in events] == [(2, visit_day)]


def _shifted(days):
    return select(dashboard_service._DayShift(literal('2025-03-10'), days))


@pytest.mark.parametrize(('days', 'sql'), [(-2, '- 2)'), (1, '+ 1)')])
def test_day_shift_renders_postgres_date_arithmetic(days, sql):
    compiled = str(_shifted(days).compile(dialect=postgresql.dialect()))
    assert f'(CAST(%(param_1)s AS DATE) {sql}' in compiled


@pytest.mark.asyncio
async def test_day_shift_statements_do_not_share_a_cached_sql_string(async_session):
    # The same engine compiles each statement once and reuses it by cache key, so alternating
    # shifts is what would expose a key that ignores the day count.
    results = [(await async_session.execute(_shifted(days))).scalar_one() for days in (-2, 3, -2, 3)]

    assert results == [date(2025, 3, 8), date(2025, 3, 13), date(2025, 3, 8), date(2025, 3, 13)]
