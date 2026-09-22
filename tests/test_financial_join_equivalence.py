"""The invariant every UNION ALL rewrite in dashboard_service rests on.

`financial_appointment_match_condition()` is an OR across two different columns, which
PostgreSQL cannot hash- or merge-join, so nine hot call sites now join through
`_financial_appointment_branches_union()` instead. That substitution is only valid because
the two branches never match the same (FinancialTransaction, Appointment) pair: the direct
branch needs `external_id` non-null (SQL `NULL = x` is never true) and the fallback branch
requires `external_id IS NULL`.

If that ever stops holding, every rewritten site silently double-counts money — and nothing
would catch it in CI. The behaviour gate that did catch this class of problem
(scripts/report_payload_snapshot.py) runs out of band against a copy of production, so it
protects a release, not a commit. These tests pin the invariant itself.

The fallback's `~has_external_match` guard matters as much as the null check: a transaction
whose record_id is some *other* appointment's external_id must not also attach to an
unrelated appointment whose internal id happens to collide with it. The third case below is
exactly that collision, which is why the ids are chosen to overlap.
"""
from datetime import date, datetime

import pytest
from sqlalchemy import and_, func, select

from dashboard_service import (
    _financial_appointment_branches_union,
    _financial_appointment_direct_condition,
    _financial_appointment_fallback_condition,
    financial_appointment_match_condition,
)
from models import Appointment, Company, FinancialTransaction


def _appointment(appointment_id: int, external_id: int | None) -> Appointment:
    return Appointment(
        id=appointment_id,
        company_id=1,
        external_id=external_id,
        client_id=appointment_id,
        staff_id=1,
        date=date(2026, 3, 1),
        datetime=datetime(2026, 3, 1, 10, 0),
        attendance=1,
    )


def _payment(transaction_id: int, record_id: int, amount: float) -> FinancialTransaction:
    return FinancialTransaction(
        id=transaction_id,
        company_id=1,
        record_id=record_id,
        amount=amount,
        date=datetime(2026, 3, 1, 12, 0),
    )


async def _seed(session):
    session.add(Company(id=1, title='Branch'))
    # id 500 / external 900: matches only the direct branch.
    session.add(_appointment(500, 900))
    # id 900 / external NULL: its internal id collides with the row above's external id, so
    # the fallback branch must refuse the payment that already belongs to that appointment.
    session.add(_appointment(900, None))
    # id 501 / external NULL, no competing external_id anywhere: the fallback's own case.
    session.add(_appointment(501, None))
    # id 700 / external 701: the case that discriminates. Payment 3's record_id equals this
    # appointment's INTERNAL id while the appointment still has a non-null external_id, and no
    # appointment anywhere owns 700 as its external_id -- so `~has_external_match` is satisfied
    # and `external_id IS NULL` is the ONLY thing keeping the fallback branch off this pair.
    session.add(_appointment(700, 701))
    session.add(_payment(1, 900, 100.0))   # -> appointment 500 via external_id, NOT 900
    session.add(_payment(2, 501, 50.0))    # -> appointment 501 via internal id
    session.add(_payment(3, 700, 25.0))    # -> nothing: 700 is an internal id of a row that has one
    await session.commit()


def _or_join_total():
    return (
        select(func.coalesce(func.sum(FinancialTransaction.amount), 0.0))
        .select_from(FinancialTransaction)
        .join(Appointment, financial_appointment_match_condition())
    )


def _union_total():
    branches = _financial_appointment_branches_union(
        lambda join_condition: (
            select(FinancialTransaction.amount.label('amount'))
            .select_from(FinancialTransaction)
            .join(Appointment, join_condition)
        )
    )
    return select(func.coalesce(func.sum(branches.c.amount), 0.0))


@pytest.mark.asyncio
async def test_union_all_total_matches_the_or_join(async_session):
    """The substitution must not change the money."""
    await _seed(async_session)

    or_total = float((await async_session.execute(_or_join_total())).scalar_one())
    union_total = float((await async_session.execute(_union_total())).scalar_one())

    assert or_total == union_total == 150.0


@pytest.mark.asyncio
async def test_no_pair_satisfies_both_branches(async_session):
    """Disjointness asserted against the conditions themselves, not via the two formulations.

    Comparing `or_(A, B)` to `union_all(A, B)` cannot catch a broken guard on its own: both
    sides are built from the same A and B, so a change to either moves them together. This
    joins on `and_(A, B)` instead — the pair set that must always be empty.

    Note what this test can and cannot catch, so nobody mistakes it for more than it is.
    The overlap is blocked twice over: `external_id IS NULL` and `~has_external_match` each
    prevent it independently, so deleting either one alone leaves this assertion green — it
    produces a spurious EXTRA match rather than a doubly-matched pair. Measured: removing the
    null guard fails the three sibling tests here and not this one. This test pins the
    property the nine rewrites actually depend on; the siblings are the ones that bite when a
    single guard goes.
    """
    await _seed(async_session)

    both = (
        await async_session.execute(
            select(func.count())
            .select_from(FinancialTransaction)
            .join(
                Appointment,
                and_(
                    _financial_appointment_direct_condition(),
                    _financial_appointment_fallback_condition(),
                ),
            )
        )
    ).scalar_one()

    assert both == 0


@pytest.mark.asyncio
async def test_fallback_does_not_claim_an_appointment_that_has_an_external_id(async_session):
    """The `external_id IS NULL` guard, isolated on the pair that only it protects.

    Payment 3 (record_id=700) collides with appointment 700's internal id, but that
    appointment carries external_id=701. Nothing owns 700 as an external_id, so
    `~has_external_match` passes and the guard is the only remaining defence.
    """
    await _seed(async_session)

    rows = (
        await async_session.execute(
            select(Appointment.id)
            .select_from(FinancialTransaction)
            .join(Appointment, financial_appointment_match_condition())
            .where(FinancialTransaction.id == 3)
        )
    ).scalars().all()

    assert rows == []


@pytest.mark.asyncio
async def test_union_branches_never_match_the_same_pair(async_session):
    """Disjointness stated directly: the union must not produce more rows than the OR join.

    A row count is the assertion that actually catches double-counting — two branches both
    matching one pair would keep the SUM wrong in the same direction but is easiest to see
    here.
    """
    await _seed(async_session)

    or_rows = (
        await async_session.execute(
            select(func.count())
            .select_from(FinancialTransaction)
            .join(Appointment, financial_appointment_match_condition())
        )
    ).scalar_one()
    branches = _financial_appointment_branches_union(
        lambda join_condition: (
            select(FinancialTransaction.id.label('ft_id'), Appointment.id.label('appt_id'))
            .select_from(FinancialTransaction)
            .join(Appointment, join_condition)
        )
    )
    union_rows = (await async_session.execute(select(func.count()).select_from(branches))).scalar_one()
    distinct_pairs = (
        await async_session.execute(
            select(func.count()).select_from(
                select(branches.c.ft_id, branches.c.appt_id).distinct().subquery()
            )
        )
    ).scalar_one()

    assert union_rows == or_rows == 2
    # No (transaction, appointment) pair appears twice: the branches partition, not overlap.
    assert distinct_pairs == union_rows


@pytest.mark.asyncio
async def test_fallback_branch_yields_to_an_appointment_that_owns_the_record_id(async_session):
    """The `~has_external_match` guard, isolated.

    Payment 1's record_id is 900. Appointment 500 owns 900 as its external_id, and
    appointment 900 has that value as its internal id with no external_id of its own.
    Only appointment 500 may claim the payment, or the money is counted against two
    branches at once.
    """
    await _seed(async_session)

    rows = (
        await async_session.execute(
            select(Appointment.id)
            .select_from(FinancialTransaction)
            .join(Appointment, financial_appointment_match_condition())
            .where(FinancialTransaction.id == 1)
        )
    ).scalars().all()

    assert rows == [500]
