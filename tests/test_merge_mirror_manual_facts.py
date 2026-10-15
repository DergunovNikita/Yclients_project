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
