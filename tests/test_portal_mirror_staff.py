"""Mirror `staff` rows of portal owners, branch admins and managers are accounts, not staff."""
from datetime import date, datetime

import pytest
from sqlalchemy import delete

import dashboard_service
from models import (
    Appointment,
    Company,
    Group,
    ManualFactMetric,
    PlanStaffInput,
    Staff,
)

NOW = datetime(2025, 12, 31, 12, 0)
MARCH = (date(2025, 3, 1), date(2025, 3, 31))
ADMIN_ID = 3
MIRROR_ID = 4
PORTAL_ADMIN_ID = 5
PORTAL_BARBER_ID = 6


def _visit(id, client_id, day, *, staff_id=1, created=None, attendance=1):
    return Appointment(
        id=id, company_id=1, client_id=client_id, staff_id=staff_id, date=day,
        datetime=datetime.combine(day, datetime.min.time()).replace(hour=12),
        attendance=attendance, create_date=created or datetime(2024, 1, 1, 9),
    )


def _manual(staff_id, code, value):
    return ManualFactMetric(
        period_start=date(2025, 3, 1), period_end=date(2025, 3, 31), company_id=1, staff_id=staff_id,
        metric_code=code, value=value, updated_at=datetime(2025, 3, 28, 10),
    )


async def _seed(async_session, *, mirror):
    async_session.add_all([
        Group(id=1, title='Group'),
        Company(id=1, title='Salon', timezone='UTC', group_id=1),
        Staff(id=1, name='Barber one', position='Барбер', company_id=1),
        Staff(id=2, name='Barber two', position='Барбер', company_id=1),
        Staff(id=ADMIN_ID, name='Admin', position='Администратор', company_id=1),
        Staff(id=PORTAL_ADMIN_ID, name='Portal stand', position='admin', company_id=1),
        Staff(id=PORTAL_BARBER_ID, name='Portal chair', position='barber', company_id=1),
    ])
    if mirror:
        async_session.add(Staff(id=MIRROR_ID, name='Branch admin', position='branch_admin', company_id=1))
    await async_session.flush()
    async_session.add_all([
        _visit(102, 2, date(2025, 2, 10)),
        _visit(103, 2, date(2025, 3, 25), attendance=0, created=datetime(2025, 2, 10, 13)),
        _visit(1, 1, date(2025, 3, 3)),
        _visit(2, 1, date(2025, 3, 20), staff_id=2, created=datetime(2025, 3, 3, 13)),
        _visit(3, 1, date(2025, 4, 10), attendance=0, created=datetime(2025, 3, 20, 13)),
        _visit(4, 2, date(2025, 1, 10)),
        _visit(5, 2, date(2025, 3, 5)),
        _visit(6, 3, date(2025, 3, 7)),
        _visit(7, 4, date(2025, 3, 8), staff_id=PORTAL_BARBER_ID),
        _manual(ADMIN_ID, 'reviews_qty', 2.0),
        _manual(ADMIN_ID, 'opz_qty', 1.0),
        PlanStaffInput(
            period_start=MARCH[0], period_end=MARCH[1], company_id=1, staff_id=ADMIN_ID,
            staff_category='administrator', updated_at=NOW,
        ),
    ])
    if mirror:
        async_session.add_all([
            # A legacy plan category must not bring the row back as an administrator.
            PlanStaffInput(
                period_start=MARCH[0], period_end=MARCH[1], company_id=1, staff_id=MIRROR_ID,
                staff_category='administrator', updated_at=NOW,
            ),
            _manual(MIRROR_ID, 'reviews_qty', 5.0),
            _manual(MIRROR_ID, 'opz_qty', 4.0),
        ])
    await async_session.commit()


async def _plan_fact(async_session, **kwargs):
    async_session.info.clear()
    return await dashboard_service.fetch_plan_fact(async_session, *MARCH, company_id=1, factual_at=NOW, **kwargs)


async def _remove_mirror(async_session):
    async_session.info.clear()
    for model in (ManualFactMetric, PlanStaffInput):
        await async_session.execute(delete(model).where(model.staff_id == MIRROR_ID))
    await async_session.execute(delete(Staff).where(Staff.id == MIRROR_ID))
    await async_session.commit()


def _staff_ids(plan_fact):
    return {group['staff_id'] for group in plan_fact['groups'] if group.get('staff_id') is not None}


@pytest.mark.asyncio
async def test_mirror_row_is_absent_from_plan_fact_and_changes_no_number(async_session):
    await _seed(async_session, mirror=True)

    with_mirror = await _plan_fact(async_session)
    network_with_mirror = await dashboard_service.fetch_plan_fact(async_session, *MARCH, factual_at=NOW)
    summary_with_mirror = await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, factual_at=NOW, period_preset='month'
    )
    await _remove_mirror(async_session)
    without_mirror = await _plan_fact(async_session)
    network_without_mirror = await dashboard_service.fetch_plan_fact(async_session, *MARCH, factual_at=NOW)
    async_session.info.clear()
    summary_without_mirror = await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, factual_at=NOW, period_preset='month'
    )

    assert MIRROR_ID not in _staff_ids(with_mirror)
    # The stand and the chair of portal admin/barber accounts stay staff.
    assert {ADMIN_ID, PORTAL_ADMIN_ID, PORTAL_BARBER_ID} <= _staff_ids(with_mirror)
    assert with_mirror['groups'] == without_mirror['groups']
    assert with_mirror['parent_group'] == without_mirror['parent_group']
    assert network_with_mirror['groups'] == network_without_mirror['groups']
    assert summary_with_mirror['visit_metrics'] == summary_without_mirror['visit_metrics']


@pytest.mark.asyncio
async def test_mirror_row_is_not_in_the_editors_and_cannot_be_written(async_session):
    await _seed(async_session, mirror=True)

    for code, fetch in (
        ('reviews_qty', dashboard_service.fetch_manual_review_facts),
        ('opz_qty', dashboard_service.fetch_manual_opz_facts),
    ):
        editor = await fetch(async_session, '2025-03', company_id=1, portal_account_id=None)
        assert MIRROR_ID not in {row['staff_id'] for row in editor['rows']}
        assert ADMIN_ID in {row['staff_id'] for row in editor['rows']}
        with pytest.raises(dashboard_service.ManualFactConflict):
            # The stored value on the mirror is not shown, so a stale editor that still shows it is a conflict.
            await dashboard_service.save_manual_facts(
                async_session, '2025-03', code, 1, None,
                [{'company_id': 1, 'staff_id': MIRROR_ID, 'value': 9, 'previous_value': 5}],
                actor_user_id=None, portal_account_id=None,
            )
        with pytest.raises(dashboard_service.ManualFactRowNotOpen):
            await dashboard_service.save_manual_facts(
                async_session, '2025-03', code, 1, None,
                [{'company_id': 1, 'staff_id': MIRROR_ID, 'value': 9, 'previous_value': None}],
                actor_user_id=None, portal_account_id=None,
            )


@pytest.mark.asyncio
async def test_mirror_row_is_not_in_plan_settings_or_the_staff_list(async_session):
    await _seed(async_session, mirror=True)

    settings = await dashboard_service.fetch_plan_settings(async_session, '2025-03')
    listed = await dashboard_service.fetch_staff(async_session, include_staff_ids=[MIRROR_ID])

    assert MIRROR_ID not in {row['staff_id'] for row in settings['staff']}
    assert MIRROR_ID not in {row['id'] for row in listed}
    assert {ADMIN_ID, PORTAL_ADMIN_ID, PORTAL_BARBER_ID} <= {row['staff_id'] for row in settings['staff']}


@pytest.mark.asyncio
async def test_selecting_a_mirror_row_explicitly_answers_like_an_empty_staff(async_session):
    await _seed(async_session, mirror=True)

    plan_fact = await _plan_fact(async_session, staff_id=MIRROR_ID)
    async_session.info.clear()
    summary = await dashboard_service.fetch_summary(
        async_session, *MARCH, company_id=1, staff_id=MIRROR_ID, factual_at=NOW, period_preset='month'
    )

    assert plan_fact['groups'] == []
    assert summary['service_attribution']['mode'] != 'administrator_schedule'


@pytest.mark.asyncio
async def test_saving_the_plan_survives_a_fired_mirror_row_with_a_saved_plan_input(async_session):
    """A deactivated account's fired mirror is carried over like any fired employee, but it is refused as staff."""
    await _seed(async_session, mirror=True)
    mirror = await async_session.get(Staff, MIRROR_ID)
    mirror.fired = 1
    await async_session.commit()
    settings = await dashboard_service.fetch_plan_settings(async_session, '2025-03')
    barber = next(row for row in settings['staff'] if row['staff_id'] == 1)

    await dashboard_service.save_plan_settings(
        async_session,
        '2025-03',
        [{'company_id': 1, 'wax_pct': 10}],
        [{
            'company_id': 1, 'staff_id': 1, 'staff_category': barber['staff_category'],
            'clients': 40, 'avg_check_total': 1000,
        }],
        actor_user_id=7,
        portal_account_id=3,
    )
