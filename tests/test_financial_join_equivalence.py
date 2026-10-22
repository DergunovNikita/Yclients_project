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
from sqlalchemy import select

from dashboard_service import fetch_top_services, financial_appointment_match_condition
from models import Appointment, Company, FinancialTransaction, Transaction


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
        sold_item_id=10,
        sold_item_type='service',
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


def _service_line(line_id, appointment_id):
    return Transaction(
        id=line_id, appointment_id=appointment_id, company_id=1, service_id=10, service_title='Cut', amount=1,
    )


@pytest.mark.asyncio
async def test_hot_paths_count_each_payment_once(async_session):
    """Service revenue joins payments to visits through the UNION ALL of the two branches: no payment twice."""
    await _seed(async_session)
    async_session.add_all(
        _service_line(line_id, appointment_id) for line_id, appointment_id in enumerate((500, 900, 501, 700), start=1)
    )
    await async_session.commit()

    top = await fetch_top_services(
        async_session, date(2026, 3, 1), date(2026, 3, 31), company_id=1, limit=None, factual_at=datetime(2026, 4, 1),
    )

    # 100 + 50 (payment 3 belongs to no visit).
    assert [(row['title'], row['revenue'], row['sold']) for row in top] == [('Cut', 150.0, 4)]


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
