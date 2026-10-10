"""Regression tests for the dashboard_service audit fixes (plan window cut, manual-fact write cut,
plan save conflicts and audit, KPI group races, tenant-required filters, local rollbacks)."""

from datetime import date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError

import api
import dashboard_service
from api import app
from models import (
    Company,
    Group,
    ManualFactMetric,
    PlanBranchSetting,
    PlanStaffInput,
    PortalAccount,
    PortalAuditEvent,
    ServiceKpiGroup,
    Staff,
)


async def _seed_leaving_branch(session):
    session.add(Group(id=1, title='G1'))
    session.add(Company(id=1, title='Salon', group_id=1, reporting_end_date=date(2025, 8, 31)))
    session.add(Staff(id=10, name='Barber', position='Барбер', company_id=1, fired=0))
    session.add(Staff(id=20, name='Admin', position='Администратор', company_id=1, fired=0))
    await session.flush()


@pytest.mark.asyncio
async def test_staff_plan_inputs_are_cut_by_the_reporting_window(async_session):
    """The staff plan follows the branch plan: a month after the handover keeps no plan values."""
    await _seed_leaving_branch(async_session)
    for month in (8, 9):
        async_session.add(PlanBranchSetting(
            period_start=date(2025, month, 1),
            period_end=date(2025, month, 28 if month == 8 else 30),
            company_id=1,
            updated_at=datetime(2025, 7, 1),
        ))
    for start, end in ((date(2025, 8, 1), date(2025, 8, 31)), (date(2025, 9, 1), date(2025, 9, 30))):
        async_session.add(PlanStaffInput(
            period_start=start,
            period_end=end,
            company_id=1,
            staff_id=10,
            staff_category='barber',
            clients=100,
            avg_check_total=3000,
            updated_at=datetime(2025, 7, 1),
        ))
    await async_session.commit()

    in_window, in_categories = await dashboard_service._plan_metric_components_by_staff(
        async_session, date(2025, 8, 1), date(2025, 8, 31), 1, [10]
    )
    after, after_categories = await dashboard_service._plan_metric_components_by_staff(
        async_session, date(2025, 9, 1), date(2025, 9, 30), 1, [10]
    )

    assert in_window[10]['clients'] == 100.0
    assert after[10] == {}
    # The category is not a plan value: the role of the person that month stays readable.
    assert in_categories == after_categories == {10: 'barber'}


@pytest.mark.asyncio
async def test_manual_fact_write_refuses_a_month_after_the_branch_left(async_session):
    """The editor hides such a branch, so a value POSTed into it could never be seen or cleared."""
    await _seed_leaving_branch(async_session)
    await async_session.commit()
    row = {'company_id': 1, 'staff_id': 20, 'value': 4, 'previous_value': None}
    scope = {'allowed_company_ids': [1], 'force_allowed': True, 'actor_user_id': None, 'portal_account_id': None}

    with pytest.raises(dashboard_service.ManualFactRowNotOpen):
        await dashboard_service.save_manual_facts(
            async_session, '2025-09', dashboard_service.OPZ_QTY_CODE, 1, None, [row], **scope
        )
    assert (await async_session.execute(select(ManualFactMetric))).scalars().all() == []

    # An untouched empty row is still ignored, and the last month of the window is still ours.
    await dashboard_service.save_manual_facts(
        async_session, '2025-09', dashboard_service.OPZ_QTY_CODE, 1, None,
        [{**row, 'value': None}], **scope,
    )
    await dashboard_service.save_manual_facts(
        async_session, '2025-08', dashboard_service.OPZ_QTY_CODE, 1, None, [row], **scope
    )
    stored = (await async_session.execute(select(ManualFactMetric))).scalars().all()
    assert [(item.period_start, item.value) for item in stored] == [(date(2025, 8, 1), 4.0)]


def _plan_payload():
    return (
        '2025-05',
        [{'company_id': 1, 'wax_pct': 10}],
        [{'company_id': 1, 'staff_id': 10, 'staff_category': 'barber', 'clients': 50, 'avg_check_total': 3000}],
    )


async def _seed_plan_branch(session):
    session.add(Group(id=1, title='G1'))
    session.add(Company(id=1, title='Salon', group_id=1))
    session.add(Staff(id=10, name='Barber', position='Барбер', company_id=1, fired=0))
    await session.commit()


@pytest.mark.asyncio
async def test_plan_settings_save_writes_an_audit_event(async_session):
    await _seed_plan_branch(async_session)

    await dashboard_service.save_plan_settings(
        async_session, *_plan_payload(), actor_user_id=7, portal_account_id=3
    )

    event = (await async_session.execute(select(PortalAuditEvent))).scalar_one()
    assert (event.action, event.actor_user_id, event.portal_account_id) == ('plan_settings.updated', 7, 3)
    assert event.target_id == '2025-05'
    assert event.metadata_json['edited_staff'] == [[1, 10]]


@pytest.mark.asyncio
async def test_plan_settings_insert_collision_is_a_conflict_and_leaves_no_audit(async_session, monkeypatch):
    """A concurrent save that wins the unique index must read as 409, not as a 500."""
    await _seed_plan_branch(async_session)

    async def collide():
        raise IntegrityError('insert', {}, Exception('duplicate'))

    monkeypatch.setattr(async_session, 'flush', collide)
    with pytest.raises(dashboard_service.PlanSettingsConflict):
        await dashboard_service.save_plan_settings(
            async_session, *_plan_payload(), actor_user_id=7, portal_account_id=3
        )
    monkeypatch.undo()

    assert (await async_session.execute(select(PortalAuditEvent))).scalars().all() == []
    assert (await async_session.execute(select(PlanStaffInput))).scalars().all() == []


@pytest.mark.asyncio
async def test_plan_settings_route_maps_a_conflict_to_409(async_session, monkeypatch):
    await _seed_plan_branch(async_session)

    async def conflict(*args, **kwargs):
        raise dashboard_service.PlanSettingsConflict(dashboard_service.PLAN_SETTINGS_CONFLICT_DETAIL)

    monkeypatch.setattr('dashboard_routes.save_plan_settings', conflict)

    async def override_db():
        yield async_session

    app.dependency_overrides[api.get_async_db] = override_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url='http://test') as client:
        response = await client.post(
            '/dashboard/plan/settings',
            json={'month': '2025-05', 'branches': [{'company_id': 1}], 'staff': []},
        )
    app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()['detail'] == dashboard_service.PLAN_SETTINGS_CONFLICT_DETAIL


@pytest.mark.asyncio
async def test_kpi_group_create_retries_with_the_next_suffix_when_the_code_was_taken(async_session, monkeypatch):
    """Two concurrent creates of one title: the loser's insert hits the unique index and moves on."""
    async_session.add(PortalAccount(id=1, label='t', created_at=datetime(2025, 1, 1)))
    await async_session.flush()
    first = await dashboard_service.create_service_kpi_group(async_session, portal_account_id=1, title='Wax')
    assert first['code'] == 'wax'

    real_unique_code = dashboard_service._unique_service_group_code
    calls = []

    async def stale_first_read(db, raw_code, **kwargs):
        calls.append(raw_code)
        if len(calls) == 1:
            return 'wax'  # the read the concurrent request made before the winner committed
        return await real_unique_code(db, raw_code, **kwargs)

    monkeypatch.setattr(dashboard_service, '_unique_service_group_code', stale_first_read)
    second = await dashboard_service.create_service_kpi_group(async_session, portal_account_id=1, title='Wax')

    assert len(calls) == 2
    assert second['code'] == 'wax_2'
    codes = (await async_session.execute(select(ServiceKpiGroup.code))).scalars().all()
    assert sorted(codes) == ['wax', 'wax_2']


@pytest.mark.asyncio
async def test_kpi_group_functions_require_a_tenant(async_session):
    with pytest.raises(TypeError):
        await dashboard_service.fetch_service_kpi_groups(async_session)
    with pytest.raises(TypeError):
        await dashboard_service.update_service_kpi_group(async_session, 1, title='x')

    async_session.add(PortalAccount(id=1, label='a', created_at=datetime(2025, 1, 1)))
    async_session.add(PortalAccount(id=2, label='b', created_at=datetime(2025, 1, 1)))
    await async_session.flush()
    group = await dashboard_service.create_service_kpi_group(async_session, portal_account_id=1, title='Wax')

    assert [row['id'] for row in await dashboard_service.fetch_service_kpi_groups(async_session, portal_account_id=1)] == [group['id']]
    assert await dashboard_service.fetch_service_kpi_groups(async_session, portal_account_id=2) == []
    with pytest.raises(ValueError, match='unknown KPI group'):
        await dashboard_service.update_service_kpi_group(
            async_session, group['id'], portal_account_id=2, title='stolen'
        )


@pytest.mark.asyncio
async def test_extra_service_revenue_failure_does_not_roll_back_the_request_session(async_session):
    """The failure degrades to None, and the caller's pending work survives it."""
    async_session.add(Group(id=1, title='G1'))
    await async_session.flush()
    await async_session.execute(text('DROP TABLE service_labels'))

    result = await dashboard_service._extra_service_revenue_by_staff(
        async_session, date(2025, 1, 1), date(2025, 1, 31), [1]
    )

    assert result is None
    assert [group.id for group in (await async_session.execute(select(Group))).scalars()] == [1]


def test_company_scope_clause_of_an_empty_allow_list_matches_nothing():
    clause = dashboard_service._company_scope_clause(Staff.company_id, None, [])
    assert 'IN' in str(clause)


def test_countable_manual_months_reads_today_in_the_branch_zone():
    """00:00-03:00 MSK on the 1st is still the last day of the month in UTC; the new month counts."""
    just_after_midnight_msk = datetime(2025, 9, 30, 22, 0)  # 01:00 on 1 October in Moscow

    assert dashboard_service._countable_manual_months(
        date(2025, 10, 1), date(2025, 10, 1), just_after_midnight_msk
    ) == {date(2025, 10, 1)}


@pytest.mark.asyncio
async def test_staff_schedule_coverage_is_computed_once_per_request(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1, reporting_start_date=date(2025, 1, 1)))
    await async_session.commit()
    statements = []

    def count_statements(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    factual_at = datetime(2025, 6, 1, 12, 0)
    event.listen(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)
    try:
        first = await dashboard_service._staff_schedule_coverage_info(
            async_session, date(2025, 5, 1), date(2025, 5, 31), 1, factual_at
        )
        issued = len(statements)
        second = await dashboard_service._staff_schedule_coverage_info(
            async_session, date(2025, 5, 1), date(2025, 5, 31), 1, factual_at
        )
        other_period = await dashboard_service._staff_schedule_coverage_info(
            async_session, date(2025, 5, 1), date(2025, 5, 15), 1, factual_at
        )
    finally:
        event.remove(async_session.bind.sync_engine, 'before_cursor_execute', count_statements)

    assert issued > 0
    assert second is first
    assert len(statements) > issued  # a different period is a different key and does query
    assert other_period['required_end'] == date(2025, 5, 15)


@pytest.mark.asyncio
async def test_fetch_staff_can_be_narrowed_to_one_employee(async_session):
    async_session.add(Group(id=1, title='G1'))
    async_session.add(Company(id=1, title='Salon', group_id=1))
    async_session.add_all([
        Staff(id=10, name='One', position='Барбер', company_id=1, fired=0),
        Staff(id=11, name='Two', position='Барбер', company_id=1, fired=0),
    ])
    await async_session.commit()

    rows = await dashboard_service.fetch_staff(async_session, staff_id=11)

    assert [row['id'] for row in rows] == [11]
