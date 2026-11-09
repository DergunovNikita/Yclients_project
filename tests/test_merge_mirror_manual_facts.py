"""The mirror-row merge adds each value once, to the right row, and refuses anything that is not a mirror."""
from datetime import date, datetime

import pytest
from sqlalchemy import select

from models import Company, Group, ManualFactMetric, PortalAuditEvent, Staff
import scripts.merge_mirror_manual_facts as merge

MIRROR, REAL, OTHER_BRANCH_REAL = 11, 12, 13
AUG = (date(2026, 8, 1), date(2026, 8, 31))
SEP = (date(2026, 9, 1), date(2026, 9, 30))


def _fact(staff_id, period, code, value, company_id=1):
    return ManualFactMetric(
        period_start=period[0], period_end=period[1], company_id=company_id, staff_id=staff_id,
        metric_code=code, value=value, updated_at=datetime(2026, 9, 1), updated_by_user_id=77,
    )


async def _seed(db):
    db.add_all([
        Group(id=1, title='G'),
        Company(id=1, title='A', group_id=1),
        Company(id=2, title='B', group_id=1),
    ])
    await db.flush()
    db.add_all([
        Staff(id=MIRROR, name='Mirror', position='branch_admin', company_id=1),
        Staff(id=REAL, name='Real', position='Администратор', company_id=1),
        Staff(id=OTHER_BRANCH_REAL, name='Elsewhere', position='Администратор', company_id=2),
    ])
    await db.flush()
    db.add_all([
        _fact(MIRROR, AUG, 'reviews_qty', 5.0),
        _fact(MIRROR, SEP, 'opz_qty', 1.0),
        _fact(MIRROR, SEP, 'reviews_qty', 0.0),
        _fact(REAL, AUG, 'reviews_qty', 8.0),
    ])
    await db.commit()


async def _values(db, staff_id):
    rows = (await db.execute(select(ManualFactMetric).where(ManualFactMetric.staff_id == staff_id))).scalars().all()
    return {(row.period_start, row.metric_code): row.value for row in rows}


def test_mappings_parse_and_reject_nonsense():
    assert merge.parse_mappings(['5:6', '7:8']) == {5: 6, 7: 8}
    for bad in (['5'], ['a:b'], ['5:5'], ['5:6', '5:7'], ['5:6', '6:7'], ['5:7', '6:7']):
        with pytest.raises(merge.MergeRefused):
            merge.parse_mappings(bad)


@pytest.mark.asyncio
async def test_dry_run_plans_only_non_zero_values_and_writes_nothing(async_session):
    await _seed(async_session)

    moves = await merge.plan_moves(async_session, {MIRROR: REAL})

    assert [(m.metric_code, m.period_start.month, m.value, m.target_before, m.target_after) for m in moves] == [
        ('reviews_qty', 8, 5.0, 8.0, 13.0),
        ('opz_qty', 9, 1.0, None, 1.0),
    ]
    assert await _values(async_session, REAL) == {(AUG[0], 'reviews_qty'): 8.0}
    assert 'new row' in merge.format_moves(moves)


@pytest.mark.asyncio
async def test_apply_adds_to_existing_and_creates_missing_rows_with_audit(async_session):
    await _seed(async_session)

    await merge.apply_moves(async_session, {MIRROR: REAL})
    await async_session.commit()

    assert await _values(async_session, REAL) == {(AUG[0], 'reviews_qty'): 13.0, (SEP[0], 'opz_qty'): 1.0}
    # The source rows stay as they were.
    assert await _values(async_session, MIRROR) == {
        (AUG[0], 'reviews_qty'): 5.0, (SEP[0], 'opz_qty'): 1.0, (SEP[0], 'reviews_qty'): 0.0,
    }
    updated = (await async_session.execute(
        select(ManualFactMetric).where(ManualFactMetric.staff_id == REAL, ManualFactMetric.metric_code == 'reviews_qty')
    )).scalar_one()
    assert updated.updated_by_user_id is None
    created = (await async_session.execute(
        select(ManualFactMetric).where(ManualFactMetric.staff_id == REAL, ManualFactMetric.metric_code == 'opz_qty')
    )).scalar_one()
    assert (created.period_start, created.period_end, created.company_id) == (SEP[0], SEP[1], 1)
    events = (await async_session.execute(select(PortalAuditEvent).where(PortalAuditEvent.action == merge.AUDIT_ACTION)
                                          )).scalars().all()
    assert len(events) == 2
    assert {event.metadata_json['target_after'] for event in events} == {13.0, 1.0}


@pytest.mark.asyncio
async def test_a_second_run_is_refused_and_changes_nothing(async_session):
    await _seed(async_session)
    await merge.apply_moves(async_session, {MIRROR: REAL})
    await async_session.commit()

    with pytest.raises(merge.MergeRefused, match='already merged'):
        await merge.apply_moves(async_session, {MIRROR: REAL})
    await async_session.rollback()

    assert await _values(async_session, REAL) == {(AUG[0], 'reviews_qty'): 13.0, (SEP[0], 'opz_qty'): 1.0}


@pytest.mark.asyncio
@pytest.mark.parametrize('mapping, reason', [
    ({REAL: MIRROR}, 'not a portal mirror'),
    ({MIRROR: OTHER_BRANCH_REAL}, 'different branches'),
    ({MIRROR: 999}, 'not found'),
])
async def test_unsafe_mappings_are_refused(async_session, mapping, reason):
    await _seed(async_session)

    with pytest.raises(merge.MergeRefused, match=reason):
        await merge.plan_moves(async_session, mapping)


@pytest.mark.asyncio
async def test_a_mirror_target_is_refused(async_session):
    await _seed(async_session)
    async_session.add(Staff(id=20, name='Other mirror', position='manager', company_id=1))
    await async_session.commit()

    with pytest.raises(merge.MergeRefused, match='itself a portal mirror'):
        await merge.plan_moves(async_session, {MIRROR: 20})


@pytest.mark.asyncio
async def test_a_legacy_month_split_over_two_source_rows_is_refused(async_session):
    await _seed(async_session)
    async_session.add(_fact(MIRROR, (AUG[0], date(2026, 8, 15)), 'reviews_qty', 2.0))
    await async_session.commit()

    with pytest.raises(merge.MergeRefused, match='several rows'):
        await merge.plan_moves(async_session, {MIRROR: REAL})


@pytest.mark.asyncio
async def test_an_admin_mirror_is_a_mirror_by_its_source_and_can_be_emptied_into_the_real_row(async_session):
    await _seed(async_session)
    async_session.add(Staff(id=30, name='Admin mirror', position='admin', company_id=1, source_type='portal'))
    await async_session.flush()
    async_session.add_all([_fact(30, AUG, 'reviews_qty', 3.0), _fact(30, SEP, 'opz_qty', 0.0)])
    await async_session.commit()

    await merge.apply_moves(async_session, {30: REAL}, actor_user_id=5, portal_account_id=1, delete_sources=True)
    await async_session.commit()

    assert await _values(async_session, REAL) == {(AUG[0], 'reviews_qty'): 11.0}
    # Every value leaves the mirror, the zero one too; the move is attributed to whoever asked for it.
    assert await _values(async_session, 30) == {}
    events = (await async_session.execute(select(PortalAuditEvent).where(PortalAuditEvent.action == merge.AUDIT_ACTION)
                                          )).scalars().all()
    assert [(event.actor_user_id, event.portal_account_id) for event in events] == [(5, 1)]


def _day(day):
    return (date(2026, 11, day), date(2026, 11, day))


async def _rows(db, staff_id):
    rows = (await db.execute(
        select(ManualFactMetric).where(ManualFactMetric.staff_id == staff_id).order_by(
            ManualFactMetric.period_start, ManualFactMetric.period_end, ManualFactMetric.metric_code)
    )).scalars().all()
    return [(row.period_start, row.period_end, row.metric_code, row.value) for row in rows]


@pytest.mark.asyncio
async def test_daily_rows_merge_into_the_same_day_and_create_missing_days(async_session):
    await _seed(async_session)
    async_session.add_all([
        _fact(MIRROR, _day(1), 'reviews_qty', 2.0),
        _fact(MIRROR, _day(5), 'reviews_qty', 3.0),
        _fact(REAL, _day(1), 'reviews_qty', 4.0),
    ])
    await async_session.commit()

    await merge.apply_moves(async_session, {MIRROR: REAL})
    await async_session.commit()

    assert [row for row in await _rows(async_session, REAL) if row[0].month == 11] == [
        (date(2026, 11, 1), date(2026, 11, 1), 'reviews_qty', 6.0),
        (date(2026, 11, 5), date(2026, 11, 5), 'reviews_qty', 3.0),
    ]
    events = (await async_session.execute(select(PortalAuditEvent).where(PortalAuditEvent.action == merge.AUDIT_ACTION)
                                          )).scalars().all()
    assert {(e.metadata_json['period_start'], e.metadata_json['period_end']) for e in events} >= {
        ('2026-11-01', '2026-11-01'), ('2026-11-05', '2026-11-05'), ('2026-08-01', '2026-08-31'),
    }


@pytest.mark.asyncio
async def test_a_month_row_and_a_daily_row_on_the_first_are_distinct_periods(async_session):
    await _seed(async_session)
    # Legacy August on the mirror, a daily row on 1 November on the mirror and on the target: different months.
    async_session.add_all([_fact(MIRROR, _day(1), 'reviews_qty', 1.0), _fact(REAL, _day(1), 'reviews_qty', 10.0)])
    await async_session.commit()

    moves = await merge.plan_moves(async_session, {MIRROR: REAL})

    by_period = {(m.period_start, m.period_end): (m.target_before, m.target_after) for m in moves}
    assert by_period[AUG] == (8.0, 13.0)
    assert by_period[_day(1)] == (10.0, 11.0)
    table = merge.format_moves(moves)
    assert '2026-11-01' in table and '2026-08 ' in table


@pytest.mark.asyncio
@pytest.mark.parametrize('mirror_daily', [True, False])
async def test_mixing_month_and_daily_rows_in_one_month_is_refused_and_writes_nothing(async_session, mirror_daily):
    await _seed(async_session)
    mirror_period, real_period = (_day(3), (date(2026, 11, 1), date(2026, 11, 30))) if mirror_daily else (
        (date(2026, 11, 1), date(2026, 11, 30)), _day(3))
    async_session.add_all([_fact(MIRROR, mirror_period, 'opz_qty', 2.0), _fact(REAL, real_period, 'opz_qty', 7.0)])
    await async_session.commit()
    before, mirror_before = await _rows(async_session, REAL), await _rows(async_session, MIRROR)

    with pytest.raises(merge.MergeRefused, match='opz_qty 2026-11'):
        await merge.apply_moves(async_session, {MIRROR: REAL}, delete_sources=True)
    await async_session.rollback()

    assert await _rows(async_session, REAL) == before
    assert await _rows(async_session, MIRROR) == mirror_before
    assert (await async_session.execute(select(PortalAuditEvent))).first() is None


@pytest.mark.asyncio
async def test_a_mirror_mixing_both_kinds_in_one_month_is_refused(async_session):
    await _seed(async_session)
    async_session.add_all([
        _fact(MIRROR, (date(2026, 11, 1), date(2026, 11, 30)), 'opz_qty', 2.0), _fact(MIRROR, _day(3), 'opz_qty', 1.0),
    ])
    await async_session.commit()

    with pytest.raises(merge.MergeRefused, match='both a whole-month row and daily rows'):
        await merge.plan_moves(async_session, {MIRROR: REAL})


@pytest.mark.asyncio
async def test_other_metrics_do_not_trigger_the_mixing_refusal(async_session):
    await _seed(async_session)
    async_session.add_all([_fact(MIRROR, _day(3), 'opz_qty', 2.0), _fact(REAL, (date(2026, 11, 1), date(2026, 11, 30)),
                                                                      'reviews_qty', 7.0)])
    await async_session.commit()

    moves = await merge.plan_moves(async_session, {MIRROR: REAL})

    assert (_day(3), 'opz_qty') in {((m.period_start, m.period_end), m.metric_code) for m in moves}


@pytest.mark.asyncio
async def test_a_zero_row_of_the_other_kind_does_not_block_the_merge_nor_receive_it(async_session):
    await _seed(async_session)
    nov = (date(2026, 11, 1), date(2026, 11, 30))
    async_session.add_all([
        _fact(MIRROR, _day(3), 'opz_qty', 2.0), _fact(REAL, nov, 'opz_qty', 0.0),
        _fact(MIRROR, nov, 'reviews_qty', 6.0), _fact(REAL, _day(1), 'reviews_qty', 0.0),
    ])
    await async_session.commit()

    await merge.apply_moves(async_session, {MIRROR: REAL})
    await async_session.commit()

    nov_rows = [row for row in await _rows(async_session, REAL) if row[0].month == 11]
    assert sorted(nov_rows) == sorted([
        (date(2026, 11, 1), nov[1], 'opz_qty', 0.0),
        (date(2026, 11, 1), date(2026, 11, 1), 'reviews_qty', 0.0),
        (date(2026, 11, 1), nov[1], 'reviews_qty', 6.0),
        (date(2026, 11, 3), date(2026, 11, 3), 'opz_qty', 2.0),
    ])
