"""The manual-fact editors work on one day; whole-month rows from before daily entry are read-only legacy."""
from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

import api
from api import app
from models import Company, Group, ManualFactMetric, PortalAuditEvent, Staff

REVIEWS = '/dashboard/plan/reviews_fact'
OPZ = '/dashboard/plan/opz_fact'


async def _seed(async_session, **company):
    async_session.add(Group(id=1, title='G'))
    async_session.add(Company(id=1, title='Salon', group_id=1, **company))
    async_session.add_all([
        Staff(id=2, name='Anna', position='Администратор', company_id=1, fired=0),
        Staff(id=3, name='Bella', position='Администратор', company_id=1, fired=0),
    ])
    await async_session.commit()


def _row(staff_id, start, end, value, code='reviews_qty'):
    return ManualFactMetric(
        period_start=start, period_end=end, company_id=1, staff_id=staff_id, metric_code=code,
        value=value, updated_at=datetime(2025, 1, 1),
    )


async def _request(async_session, method, path, **kwargs):
    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            return await client.request(method, path, **kwargs)
    finally:
        app.dependency_overrides.clear()


async def _stored(async_session):
    rows = (await async_session.execute(select(ManualFactMetric))).scalars().all()
    return sorted((row.staff_id, row.period_start, row.period_end, row.value) for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize('path', [REVIEWS, OPZ])
@pytest.mark.parametrize(
    'day', ['2025-03', '20250315', '2025-03-32', '2025-02-30', '2025-3-5', 'yesterday', '2999-01-01']
)
async def test_a_malformed_or_future_day_is_a_400_on_read_and_write(async_session, path, day):
    await _seed(async_session)

    read = await _request(async_session, 'GET', path, params={'day': day, 'company_id': 1})
    write = await _request(async_session, 'POST', path, json={
        'day': day, 'company_id': 1, 'items': [{'company_id': 1, 'staff_id': 2, 'value': 1, 'previous_value': None}],
    })

    assert (read.status_code, write.status_code) == (400, 400), (read.text, write.text)
    assert 'day' in read.json()['detail'] and 'day' in write.json()['detail']
    assert await _stored(async_session) == []


@pytest.mark.asyncio
async def test_a_legacy_month_row_closes_that_row_for_day_entry_and_is_never_touched(async_session):
    await _seed(async_session)
    march = (date(2025, 3, 1), date(2025, 3, 31))
    async_session.add_all([
        _row(2, *march, 40.0),                              # Anna: legacy month value
        _row(3, date(2025, 3, 14), date(2025, 3, 14), 2.0),  # Bella: daily rows only
        _row(3, date(2025, 2, 28), date(2025, 2, 28), 9.0),
    ])
    await async_session.commit()

    editor = await _request(async_session, 'GET', REVIEWS, params={'day': '2025-03-15', 'company_id': 1})
    rows = {row['staff_id']: row for row in editor.json()['data']['rows']}

    assert (rows[2]['value'], rows[2]['month_value'], rows[2]['legacy_month_value'], rows[2]['editable']) == (
        None, 40, 40, False
    )
    assert (rows[3]['value'], rows[3]['month_value'], rows[3]['legacy_month_value'], rows[3]['editable']) == (
        None, 2, None, True
    )

    def post(items):
        return _request(
            async_session, 'POST', REVIEWS, json={'day': '2025-03-15', 'company_id': 1, 'items': items}
        )

    refused = await post([{'company_id': 1, 'staff_id': 2, 'value': 5, 'previous_value': None}])
    assert refused.status_code == 400
    assert 'staff' not in refused.json()['detail']
    assert (await _stored(async_session))[0] == (2, *march, 40.0)
    assert len(await _stored(async_session)) == 3

    # An untouched locked row rides along; only the edited row is written, on its own day.
    saved = await post([
        {'company_id': 1, 'staff_id': 2, 'value': None, 'previous_value': None},
        {'company_id': 1, 'staff_id': 3, 'value': 6, 'previous_value': None},
    ])
    assert saved.status_code == 200, saved.text
    assert await _stored(async_session) == [
        (2, *march, 40.0),
        (3, date(2025, 2, 28), date(2025, 2, 28), 9.0),
        (3, date(2025, 3, 14), date(2025, 3, 14), 2.0),
        (3, date(2025, 3, 15), date(2025, 3, 15), 6.0),
    ]
    bella = next(row for row in saved.json()['data']['rows'] if row['staff_id'] == 3)
    assert (bella['value'], bella['month_value']) == (6, 8)
    assert saved.json()["data"]["month_total_value"] == 48


@pytest.mark.asyncio
async def test_a_legacy_row_of_another_month_does_not_lock_the_day(async_session):
    await _seed(async_session)
    async_session.add(_row(2, date(2025, 2, 1), date(2025, 2, 28), 40.0))
    await async_session.commit()

    saved = await _request(async_session, 'POST', REVIEWS, json={
        'day': '2025-03-01', 'company_id': 1,
        'items': [{'company_id': 1, 'staff_id': 2, 'value': 3, 'previous_value': None}],
    })

    assert saved.status_code == 200, saved.text
    assert saved.json()['data']['rows'][0]['editable'] is True


@pytest.mark.asyncio
async def test_saving_a_day_audits_that_day(async_session):
    await _seed(async_session)

    await _request(async_session, 'POST', OPZ, json={
        'day': '2025-03-15', 'company_id': 1,
        'items': [{'company_id': 1, 'staff_id': 2, 'value': 2, 'previous_value': None}],
    })

    event = (await async_session.execute(
        select(PortalAuditEvent).where(PortalAuditEvent.action == 'manual_fact.updated')
    )).scalars().one()
    assert (event.target_id, event.metadata_json['day']) == ('2025-03-15', '2025-03-15')


@pytest.mark.asyncio
async def test_a_day_before_the_branch_opened_is_marked_uncounted_and_a_day_after_it_left_is_closed(async_session):
    await _seed(async_session, reporting_start_date=date(2025, 3, 10), reporting_end_date=date(2025, 3, 20))

    def post(day, value=1):
        return _request(async_session, 'POST', OPZ, json={
            'day': day, 'company_id': 1,
            'items': [{'company_id': 1, 'staff_id': 2, 'value': value, 'previous_value': None}],
        })

    early = await post('2025-03-09')
    last = await post('2025-03-20')
    late = await post('2025-03-21')
    late_read = await _request(async_session, 'GET', OPZ, params={'day': '2025-03-21', 'company_id': 1})

    assert early.status_code == 200 and last.status_code == 200
    early_row = early.json()['data']['rows'][0]
    last_row = last.json()['data']['rows'][0]
    assert (early_row['value'], early_row['counted']) == (1, False)
    assert early.json()['data']['manual_total'] == 0
    assert (last_row['value'], last_row['counted']) == (1, True)
    assert late.status_code == 400
    assert late_read.json()['data']['rows'] == []
    assert [row[1] for row in await _stored(async_session)] == [date(2025, 3, 9), date(2025, 3, 20)]


@pytest.mark.asyncio
async def test_a_zero_legacy_month_row_does_not_lock_day_entry(async_session):
    await _seed(async_session)
    async_session.add(_row(2, date(2025, 3, 1), date(2025, 3, 31), 0.0))
    await async_session.commit()

    editor = await _request(async_session, 'GET', REVIEWS, params={'day': '2025-03-15', 'company_id': 1})
    anna = next(row for row in editor.json()['data']['rows'] if row['staff_id'] == 2)
    assert (anna['legacy_month_value'], anna['editable']) == (None, True)

    saved = await _request(async_session, 'POST', REVIEWS, json={
        'day': '2025-03-15', 'company_id': 1,
        'items': [{'company_id': 1, 'staff_id': 2, 'value': 4, 'previous_value': None}],
    })

    assert saved.status_code == 200, saved.text
    assert await _stored(async_session) == [
        (2, date(2025, 3, 1), date(2025, 3, 31), 0.0),
        (2, date(2025, 3, 15), date(2025, 3, 15), 4.0),
    ]
