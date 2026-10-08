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


def _reference(appointments, tz_name, start, end, *, dedup):
    tz = ZoneInfo(tz_name)
    visits = [a for a in appointments if a.attendance == 1 and a.client_id is not None and a.date is not None
              and start - timedelta(days=1) <= a.date <= end]
    events, seen = [], set()
    for c in sorted((a for a in appointments if a.client_id is not None and a.date is not None
                     and a.create_date is not None and a.create_date <= NOW), key=lambda a: (a.create_date, a.id)):
        local = c.create_date.replace(tzinfo=UTC).astimezone(tz).date()
        if not (start <= local <= end):
            continue
        earlier = [v for v in visits if v.client_id == c.client_id and v.date <= local]
        if not earlier:
            continue
        last = max(earlier, key=lambda v: (v.date, v.datetime or datetime.combine(v.date, time.min), v.id))
        if c.date <= last.date or local not in {last.date, last.date + timedelta(days=1)}:
            continue
        key = (c.client_id, *{'period': (), 'year': (local.year,), 'month': (local.year, local.month)}[dedup])
        if key in seen:
            continue
        seen.add(key)
        events.append((c.id, local, last.date, last.staff_id))
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'tz_name', ['Pacific/Kiritimati', 'Europe/Moscow', 'UTC', 'America/Los_Angeles', 'Pacific/Pago_Pago']
)
@pytest.mark.parametrize('dedup', ['period', 'year', 'month'])
# 0 is the path taken past the limit: the visits read is not narrowed by candidate ids
@pytest.mark.parametrize('survivor_limit', [20000, 0])
async def test_opz_events_match_the_unfiltered_rule(async_session, monkeypatch, tz_name, dedup, survivor_limit):
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
    async_session.add_all(rows)
    await async_session.commit()

    got = await dashboard_service._opz_events(async_session, START, END, 1, deduplicate_by=dedup, factual_at=NOW)
    assert [(e.event_id, e.event_date, e.last_visit_date, e.barber_staff_id) for e in got] == _reference(
        rows, tz_name, START, END, dedup=dedup)
    assert got  # a vacuous comparison would prove nothing


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


def test_day_shift_is_part_of_the_statement_cache_key():
    assert _shifted(-2)._generate_cache_key() != _shifted(3)._generate_cache_key()
    assert _shifted(3)._generate_cache_key() == _shifted(3)._generate_cache_key()


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
