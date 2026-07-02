"""Aggregated metrics for the product dashboard (JSON for SPA / Chart.js)."""

from __future__ import annotations

import asyncio
import math
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from sqlalchemy import String, and_, case, cast, delete, exists, func, or_, select
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

import yclients_analytics
from models import (
    AccountCatalog,
    Appointment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    ManualFactMetric,
    PlanBranchSetting,
    PlanMetric,
    PlanStaffInput,
    PortalBranch,
    ServiceCatalog,
    Service,
    ServiceKpiAssignment,
    ServiceKpiGroup,
    ServiceLabel,
    Staff,
    StaffSchedule,
    SyncSourceState,
    Transaction,
)
from plan_config import (
    PLAN_FACT_METRICS,
    RAW_PLAN_FACT_CODES,
    REVIEWS_QTY_CODE,
    STAFF_CATEGORY_LABELS,
    STAFF_CATEGORY_METRIC_CODES,
    is_visible_staff_plan,
    metrics_for_category,
    normalize_staff_category,
)

GOODS_SALE_TYPE_ID = 1
SERVICE_SOLD_ITEM_TYPE = 'service'
GOODS_SOLD_ITEM_TYPE = 'goods_transaction'
WAITLIST_STAFF_NAME = 'лист ожидания'
ADMIN_PLACEHOLDER_STAFF_PREFIX = 'администратор'
PLAN_SETTINGS_SOURCE = 'dashboard_plan_settings'
GOODS_KPI_CODES = ('wax_qty', 'camouflage_qty', 'face_care_qty', 'head_care_qty')
COMPLETED_ATTENDANCE = 1
PERSONAL_ACCOUNT_SOURCE = 'financial_transactions_detail'
PERSONAL_ACCOUNT_TYPES = ('client_account', 'personal_account', 'account_replenishment')
PERSONAL_ACCOUNT_EXPENSE_MARKERS = ('пополн', 'личн', 'депозит')
NON_CASH_ACCOUNT_MARKERS = ('бонус', 'скид', 'лояльн', 'сертификат')

WAX_TITLE_PARTS = ('воск',)
CAMOUFLAGE_TITLE_PARTS = ('камуфляж',)
FACE_CARE_TITLE_PARTS = (
    'spa volcano',
    'спа volcano',
    'black mask',
    'спа для лица',
    'для лица',
    'уход за кожей лица',
    'уход за бородой и кожей лица',
    'кожей лица',
)
HEAD_CARE_TITLE_PARTS = (
    'пилинг',
    'компл. мойка',
    'комплексное мытье головы',
    'комплексное мытьё головы',
    'мытье головы',
    'мытьё головы',
    'уход за гол',
    'уход за кожей головы',
    'кожей головы',
)

BRANCH_SETTING_FIELDS = (
    'wax_pct',
    'head_care_pct',
    'face_care_pct',
    'camouflage_pct',
    'cosmo_pct',
    'opz_pct',
    'cosmo_price',
)
PERCENT_SETTING_FIELDS = (
    'wax_pct',
    'head_care_pct',
    'face_care_pct',
    'camouflage_pct',
    'cosmo_pct',
    'opz_pct',
)
STAFF_INPUT_FIELDS = (
    'clients',
    'avg_check_total',
    'reviews_qty',
    'cosmo_qty',
)


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous_period(self) -> DateRange:
        span = self.days
        prev_end = self.start - timedelta(days=1)
        prev_start = prev_end - timedelta(days=span - 1)
        return DateRange(start=prev_start, end=prev_end)


@dataclass(frozen=True)
class AdminAssignmentEvent:
    event_id: int
    event_date: date
    event_moment: Any
    created_user_id: Optional[int]


@dataclass(frozen=True)
class OpzEvent:
    event_id: int
    company_id: int
    client_id: int
    create_date: datetime
    appointment_date: date
    barber_staff_id: Optional[int]
    created_user_id: Optional[int]


def _pct_change(current: float, previous: float) -> Optional[float]:
    if previous == 0:
        return None if current == 0 else 100.0
    return round(100.0 * (current - previous) / previous, 2)


def _safe_div(numerator: float, denominator: float) -> float:
    return float(numerator or 0) / float(denominator or 0) if denominator else 0.0


def _appointment_shares(counts: dict[str, int]) -> dict[str, int]:
    total = counts['total']
    fields = ('cancelled', 'completed', 'incomplete')
    if total == 0:
        return {field: 0 for field in fields}

    exact = {field: 100.0 * counts[field] / total for field in fields}
    shares = {field: math.floor(exact[field]) for field in fields}
    remainder = 100 - sum(shares.values())
    ranked = sorted(
        fields,
        key=lambda field: (-(exact[field] - shares[field]), fields.index(field)),
    )
    for field in ranked[:remainder]:
        shares[field] += 1
    return shares


def _unavailable_appointments_breakdown() -> dict[str, Any]:
    return {
        'source_status': 'unavailable',
        'total': None,
        'cancelled': None,
        'completed': None,
        'incomplete': None,
        'total_share_pct': None,
        'cancelled_share_pct': None,
        'completed_share_pct': None,
        'incomplete_share_pct': None,
        'shares_total_pct': None,
        'attended': None,
        'pending': None,
    }


def _ready_appointments_breakdown(counts: dict[str, int], source_status: str = 'ready') -> dict[str, Any]:
    if any(counts[field] < 0 for field in ('total', 'cancelled', 'completed', 'incomplete')):
        return _unavailable_appointments_breakdown()
    if counts['cancelled'] + counts['completed'] + counts['incomplete'] != counts['total']:
        return _unavailable_appointments_breakdown()

    shares = _appointment_shares(counts)
    shares_total = sum(shares.values()) if counts['total'] else 0
    return {
        'source_status': source_status,
        **counts,
        'total_share_pct': 100 if counts['total'] else 0,
        'cancelled_share_pct': shares['cancelled'],
        'completed_share_pct': shares['completed'],
        'incomplete_share_pct': shares['incomplete'],
        'shares_total_pct': shares_total,
        'attended': counts['completed'],
        'pending': counts['incomplete'],
    }


async def _local_appointments_breakdown(
    db: AsyncSession,
    company_ids: list[int],
    start: date,
    end: date,
    staff_id: Optional[int] = None,
) -> dict[str, Any]:
    if not company_ids:
        return _ready_appointments_breakdown(
            {'total': 0, 'cancelled': 0, 'completed': 0, 'incomplete': 0},
            source_status='local',
        )
    filters = [
        Appointment.company_id.in_(company_ids),
        Appointment.date >= start,
        Appointment.date <= end,
    ]
    if staff_id is not None:
        filters.append(Appointment.staff_id == staff_id)
    row = (
        await db.execute(
            select(
                func.count(Appointment.id).label('total'),
                func.coalesce(
                    func.sum(case((Appointment.attendance == -1, 1), else_=0)),
                    0,
                ).label('cancelled'),
                func.coalesce(
                    func.sum(case((Appointment.attendance == COMPLETED_ATTENDANCE, 1), else_=0)),
                    0,
                ).label('completed'),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    Appointment.attendance != COMPLETED_ATTENDANCE,
                                    Appointment.attendance != -1,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label('incomplete'),
            ).where(*filters)
        )
    ).one()
    return _ready_appointments_breakdown(
        {
            'total': int(row.total or 0),
            'cancelled': int(row.cancelled or 0),
            'completed': int(row.completed or 0),
            'incomplete': int(row.incomplete or 0),
        },
        source_status='local',
    )


async def _appointment_company_ids(
    db: AsyncSession,
    company_id: Optional[int],
    staff_id: Optional[int],
    allowed_company_ids: Optional[list[int]] = None,
) -> list[int]:
    allowed_set = set(allowed_company_ids or []) if allowed_company_ids is not None else None
    if staff_id is not None:
        staff_company_id = await db.scalar(
            select(Staff.company_id).where(Staff.id == staff_id).limit(1)
        )
        if allowed_set is not None and staff_company_id not in allowed_set:
            return []
        return [int(staff_company_id)] if staff_company_id is not None else []
    if company_id is not None:
        if allowed_set is not None and company_id not in allowed_set:
            return []
        return [int(company_id)]

    if allowed_company_ids is not None:
        return [int(item) for item in allowed_company_ids]

    allowed = await branch_company_ids(db)
    if allowed is not None:
        return [int(item) for item in allowed]
    rows = (await db.execute(select(Company.id).order_by(Company.id.asc()))).scalars().all()
    return [int(item) for item in rows]


async def _fetch_appointments_breakdown(
    db_or_company_ids: AsyncSession | list[int],
    company_ids_or_start: list[int] | date,
    start_or_end: date,
    end_or_staff_id: date | Optional[int],
    staff_id: Optional[int] = None,
) -> dict[str, Any]:
    if isinstance(db_or_company_ids, list):
        db = None
        company_ids = db_or_company_ids
        start = company_ids_or_start
        end = start_or_end
        staff_id = end_or_staff_id
    else:
        db = db_or_company_ids
        company_ids = company_ids_or_start
        start = start_or_end
        end = end_or_staff_id

    try:
        if db is None:
            counts = await yclients_analytics.fetch_record_stats(company_ids, start, end, staff_id)
        else:
            try:
                counts = await yclients_analytics.fetch_record_stats(company_ids, start, end, staff_id, db=db)
            except TypeError:
                counts = await yclients_analytics.fetch_record_stats(company_ids, start, end, staff_id)
    except yclients_analytics.YClientsAnalyticsError:
        if db is not None:
            return await _local_appointments_breakdown(db, company_ids, start, end, staff_id)
        return _unavailable_appointments_breakdown()
    return _ready_appointments_breakdown(counts)


async def fetch_appointments_breakdown(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    company_ids = await _appointment_company_ids(db, company_id, staff_id, allowed_company_ids)
    return await _fetch_appointments_breakdown(db, company_ids, start, end, staff_id)


def _is_waitlist_staff_name(value: Any) -> bool:
    return str(value or '').strip().casefold() == WAITLIST_STAFF_NAME


def _is_admin_placeholder_staff_name(value: Any) -> bool:
    return str(value or '').strip().casefold().startswith(ADMIN_PLACEHOLDER_STAFF_PREFIX)


def _business_staff_id_condition(staff_id_column):
    staff_alias = aliased(Staff)
    staff_name_raw = func.coalesce(staff_alias.name, '')
    staff_name = func.lower(staff_name_raw)
    non_business_staff = exists(
        select(1).where(
            staff_alias.id == staff_id_column,
            or_(
                staff_name == WAITLIST_STAFF_NAME,
                staff_name_raw == 'Лист ожидания',
                staff_name.like(f'{ADMIN_PLACEHOLDER_STAFF_PREFIX}%'),
                staff_name_raw.like('Администратор%'),
            ),
        )
    )
    return or_(staff_id_column.is_(None), ~non_business_staff)


def business_appointment_condition():
    return _business_staff_id_condition(Appointment.staff_id)


def _business_financial_master_condition():
    linked_business_appointment = exists(
        select(1).where(
            Appointment.id == FinancialTransaction.record_id,
            business_appointment_condition(),
        )
    )
    return or_(
        and_(
            FinancialTransaction.master_id.is_not(None),
            _business_staff_id_condition(FinancialTransaction.master_id),
        ),
        and_(
            FinancialTransaction.master_id.is_(None),
            or_(FinancialTransaction.record_id.is_(None), linked_business_appointment),
        ),
    )


def _coerce_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _company_scope_clause(column, company_id: Optional[int], allowed_company_ids: Optional[list[int]]):
    if company_id is not None:
        return column == company_id
    if allowed_company_ids is not None:
        return column.in_(allowed_company_ids) if allowed_company_ids else column.in_([])
    return None


def _coerce_time(value: Any) -> time | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.time()
    if isinstance(value, time):
        return value
    text = str(value)
    if 'T' in text:
        text = text.split('T', 1)[1]
    elif ' ' in text:
        text = text.split(' ', 1)[1]
    return time.fromisoformat(text[:8])


def _schedule_slot_covers_datetime(slot_from: Any, slot_to: Any, value: Any) -> bool:
    appt_time = _coerce_time(value)
    start_time = _coerce_time(slot_from)
    end_time = _coerce_time(slot_to)
    if appt_time is None or start_time is None or end_time is None:
        return True
    if start_time <= end_time:
        return start_time <= appt_time < end_time
    return appt_time >= start_time or appt_time < end_time


def _appt_revenue_filters(
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    created_user_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
):
    parts = [
        Appointment.attendance == COMPLETED_ATTENDANCE,
        Appointment.date >= start,
        Appointment.date <= end,
        business_appointment_condition(),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        parts.append(scope)
    if created_user_id is not None:
        parts.append(Appointment.created_user_id == created_user_id)
    elif staff_id is not None:
        parts.append(Appointment.staff_id == staff_id)
    return and_(*parts)


def _appt_all_filters(
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
):
    parts = [
        Appointment.date >= start,
        Appointment.date <= end,
        business_appointment_condition(),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        parts.append(scope)
    if staff_id is not None:
        parts.append(Appointment.staff_id == staff_id)
    return and_(*parts)


def _goods_revenue_filters(
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
):
    parts = [
        GoodTransaction.type_id == GOODS_SALE_TYPE_ID,
        func.date(GoodTransaction.date) >= start,
        func.date(GoodTransaction.date) <= end,
        _business_staff_id_condition(GoodTransaction.master_id),
    ]
    scope = _company_scope_clause(GoodTransaction.company_id, company_id, allowed_company_ids)
    if scope is not None:
        parts.append(scope)
    if staff_id is not None:
        parts.append(GoodTransaction.master_id == staff_id)
    return and_(*parts)


def _service_paid_filters(
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    created_user_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
):
    parts = [
        FinancialTransaction.sold_item_type == SERVICE_SOLD_ITEM_TYPE,
        Appointment.attendance == COMPLETED_ATTENDANCE,
        FinancialTransaction.amount > 0,
        func.date(FinancialTransaction.date) >= start,
        func.date(FinancialTransaction.date) <= end,
        business_appointment_condition(),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        parts.append(scope)
    if created_user_id is not None:
        parts.append(Appointment.created_user_id == created_user_id)
    elif staff_id is not None:
        parts.append(Appointment.staff_id == staff_id)
    return and_(*parts)


def _goods_paid_filters(
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
):
    parts = [
        FinancialTransaction.sold_item_type == GOODS_SOLD_ITEM_TYPE,
        FinancialTransaction.amount > 0,
        func.date(FinancialTransaction.date) >= start,
        func.date(FinancialTransaction.date) <= end,
        _business_financial_master_condition(),
    ]
    scope = _company_scope_clause(FinancialTransaction.company_id, company_id, allowed_company_ids)
    if scope is not None:
        parts.append(scope)
    if staff_id is not None:
        parts.append(_financial_staff_attribution_condition(staff_id))
    return and_(*parts)


def _financial_staff_attribution_condition(staff_id: int):
    """Prefer the payment master, falling back to the linked visit master."""
    return or_(
        FinancialTransaction.master_id == staff_id,
        and_(
            FinancialTransaction.master_id.is_(None),
            exists(
                select(1).where(
                    Appointment.id == FinancialTransaction.record_id,
                    Appointment.staff_id == staff_id,
                )
            ),
        ),
    )


async def _goods_paid_revenue_total(
    db: AsyncSession,
    dr: DateRange,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> float:
    stmt = (
        select(func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'))
        .where(_goods_paid_filters(dr.start, dr.end, company_id, staff_id, allowed_company_ids))
    )
    row = (await db.execute(stmt)).one()
    return float(row.revenue or 0)


async def _goods_sold_count(
    db: AsyncSession,
    dr: DateRange,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> float:
    sold_qty = func.coalesce(
        func.sum(func.abs(func.coalesce(GoodTransaction.amount, 0.0))),
        0.0,
    )
    stmt = (
        select(sold_qty.label('qty'))
        .where(_goods_revenue_filters(dr.start, dr.end, company_id, staff_id, allowed_company_ids))
    )
    row = (await db.execute(stmt)).one()
    return float(row.qty or 0)


def _physical_account_condition():
    title = func.coalesce(AccountCatalog.title, '')
    excluded = [
        or_(
            title.like(f'%{marker}%'),
            title.like(f'%{marker.capitalize()}%'),
        )
        for marker in NON_CASH_ACCOUNT_MARKERS
    ]
    return and_(*[~condition for condition in excluded])


def _personal_account_condition():
    expense_title = func.coalesce(FinancialTransaction.expense_title, '')
    return or_(
        func.coalesce(FinancialTransaction.sold_item_type, '').in_(PERSONAL_ACCOUNT_TYPES),
        *[
            or_(
                expense_title.like(f'%{marker}%'),
                expense_title.like(f'%{marker.capitalize()}%'),
            )
            for marker in PERSONAL_ACCOUNT_EXPENSE_MARKERS
        ],
    )


async def _source_coverage_status(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int],
    company_ids_override: Optional[list[int]] = None,
) -> tuple[str, list[str]]:
    company_ids = (
        company_ids_override
        if company_ids_override is not None
        else await _appointment_company_ids(db, company_id, staff_id)
    )
    if not company_ids:
        return 'partial', ['personal_account_topups']
    covered = await db.scalar(
        select(func.count())
        .select_from(SyncSourceState)
        .where(
            SyncSourceState.company_id.in_(company_ids),
            SyncSourceState.source == PERSONAL_ACCOUNT_SOURCE,
            SyncSourceState.period_start <= start,
            SyncSourceState.period_end >= end,
        )
    )
    if int(covered or 0) == len(company_ids):
        return 'ready', []
    return 'partial', ['personal_account_topups']


async def _average_check_block(
    db: AsyncSession,
    dr: DateRange,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    created_user_id: Optional[int] = None,
    company_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    visit_filters = [
        Appointment.attendance == COMPLETED_ATTENDANCE,
        Appointment.date >= dr.start,
        Appointment.date <= dr.end,
        business_appointment_condition(),
    ]
    if company_id is not None:
        visit_filters.append(Appointment.company_id == company_id)
    elif company_ids is not None:
        visit_filters.append(Appointment.company_id.in_(company_ids))
    if created_user_id is not None:
        visit_filters.append(Appointment.created_user_id == created_user_id)
    elif staff_id is not None:
        visit_filters.append(Appointment.staff_id == staff_id)

    visit_row = (
        await db.execute(
            select(
                func.count(func.distinct(Appointment.id)).label('completed_appointments'),
                func.count(func.distinct(Appointment.client_id)).label('unique_clients'),
                func.coalesce(
                    func.sum(case((Appointment.client_id.is_(None), 1), else_=0)),
                    0,
                ).label('appointments_without_client'),
            ).where(*visit_filters)
        )
    ).one()

    goods_filters = [
        GoodTransaction.type_id == GOODS_SALE_TYPE_ID,
        GoodTransaction.document_id.is_not(None),
        func.date(GoodTransaction.date) >= dr.start,
        func.date(GoodTransaction.date) <= dr.end,
        _business_staff_id_condition(GoodTransaction.master_id),
    ]
    if company_id is not None:
        goods_filters.append(GoodTransaction.company_id == company_id)
    elif company_ids is not None:
        goods_filters.append(GoodTransaction.company_id.in_(company_ids))
    if staff_id is not None and created_user_id is None:
        goods_filters.append(GoodTransaction.master_id == staff_id)
    goods_checks = int(
        await db.scalar(
            select(func.count(func.distinct(GoodTransaction.document_id))).where(*goods_filters)
        )
        or 0
    )

    base_payment_filters = [
        FinancialTransaction.amount > 0,
        func.date(FinancialTransaction.date) >= dr.start,
        func.date(FinancialTransaction.date) <= dr.end,
        _physical_account_condition(),
    ]

    service_filters = [
        *base_payment_filters,
        FinancialTransaction.sold_item_type == SERVICE_SOLD_ITEM_TYPE,
        Appointment.attendance == COMPLETED_ATTENDANCE,
        business_appointment_condition(),
    ]
    if company_id is not None:
        service_filters.append(Appointment.company_id == company_id)
    elif company_ids is not None:
        service_filters.append(Appointment.company_id.in_(company_ids))
    if created_user_id is not None:
        service_filters.append(Appointment.created_user_id == created_user_id)
    elif staff_id is not None:
        service_filters.append(Appointment.staff_id == staff_id)
    service_revenue = float(
        await db.scalar(
            select(func.coalesce(func.sum(FinancialTransaction.amount), 0.0))
            .select_from(FinancialTransaction)
            .join(Appointment, Appointment.id == FinancialTransaction.record_id)
            .outerjoin(
                AccountCatalog,
                and_(
                    AccountCatalog.company_id == FinancialTransaction.company_id,
                    AccountCatalog.account_id == FinancialTransaction.account_id,
                ),
            )
            .where(*service_filters)
        )
        or 0
    )

    classified_revenue = {}
    direct_payment_filters = list(base_payment_filters)
    direct_payment_filters.append(_business_financial_master_condition())
    if company_id is not None:
        direct_payment_filters.append(FinancialTransaction.company_id == company_id)
    elif company_ids is not None:
        direct_payment_filters.append(FinancialTransaction.company_id.in_(company_ids))
    for name, condition, staff_condition in (
        (
            'goods_revenue',
            FinancialTransaction.sold_item_type == GOODS_SOLD_ITEM_TYPE,
            _financial_staff_attribution_condition(staff_id) if staff_id is not None else None,
        ),
        (
            'topup_revenue',
            _personal_account_condition(),
            FinancialTransaction.master_id == staff_id if staff_id is not None else None,
        ),
    ):
        metric_filters = [*direct_payment_filters, condition]
        if staff_condition is not None and created_user_id is None:
            metric_filters.append(staff_condition)
        classified_revenue[name] = float(
            await db.scalar(
                select(func.coalesce(func.sum(FinancialTransaction.amount), 0.0))
                .select_from(FinancialTransaction)
                .outerjoin(
                    AccountCatalog,
                    and_(
                        AccountCatalog.company_id == FinancialTransaction.company_id,
                        AccountCatalog.account_id == FinancialTransaction.account_id,
                    ),
                )
                .where(*metric_filters)
            )
            or 0
        )

    unclassified_filters = list(direct_payment_filters)
    if staff_id is not None and created_user_id is None:
        unclassified_filters.append(FinancialTransaction.master_id == staff_id)
    known_condition = or_(
        func.coalesce(FinancialTransaction.sold_item_type, '') == SERVICE_SOLD_ITEM_TYPE,
        func.coalesce(FinancialTransaction.sold_item_type, '') == GOODS_SOLD_ITEM_TYPE,
        _personal_account_condition(),
    )
    unclassified_operations = int(
        await db.scalar(
            select(func.count(FinancialTransaction.id))
            .select_from(FinancialTransaction)
            .outerjoin(
                AccountCatalog,
                and_(
                    AccountCatalog.company_id == FinancialTransaction.company_id,
                    AccountCatalog.account_id == FinancialTransaction.account_id,
                ),
            )
            .where(*unclassified_filters, ~known_condition)
        )
        or 0
    )

    unique_clients = int(visit_row.unique_clients or 0)
    completed_appointments = int(visit_row.completed_appointments or 0)
    appointments_without_client = int(visit_row.appointments_without_client or 0)
    numerator = (
        service_revenue
        + classified_revenue['goods_revenue']
        + classified_revenue['topup_revenue']
    )
    denominator = completed_appointments
    source_status, missing_components = await _source_coverage_status(
        db,
        dr.start,
        dr.end,
        company_id,
        staff_id,
        company_ids_override=company_ids,
    )
    return {
        'source_status': source_status,
        'missing_components': missing_components,
        'service_revenue': service_revenue,
        'goods_revenue': classified_revenue['goods_revenue'],
        'topup_revenue': classified_revenue['topup_revenue'],
        'completed_appointments': completed_appointments,
        'unique_clients': unique_clients,
        'appointments_without_client': appointments_without_client,
        'goods_checks': goods_checks,
        'numerator': numerator,
        'denominator': denominator,
        'formula': (
            'income / completed_appointments'
        ),
        'unclassified_operations': unclassified_operations,
        'total': _safe_div(numerator, denominator),
    }


def _returning_bucket(count: int, total: int) -> dict[str, Any]:
    return {
        'count': count,
        'pct': 100.0 * _safe_div(float(count), float(total)),
    }


async def _client_visit_frequency_block(
    db: AsyncSession,
    dr: DateRange,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    filters = [
        Appointment.attendance == COMPLETED_ATTENDANCE,
        Appointment.client_id.is_not(None),
        Appointment.date >= dr.start,
        Appointment.date <= dr.end,
        business_appointment_condition(),
    ]
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    if scope is not None:
        filters.append(scope)
    if staff_id is not None:
        filters.append(Appointment.staff_id == staff_id)

    visits_by_client = (
        select(
            Appointment.client_id.label('client_id'),
            func.count(Appointment.id).label('visit_count'),
        )
        .where(*filters)
        .group_by(Appointment.client_id)
        .subquery()
    )
    row = (
        await db.execute(
            select(
                func.count(visits_by_client.c.client_id).label('total_clients'),
                func.coalesce(
                    func.sum(case((visits_by_client.c.visit_count == 1, 1), else_=0)),
                    0,
                ).label('one_visit'),
                func.coalesce(
                    func.sum(
                        case(
                            (
                                and_(
                                    visits_by_client.c.visit_count >= 2,
                                    visits_by_client.c.visit_count <= 3,
                                ),
                                1,
                            ),
                            else_=0,
                        )
                    ),
                    0,
                ).label('two_to_three_visits'),
                func.coalesce(
                    func.sum(case((visits_by_client.c.visit_count >= 4, 1), else_=0)),
                    0,
                ).label('four_plus_visits'),
            ).select_from(visits_by_client)
        )
    ).one()

    total_clients = int(row.total_clients or 0)
    one_visit = int(row.one_visit or 0)
    two_to_three_visits = int(row.two_to_three_visits or 0)
    four_plus_visits = int(row.four_plus_visits or 0)
    return {
        'total_clients': total_clients,
        'one_visit': _returning_bucket(one_visit, total_clients),
        'two_to_three_visits': _returning_bucket(two_to_three_visits, total_clients),
        'four_plus_visits': _returning_bucket(four_plus_visits, total_clients),
    }


async def _client_recency_block(
    db: AsyncSession,
    dr: DateRange,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    scope = _company_scope_clause(Appointment.company_id, company_id, allowed_company_ids)
    scope_filters = [
        Appointment.attendance == COMPLETED_ATTENDANCE,
        Appointment.client_id.is_not(None),
        business_appointment_condition(),
    ]
    if scope is not None:
        scope_filters.append(scope)
    current_filters = list(scope_filters)
    if staff_id is not None:
        current_filters.append(Appointment.staff_id == staff_id)

    current_clients = (
        select(Appointment.client_id.label('client_id'))
        .where(
            *current_filters,
            Appointment.date >= dr.start,
            Appointment.date <= dr.end,
        )
        .group_by(Appointment.client_id)
        .subquery()
    )
    first_visits = (
        select(
            Appointment.client_id.label('client_id'),
            func.min(Appointment.date).label('first_visit_date'),
        )
        .where(*scope_filters)
        .group_by(Appointment.client_id)
        .subquery()
    )
    row = (
        await db.execute(
            select(
                func.count(current_clients.c.client_id).label('total_clients'),
                func.coalesce(
                    func.sum(
                        case((first_visits.c.first_visit_date >= dr.start, 1), else_=0)
                    ),
                    0,
                ).label('new_clients'),
                func.coalesce(
                    func.sum(
                        case((first_visits.c.first_visit_date < dr.start, 1), else_=0)
                    ),
                    0,
                ).label('repeat_clients'),
            )
            .select_from(current_clients)
            .join(first_visits, first_visits.c.client_id == current_clients.c.client_id)
        )
    ).one()

    total_clients = int(row.total_clients or 0)
    new_clients = int(row.new_clients or 0)
    repeat_clients = int(row.repeat_clients or 0)
    return {
        'total_clients': total_clients,
        'new_clients': new_clients,
        'new_clients_pct': 100.0 * _safe_div(float(new_clients), float(total_clients)),
        'repeat_clients': repeat_clients,
        'repeat_clients_pct': 100.0 * _safe_div(float(repeat_clients), float(total_clients)),
    }


def _title_matches(title_expr, parts: tuple[str, ...]):
    conditions = []
    for part in parts:
        conditions.append(title_expr.like(f'%{part.lower()}%'))
        conditions.append(title_expr.like(f'%{part}%'))
    return or_(*conditions)


def _service_qty_sum(title_expr, parts: tuple[str, ...]):
    return func.coalesce(
        func.sum(
            case(
                (_title_matches(title_expr, parts), func.coalesce(Transaction.amount, 0)),
                else_=0,
            )
        ),
        0,
    )


def _service_group_key(title_expr, service_id_expr):
    normalized_title = func.lower(func.replace(title_expr, 'ё', 'е'))
    return func.coalesce(func.nullif(normalized_title, ''), cast(service_id_expr, String))


def _transaction_service_label_join():
    return and_(
        ServiceLabel.service_id == Transaction.service_id,
        ServiceLabel.company_id == Appointment.company_id,
    )


def _financial_service_label_join():
    return and_(
        ServiceLabel.service_id == FinancialTransaction.sold_item_id,
        ServiceLabel.company_id == Appointment.company_id,
    )


def _transaction_service_catalog_join():
    return and_(
        ServiceCatalog.service_id == Transaction.service_id,
        ServiceCatalog.company_id == Appointment.company_id,
    )


def _financial_service_catalog_join():
    return and_(
        ServiceCatalog.service_id == FinancialTransaction.sold_item_id,
        ServiceCatalog.company_id == Appointment.company_id,
    )


def _derive_metric_values(
    values: dict[str, float],
    *,
    include_zero_derived: bool,
    prefer_explicit: bool = True,
) -> dict[str, float]:
    out = {code: float(value) for code, value in values.items() if value is not None}

    clients = out.get('clients', 0.0)
    avg_check_denominator = out.get('avg_check_denominator', clients)
    if (not prefer_explicit or 'avg_check_total' not in out) and (
        include_zero_derived or {'revenue', 'clients'} <= out.keys()
    ):
        out['avg_check_total'] = (
            out.get('revenue', 0.0) / avg_check_denominator
            if avg_check_denominator
            else 0.0
        )

    if (not prefer_explicit or 'opz_pct' not in out) and (
        include_zero_derived or {'opz_qty', 'clients'} <= out.keys()
    ):
        out['opz_pct'] = 100.0 * out.get('opz_qty', 0.0) / clients if clients else 0.0

    if (not prefer_explicit or 'extra_services_pct' not in out) and (
        include_zero_derived
        or (
            'clients' in out
            and any(
                code in out
                for code in ('wax_qty', 'camouflage_qty', 'face_care_qty', 'head_care_qty')
            )
        )
    ):
        extra_qty = (
            out.get('wax_qty', 0.0)
            + out.get('camouflage_qty', 0.0)
            + out.get('face_care_qty', 0.0)
            + out.get('head_care_qty', 0.0)
        )
        out['extra_services_pct'] = 100.0 * extra_qty / clients if clients else 0.0

    return out


def _sum_metric_components(component_rows: list[dict[str, float]]) -> dict[str, float]:
    summed: dict[str, float] = {}
    for values in component_rows:
        for code in RAW_PLAN_FACT_CODES:
            if code in values:
                summed[code] = summed.get(code, 0.0) + float(values[code] or 0.0)
    return _derive_metric_values(summed, include_zero_derived=False, prefer_explicit=False)


def _round_optional(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(float(value), 2)


def _round_half_up_int(value: float) -> float:
    """School-style rounding: 16.5 → 17, unlike Python's banker's round()."""
    if value >= 0:
        return float(math.floor(value + 0.5))
    return float(-math.floor(-value + 0.5))


def _round_metric_value(value: Optional[float], fmt: str) -> Optional[float]:
    """Round a plan/fact metric value to the precision its display format expects."""
    if value is None:
        return None
    v = float(value)
    if fmt == 'number':
        return _round_half_up_int(v)
    return round(v, 2)


def _iso_datetime(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _service_kpi_group_payload(group: ServiceKpiGroup) -> dict[str, Any]:
    return {
        'id': group.id,
        'portal_account_id': group.portal_account_id,
        'code': group.code,
        'title': group.title,
        'description': group.description or '',
        'is_active': bool(group.is_active),
        'sort_order': int(group.sort_order or 0),
        'created_at': _iso_datetime(group.created_at),
        'updated_at': _iso_datetime(group.updated_at),
    }


def _service_display_category(category_title: Any, service_title: Any) -> str:
    existing = str(category_title or '').strip()
    if existing:
        return existing
    title = str(service_title or '').strip().lower().replace('ё', 'е')
    if not title:
        return ''
    if any(part in title for part in ('лиц', 'mask', 'volcano', 'mr. q', 'волкано')):
        return 'УХОД ЗА ЛИЦОМ'
    if any(part in title for part in ('голов', 'волос', 'пилинг', 'мыть', 'мойка')):
        return 'УХОД ЗА ГОЛОВОЙ'
    if any(part in title for part in ('бород', 'усы', 'усов')):
        return 'БОРОДА'
    if any(part in title for part in ('брить', 'брит', 'шейвер', 'shaver')):
        return 'БРИТЬЕ'
    if 'стриж' in title or 'окантов' in title:
        return 'СТРИЖКА'
    if any(part in title for part in ('воск', 'камуфляж', 'spa', 'спа')):
        return 'Дополнительные услуги'
    return ''


def _completion_status(completion_pct: Optional[float]) -> str:
    if completion_pct is None:
        return 'no-plan'
    if completion_pct >= 100:
        return 'ok'
    if completion_pct >= 80:
        return 'warn'
    return 'bad'


async def _revenue_block(
    db: AsyncSession,
    dr: DateRange,
    company_id: Optional[int],
    staff_id: Optional[int] = None,
    created_user_id: Optional[int] = None,
    include_goods: bool = True,
    allowed_company_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    cond = _appt_revenue_filters(
        dr.start,
        dr.end,
        company_id,
        staff_id,
        created_user_id=created_user_id,
        allowed_company_ids=allowed_company_ids,
    )
    extra_appt = case(
        (ServiceLabel.is_extra.is_(True), Appointment.id),
        else_=None,
    )
    extra_client = case(
        (ServiceLabel.is_extra.is_(True), Appointment.client_id),
        else_=None,
    )
    service_count = func.coalesce(func.sum(func.coalesce(Transaction.amount, 0)), 0)
    extra_service_count = func.coalesce(
        func.sum(
            case(
                (ServiceLabel.is_extra.is_(True), func.coalesce(Transaction.amount, 0)),
                else_=0,
            )
        ),
        0,
    )
    counts_stmt = (
        select(
            service_count.label('service_count'),
            extra_service_count.label('extra_service_count'),
            func.count(func.distinct(Appointment.id)).label('appointments'),
            func.count(func.distinct(extra_appt)).label('extra_service_appointments'),
            func.count(func.distinct(Appointment.client_id)).label('unique_clients'),
            func.count(func.distinct(extra_client)).label('extra_service_clients'),
        )
        .select_from(Appointment)
        .outerjoin(Transaction, Transaction.appointment_id == Appointment.id)
        .outerjoin(ServiceLabel, _transaction_service_label_join())
        .where(cond)
    )
    paid_stmt = (
        select(
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
            func.coalesce(
                func.sum(
                    case(
                        (ServiceLabel.is_extra.is_(True), FinancialTransaction.amount),
                        else_=0.0,
                    )
                ),
                0.0,
            ).label('extra_service_revenue'),
        )
        .select_from(FinancialTransaction)
        .join(Appointment, Appointment.id == FinancialTransaction.record_id)
        .outerjoin(ServiceLabel, _financial_service_label_join())
        .where(
            _service_paid_filters(
                dr.start,
                dr.end,
                company_id,
                staff_id,
                created_user_id=created_user_id,
                allowed_company_ids=allowed_company_ids,
            )
        )
    )
    row = (await db.execute(counts_stmt)).one()
    paid_row = (await db.execute(paid_stmt)).one()
    service_revenue = float(paid_row.revenue or 0)
    extra_service_revenue = float(paid_row.extra_service_revenue or 0)
    goods_revenue = (
        await _goods_paid_revenue_total(db, dr, company_id, staff_id, allowed_company_ids)
        if include_goods
        else 0.0
    )
    goods_count = (
        await _goods_sold_count(db, dr, company_id, staff_id, allowed_company_ids)
        if include_goods
        else 0.0
    )
    return {
        'revenue': service_revenue + goods_revenue,
        'service_revenue': service_revenue,
        'goods_revenue': goods_revenue,
        'extra_service_revenue': extra_service_revenue,
        'service_count': float(row.service_count or 0),
        'goods_count': goods_count,
        'extra_service_count': float(row.extra_service_count or 0),
        'appointments': int(row.appointments or 0),
        'extra_service_appointments': int(row.extra_service_appointments or 0),
        'unique_clients': int(row.unique_clients or 0),
        'extra_service_clients': int(row.extra_service_clients or 0),
    }


async def fetch_summary(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> dict[str, Any]:
    current_dr = DateRange(start=start, end=end)
    prev_dr = current_dr.previous_period()
    appointment_company_ids = await _appointment_company_ids(db, company_id, staff_id)
    if allowed_company_ids is not None:
        allowed_set = {int(item) for item in allowed_company_ids}
        appointment_company_ids = [item for item in appointment_company_ids if item in allowed_set]
    appointments_task = asyncio.create_task(
        _fetch_appointments_breakdown(db, appointment_company_ids, start, end, staff_id)
    )
    previous_appointments_task = asyncio.create_task(
        _fetch_appointments_breakdown(
            db,
            appointment_company_ids,
            prev_dr.start,
            prev_dr.end,
            staff_id,
        )
    )

    cur = await _revenue_block(db, current_dr, company_id, staff_id, allowed_company_ids=allowed_company_ids)
    prev = await _revenue_block(db, prev_dr, company_id, staff_id, allowed_company_ids=allowed_company_ids)
    cur_opz_qty = await _opz_count_scope(
        db, start, end, company_id, staff_id, company_ids=appointment_company_ids
    )
    prev_opz_qty = await _opz_count_scope(
        db,
        prev_dr.start,
        prev_dr.end,
        company_id,
        staff_id,
        company_ids=appointment_company_ids,
    )
    avg_company_ids = appointment_company_ids if company_id is None else None
    cur_average_check = await _average_check_block(
        db, current_dr, company_id, staff_id, company_ids=avg_company_ids
    )
    prev_average_check = await _average_check_block(
        db, prev_dr, company_id, staff_id, company_ids=avg_company_ids
    )
    client_frequency = await _client_visit_frequency_block(
        db,
        current_dr,
        company_id,
        staff_id,
        allowed_company_ids=allowed_company_ids,
    )
    cur_client_recency = await _client_recency_block(
        db,
        current_dr,
        company_id,
        staff_id,
        allowed_company_ids=allowed_company_ids,
    )
    prev_client_recency = await _client_recency_block(
        db,
        prev_dr,
        company_id,
        staff_id,
        allowed_company_ids=allowed_company_ids,
    )
    for block, average_check in ((cur, cur_average_check), (prev, prev_average_check)):
        block['service_revenue'] = average_check['service_revenue']
        block['goods_revenue'] = average_check['goods_revenue']
        block['topup_revenue'] = average_check['topup_revenue']
        block['revenue'] = average_check['numerator']
    appointments_breakdown = await appointments_task
    previous_appointments_breakdown = await previous_appointments_task
    local_completed = int(cur['appointments'] or 0)
    exact_completed = appointments_breakdown.get('completed')
    appointments_breakdown = {
        **appointments_breakdown,
        'local_completed': local_completed,
        'completed_difference': (
            int(exact_completed) - local_completed
            if appointments_breakdown['source_status'] == 'ready'
            else None
        ),
        'is_consistent': (
            int(exact_completed) == local_completed
            if appointments_breakdown['source_status'] == 'ready'
            else None
        ),
    }

    cur_rev = cur['revenue']
    prev_rev = prev['revenue']
    cur_appointments = float(
        appointments_breakdown['completed']
        if appointments_breakdown['source_status'] == 'ready'
        else local_completed
    )
    prev_appointments = float(
        previous_appointments_breakdown['completed']
        if previous_appointments_breakdown['source_status'] == 'ready'
        else prev['appointments']
    )
    cur_avg_total = float(cur_average_check['total'])
    prev_avg_total = float(prev_average_check['total'])
    cur_avg_services = _safe_div(cur['service_revenue'], cur_appointments)
    prev_avg_services = _safe_div(prev['service_revenue'], prev_appointments)
    cur_avg_goods = _safe_div(cur['goods_revenue'], float(cur['goods_count'] or 0))
    prev_avg_goods = _safe_div(prev['goods_revenue'], float(prev['goods_count'] or 0))
    cur_avg_extra_services = _safe_div(
        cur['extra_service_revenue'],
        float(cur['extra_service_count'] or 0),
    )
    prev_avg_extra_services = _safe_div(
        prev['extra_service_revenue'],
        float(prev['extra_service_count'] or 0),
    )
    cur_extra_services_per_appointment_pct = 100.0 * _safe_div(
        float(cur['extra_service_count'] or 0),
        cur_appointments,
    )
    prev_extra_services_per_appointment_pct = 100.0 * _safe_div(
        float(prev['extra_service_count'] or 0),
        prev_appointments,
    )
    cur_unique_clients = float(cur['unique_clients'] or 0)
    prev_unique_clients = float(prev['unique_clients'] or 0)
    cur_visits_per_client = _safe_div(cur_appointments, cur_unique_clients)
    prev_visits_per_client = _safe_div(prev_appointments, prev_unique_clients)
    cur_extra_service_clients_pct = 100.0 * _safe_div(
        float(cur['extra_service_clients'] or 0),
        cur_unique_clients,
    )
    prev_extra_service_clients_pct = 100.0 * _safe_div(
        float(prev['extra_service_clients'] or 0),
        prev_unique_clients,
    )
    cur_opz_pct = 100.0 * _safe_div(cur_opz_qty, float(cur['appointments'] or 0))
    prev_opz_pct = 100.0 * _safe_div(prev_opz_qty, float(prev['appointments'] or 0))

    return {
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'previous_period': {'start': prev_dr.start.isoformat(), 'end': prev_dr.end.isoformat()},
        'revenue': {
            'total': cur_rev,
            'service_revenue': cur['service_revenue'],
            'goods_revenue': cur['goods_revenue'],
            'topup_revenue': cur['topup_revenue'],
            'extra_service_revenue': cur['extra_service_revenue'],
            'change_pct': _pct_change(cur_rev, prev_rev),
            'service_revenue_change_pct': _pct_change(
                float(cur['service_revenue']), float(prev['service_revenue'])
            ),
            'goods_revenue_change_pct': _pct_change(
                float(cur['goods_revenue']), float(prev['goods_revenue'])
            ),
            'topup_revenue_change_pct': _pct_change(
                float(cur['topup_revenue']), float(prev['topup_revenue'])
            ),
            'extra_service_revenue_change_pct': _pct_change(
                float(cur['extra_service_revenue']), float(prev['extra_service_revenue'])
            ),
            'service_count': cur['service_count'],
            'service_count_change_pct': _pct_change(
                float(cur['service_count']), float(prev['service_count'])
            ),
            'goods_count': cur['goods_count'],
            'goods_count_change_pct': _pct_change(
                float(cur['goods_count']), float(prev['goods_count'])
            ),
            'extra_service_count': cur['extra_service_count'],
            'extra_service_count_change_pct': _pct_change(
                float(cur['extra_service_count']), float(prev['extra_service_count'])
            ),
            'appointments': int(cur_appointments),
            'appointments_change_pct': _pct_change(
                cur_appointments, prev_appointments
            ),
            'extra_service_appointments': cur['extra_service_appointments'],
            'unique_clients': cur['unique_clients'],
            'unique_clients_change_pct': _pct_change(
                float(cur['unique_clients']), float(prev['unique_clients'])
            ),
            'extra_service_clients': cur['extra_service_clients'],
            'extra_service_clients_change_pct': _pct_change(
                float(cur['extra_service_clients']), float(prev['extra_service_clients'])
            ),
        },
        'visit_metrics': {
            'opz_qty': cur_opz_qty,
            'opz_qty_change_pct': _pct_change(cur_opz_qty, prev_opz_qty),
            'opz_pct': cur_opz_pct,
            'opz_pct_change_pct': _pct_change(cur_opz_pct, prev_opz_pct),
            'extra_services_per_appointment_pct': cur_extra_services_per_appointment_pct,
            'extra_services_per_appointment_pct_change_pct': _pct_change(
                cur_extra_services_per_appointment_pct,
                prev_extra_services_per_appointment_pct,
            ),
            'unique_clients': cur['unique_clients'],
            'unique_clients_change_pct': _pct_change(
                float(cur['unique_clients']),
                float(prev['unique_clients']),
            ),
            'visits_per_client': cur_visits_per_client,
            'visits_per_client_change_pct': _pct_change(
                cur_visits_per_client,
                prev_visits_per_client,
            ),
            'extra_service_clients': cur['extra_service_clients'],
            'extra_service_clients_pct': cur_extra_service_clients_pct,
            'extra_service_clients_pct_change_pct': _pct_change(
                cur_extra_service_clients_pct,
                prev_extra_service_clients_pct,
            ),
            'client_visit_frequency': client_frequency,
            'new_clients': cur_client_recency['new_clients'],
            'new_clients_change_pct': _pct_change(
                float(cur_client_recency['new_clients']),
                float(prev_client_recency['new_clients']),
            ),
            'new_clients_pct': cur_client_recency['new_clients_pct'],
            'new_clients_pct_change_pct': _pct_change(
                cur_client_recency['new_clients_pct'],
                prev_client_recency['new_clients_pct'],
            ),
            'repeat_clients': cur_client_recency['repeat_clients'],
            'repeat_clients_change_pct': _pct_change(
                float(cur_client_recency['repeat_clients']),
                float(prev_client_recency['repeat_clients']),
            ),
            'repeat_clients_pct': cur_client_recency['repeat_clients_pct'],
            'repeat_clients_pct_change_pct': _pct_change(
                cur_client_recency['repeat_clients_pct'],
                prev_client_recency['repeat_clients_pct'],
            ),
        },
        'average_check': {
            **cur_average_check,
            'total': cur_avg_total,
            'services': cur_avg_services,
            'goods': cur_avg_goods,
            'extra_services': cur_avg_extra_services,
            'total_change_pct': _pct_change(cur_avg_total, prev_avg_total),
            'services_change_pct': _pct_change(cur_avg_services, prev_avg_services),
            'goods_change_pct': _pct_change(cur_avg_goods, prev_avg_goods),
            'extra_services_change_pct': _pct_change(
                cur_avg_extra_services,
                prev_avg_extra_services,
            ),
            'appointments': int(cur_appointments),
            'extra_service_appointments': cur['extra_service_appointments'],
            'specialized_formulas': {
                'services': 'service_revenue / completed_appointments',
                'goods': 'goods_revenue / goods_units',
                'extra_services': 'extra_service_revenue / extra_service_units',
            },
        },
        'appointments_breakdown': appointments_breakdown,
    }


async def fetch_revenue_daily(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    svc_stmt = (
        select(
            Appointment.date.label('d'),
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
        )
        .select_from(Appointment)
        .join(FinancialTransaction, FinancialTransaction.record_id == Appointment.id)
        .where(
            _service_paid_filters(start, end, company_id, staff_id, allowed_company_ids=allowed_company_ids),
        )
        .group_by(Appointment.date)
    )
    appt_stmt = (
        select(
            Appointment.date.label('d'),
            func.count(func.distinct(Appointment.id)).label('appointments'),
        )
        .select_from(Appointment)
        .where(_appt_revenue_filters(start, end, company_id, staff_id, allowed_company_ids=allowed_company_ids))
        .group_by(Appointment.date)
    )

    goods_day = func.date(FinancialTransaction.date)
    goods_stmt = (
        select(
            goods_day.label('d'),
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
        )
        .where(_goods_paid_filters(start, end, company_id, staff_id, allowed_company_ids))
        .group_by(goods_day)
    )

    svc_rows = (await db.execute(svc_stmt)).all()
    appt_rows = (await db.execute(appt_stmt)).all()
    goods_rows = (await db.execute(goods_stmt)).all()
    company_ids = await _appointment_company_ids(db, company_id, staff_id)
    opz_by_date: dict[date, int] = {}
    for item_company_id in company_ids:
        events = await _opz_events(db, start, end, item_company_id)
        if staff_id is not None:
            events = [event for event in events if event.barber_staff_id == staff_id]
        for event in events:
            event_date = event.create_date.date()
            opz_by_date[event_date] = opz_by_date.get(event_date, 0) + 1

    by_date: dict[date, dict[str, float | int]] = {}
    for r in svc_rows:
        day = _coerce_date(r.d)
        by_date.setdefault(day, {'service_revenue': 0.0, 'goods_revenue': 0.0, 'appointments': 0, 'opz_qty': 0})
        by_date[day]['service_revenue'] = float(r.revenue or 0)
    for r in appt_rows:
        day = _coerce_date(r.d)
        by_date.setdefault(day, {'service_revenue': 0.0, 'goods_revenue': 0.0, 'appointments': 0, 'opz_qty': 0})
        by_date[day]['appointments'] = int(r.appointments or 0)
    for r in goods_rows:
        day = _coerce_date(r.d)
        by_date.setdefault(day, {'service_revenue': 0.0, 'goods_revenue': 0.0, 'appointments': 0, 'opz_qty': 0})
        by_date[day]['goods_revenue'] = float(r.revenue or 0)
    for day, opz_qty in opz_by_date.items():
        by_date.setdefault(day, {'service_revenue': 0.0, 'goods_revenue': 0.0, 'appointments': 0, 'opz_qty': 0})
        by_date[day]['opz_qty'] = opz_qty

    return [
        {
            'date': d.isoformat(),
            'revenue': float(v['service_revenue']) + float(v['goods_revenue']),
            'service_revenue': float(v['service_revenue']),
            'goods_revenue': float(v['goods_revenue']),
            'appointments': int(v['appointments']),
            'opz_qty': int(v['opz_qty']),
            'opz_pct': 100.0 * _safe_div(float(v['opz_qty']), float(v['appointments'])),
        }
        for d, v in sorted(by_date.items(), key=lambda kv: kv[0])
    ]


def _normalize_service_group_code(value: Any) -> str:
    text = str(value or '').strip().lower()
    text = re.sub(r'[^a-z0-9_]+', '_', text)
    text = re.sub(r'_+', '_', text).strip('_')
    return text[:80]


async def _unique_service_group_code(
    db: AsyncSession,
    raw_code: Any,
    *,
    portal_account_id: int,
    ignore_group_id: int | None = None,
) -> str:
    base = _normalize_service_group_code(raw_code) or 'kpi_group'
    candidate = base
    suffix = 2
    while True:
        stmt = select(ServiceKpiGroup.id).where(
            ServiceKpiGroup.portal_account_id == portal_account_id,
            ServiceKpiGroup.code == candidate,
        )
        if ignore_group_id is not None:
            stmt = stmt.where(ServiceKpiGroup.id != ignore_group_id)
        existing = await db.scalar(stmt.limit(1))
        if existing is None:
            return candidate
        candidate = f'{base}_{suffix}'
        suffix += 1


async def _service_catalog_row(
    db: AsyncSession,
    company_id: int,
    service_id: int,
) -> ServiceCatalog | None:
    return await db.get(ServiceCatalog, {'company_id': company_id, 'service_id': service_id})


async def fetch_service_kpi_groups(
    db: AsyncSession,
    *,
    portal_account_id: int | None = None,
    include_inactive: bool = True,
) -> list[dict[str, Any]]:
    stmt = select(ServiceKpiGroup)
    if portal_account_id is not None:
        stmt = stmt.where(ServiceKpiGroup.portal_account_id == portal_account_id)
    if not include_inactive:
        stmt = stmt.where(ServiceKpiGroup.is_active.is_(True))
    stmt = stmt.order_by(
        ServiceKpiGroup.is_active.desc(),
        ServiceKpiGroup.sort_order.asc(),
        ServiceKpiGroup.title.asc(),
        ServiceKpiGroup.id.asc(),
    )
    return [_service_kpi_group_payload(group) for group in (await db.execute(stmt)).scalars().all()]


async def fetch_dashboard_services(
    db: AsyncSession,
    *,
    company_id: int | None = None,
    q: str | None = None,
    category: str | None = None,
    is_extra: bool | None = None,
    kpi_group_id: int | None = None,
    allowed_company_ids: list[int] | None = None,
    portal_account_id: int | None = None,
) -> dict[str, Any]:
    label_join = and_(
        ServiceLabel.company_id == ServiceCatalog.company_id,
        ServiceLabel.service_id == ServiceCatalog.service_id,
    )
    assignment_join = and_(
        ServiceKpiAssignment.company_id == ServiceCatalog.company_id,
        ServiceKpiAssignment.service_id == ServiceCatalog.service_id,
    )
    stmt = (
        select(
            ServiceCatalog.company_id,
            Company.title.label('company_title'),
            ServiceCatalog.service_id,
            ServiceCatalog.title,
            ServiceCatalog.price_min,
            ServiceCatalog.duration,
            ServiceCatalog.category_id,
            ServiceCatalog.category_title,
            ServiceCatalog.updated_at,
            ServiceLabel.is_extra,
            ServiceLabel.source.label('label_source'),
            ServiceLabel.updated_at.label('label_updated_at'),
            ServiceKpiAssignment.group_id,
            ServiceKpiAssignment.updated_at.label('assignment_updated_at'),
            ServiceKpiGroup.code.label('group_code'),
            ServiceKpiGroup.title.label('group_title'),
            ServiceKpiGroup.is_active.label('group_is_active'),
        )
        .select_from(ServiceCatalog)
        .join(Company, Company.id == ServiceCatalog.company_id)
        .outerjoin(ServiceLabel, label_join)
        .outerjoin(ServiceKpiAssignment, assignment_join)
        .outerjoin(ServiceKpiGroup, ServiceKpiGroup.id == ServiceKpiAssignment.group_id)
    )
    filters = []
    if company_id is not None:
        filters.append(ServiceCatalog.company_id == company_id)
    elif allowed_company_ids is not None:
        filters.append(ServiceCatalog.company_id.in_(allowed_company_ids))
    if q:
        needle = f'%{q.strip().lower()}%'
        filters.append(
            or_(
                func.lower(ServiceCatalog.title).like(needle),
                cast(ServiceCatalog.service_id, String).like(f'%{q.strip()}%'),
            )
        )
    if is_extra is True:
        filters.append(ServiceLabel.is_extra.is_(True))
    elif is_extra is False:
        filters.append(or_(ServiceLabel.service_id.is_(None), ServiceLabel.is_extra.is_(False)))
    if kpi_group_id is not None:
        filters.append(ServiceKpiAssignment.group_id == kpi_group_id)
    latest_appointment_date = await db.scalar(select(func.max(Appointment.date)))
    if latest_appointment_date is not None:
        active_since = latest_appointment_date - timedelta(days=365)
        active_service = exists(
            select(1)
            .select_from(Transaction)
            .join(
                Appointment,
                and_(
                    Appointment.id == Transaction.appointment_id,
                    Appointment.company_id == Transaction.company_id,
                ),
            )
            .where(
                Transaction.company_id == ServiceCatalog.company_id,
                Transaction.service_id == ServiceCatalog.service_id,
                Appointment.date >= active_since,
            )
        )
        filters.append(active_service)
    if filters:
        stmt = stmt.where(*filters)
    stmt = stmt.order_by(Company.title.asc(), ServiceCatalog.category_title.asc(), ServiceCatalog.title.asc())
    rows = (await db.execute(stmt)).all()

    out_rows = [
        {
                'company_id': int(row.company_id),
                'company_title': row.company_title,
                'service_id': int(row.service_id),
                'title': row.title or '',
                'price_min': row.price_min,
                'duration': row.duration,
                'category_id': row.category_id,
                'category_title': _service_display_category(row.category_title, row.title),
                'updated_at': _iso_datetime(row.updated_at),
                'is_extra': bool(row.is_extra) if row.is_extra is not None else False,
                'label_source': row.label_source,
                'label_updated_at': _iso_datetime(row.label_updated_at),
                'kpi_group_id': row.group_id,
                'kpi_group_code': row.group_code,
                'kpi_group_title': row.group_title,
                'kpi_group_is_active': bool(row.group_is_active) if row.group_is_active is not None else None,
                'kpi_assignment_updated_at': _iso_datetime(row.assignment_updated_at),
            }
            for row in rows
    ]
    categories = sorted({row['category_title'] for row in out_rows if row['category_title']})
    if category:
        out_rows = [row for row in out_rows if row['category_title'] == category]

    return {
        'rows': out_rows,
        'groups': await fetch_service_kpi_groups(
            db,
            portal_account_id=portal_account_id,
            include_inactive=True,
        ),
        'categories': categories,
        'total': len(out_rows),
    }


async def save_service_label(
    db: AsyncSession,
    company_id: int,
    service_id: int,
    *,
    is_extra: bool,
    allowed_company_ids: list[int] | None = None,
    portal_account_id: int | None = None,
) -> dict[str, Any]:
    if allowed_company_ids is not None and company_id not in set(allowed_company_ids):
        raise ValueError('company is not allowed')
    catalog = await _service_catalog_row(db, company_id, service_id)
    if catalog is None:
        raise ValueError('unknown service for company')

    now = datetime.utcnow()
    if is_extra:
        legacy_service = await db.get(Service, service_id)
        if legacy_service is None:
            db.add(
                Service(
                    id=service_id,
                    title=catalog.title,
                    price_min=catalog.price_min,
                    duration=catalog.duration,
                    category_title=catalog.category_title,
                    company_id=company_id,
                )
            )
            await db.flush()

        label = await db.get(ServiceLabel, {'company_id': company_id, 'service_id': service_id})
        if label is None:
            db.add(
                ServiceLabel(
                    company_id=company_id,
                    service_id=service_id,
                    is_extra=True,
                    source='dashboard',
                    updated_at=now,
                )
            )
        else:
            label.is_extra = True
            label.source = 'dashboard'
            label.updated_at = now
    else:
        await db.execute(
            delete(ServiceLabel).where(
                ServiceLabel.company_id == company_id,
                ServiceLabel.service_id == service_id,
            )
        )

    await db.commit()
    return await fetch_dashboard_services(
        db,
        company_id=company_id,
        q=str(service_id),
        allowed_company_ids=allowed_company_ids,
        portal_account_id=portal_account_id,
    )


async def create_service_kpi_group(
    db: AsyncSession,
    *,
    portal_account_id: int,
    title: str,
    code: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    is_active: bool = True,
) -> dict[str, Any]:
    clean_title = str(title or '').strip()
    if not clean_title:
        raise ValueError('title is required')
    now = datetime.utcnow()
    group = ServiceKpiGroup(
        portal_account_id=portal_account_id,
        code=await _unique_service_group_code(db, code or clean_title, portal_account_id=portal_account_id),
        title=clean_title,
        description=str(description or '').strip() or None,
        sort_order=int(sort_order or 0),
        is_active=bool(is_active),
        created_at=now,
        updated_at=now,
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return _service_kpi_group_payload(group)


async def update_service_kpi_group(
    db: AsyncSession,
    group_id: int,
    *,
    portal_account_id: int | None = None,
    title: str | None = None,
    code: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    is_active: bool | None = None,
) -> dict[str, Any]:
    group = await db.get(ServiceKpiGroup, group_id)
    if group is None:
        raise ValueError('unknown KPI group')
    if portal_account_id is not None and group.portal_account_id != portal_account_id:
        raise ValueError('unknown KPI group')
    if title is not None:
        clean_title = str(title or '').strip()
        if not clean_title:
            raise ValueError('title is required')
        group.title = clean_title
    if code is not None:
        group.code = await _unique_service_group_code(
            db,
            code,
            portal_account_id=group.portal_account_id,
            ignore_group_id=group_id,
        )
    if description is not None:
        group.description = str(description or '').strip() or None
    if sort_order is not None:
        group.sort_order = int(sort_order or 0)
    if is_active is not None:
        group.is_active = bool(is_active)
    group.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(group)
    return _service_kpi_group_payload(group)


async def archive_service_kpi_group(
    db: AsyncSession,
    group_id: int,
    *,
    portal_account_id: int | None = None,
) -> dict[str, Any]:
    return await update_service_kpi_group(
        db,
        group_id,
        portal_account_id=portal_account_id,
        is_active=False,
    )


async def save_service_kpi_assignment(
    db: AsyncSession,
    company_id: int,
    service_id: int,
    *,
    group_id: int | None,
    allowed_company_ids: list[int] | None = None,
    portal_account_id: int | None = None,
) -> dict[str, Any]:
    if allowed_company_ids is not None and company_id not in set(allowed_company_ids):
        raise ValueError('company is not allowed')
    catalog = await _service_catalog_row(db, company_id, service_id)
    if catalog is None:
        raise ValueError('unknown service for company')

    if group_id is None:
        await db.execute(
            delete(ServiceKpiAssignment).where(
                ServiceKpiAssignment.company_id == company_id,
                ServiceKpiAssignment.service_id == service_id,
            )
        )
        await db.commit()
        return await fetch_dashboard_services(
            db,
            company_id=company_id,
            q=str(service_id),
            allowed_company_ids=allowed_company_ids,
            portal_account_id=portal_account_id,
        )

    group = await db.get(ServiceKpiGroup, group_id)
    if group is None or not group.is_active:
        raise ValueError('unknown active KPI group')
    if portal_account_id is not None and group.portal_account_id != portal_account_id:
        raise ValueError('unknown active KPI group')

    assignment = await db.get(ServiceKpiAssignment, {'company_id': company_id, 'service_id': service_id})
    now = datetime.utcnow()
    if assignment is None:
        db.add(
            ServiceKpiAssignment(
                company_id=company_id,
                service_id=service_id,
                group_id=group_id,
                source='dashboard',
                updated_at=now,
            )
        )
    else:
        assignment.group_id = group_id
        assignment.source = 'dashboard'
        assignment.updated_at = now
    await db.commit()
    return await fetch_dashboard_services(
        db,
        company_id=company_id,
        q=str(service_id),
        allowed_company_ids=allowed_company_ids,
        portal_account_id=portal_account_id,
    )


async def fetch_top_services(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int] = None,
    limit: int = 10,
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    title_expr = func.trim(func.coalesce(func.nullif(Transaction.service_title, ''), ServiceCatalog.title, ''))
    group_key = _service_group_key(title_expr, Transaction.service_id)
    count_stmt = (
        select(
            group_key.label('group_key'),
            func.min(Transaction.service_id).label('service_id'),
            func.min(title_expr).label('service_title'),
            func.sum(Transaction.amount).label('sold'),
            func.count(func.distinct(Transaction.service_id)).label('service_count'),
            func.count(func.distinct(Appointment.company_id)).label('branch_count'),
        )
        .select_from(Transaction)
        .join(Appointment, Appointment.id == Transaction.appointment_id)
        .outerjoin(ServiceCatalog, _transaction_service_catalog_join())
        .where(
            _appt_revenue_filters(start, end, company_id, staff_id, allowed_company_ids=allowed_company_ids),
        )
        .group_by(group_key)
    )
    tx_titles = (
        select(
            Transaction.appointment_id.label('record_id'),
            Transaction.service_id.label('service_id'),
            func.min(func.nullif(Transaction.service_title, '')).label('service_title'),
        )
        .group_by(Transaction.appointment_id, Transaction.service_id)
        .subquery()
    )
    paid_title_expr = func.trim(
        func.coalesce(
            tx_titles.c.service_title,
            func.nullif(ServiceCatalog.title, ''),
            cast(FinancialTransaction.sold_item_id, String),
        )
    )
    paid_group_key = _service_group_key(paid_title_expr, FinancialTransaction.sold_item_id)
    paid_revenue = func.coalesce(func.sum(FinancialTransaction.amount), 0.0)
    paid_stmt = (
        select(
            paid_group_key.label('group_key'),
            func.min(FinancialTransaction.sold_item_id).label('service_id'),
            func.min(paid_title_expr).label('service_title'),
            paid_revenue.label('revenue'),
            func.count(func.distinct(FinancialTransaction.sold_item_id)).label('service_count'),
            func.count(func.distinct(Appointment.company_id)).label('branch_count'),
        )
        .select_from(FinancialTransaction)
        .join(Appointment, Appointment.id == FinancialTransaction.record_id)
        .outerjoin(
            tx_titles,
            and_(
                tx_titles.c.record_id == FinancialTransaction.record_id,
                tx_titles.c.service_id == FinancialTransaction.sold_item_id,
            ),
        )
        .outerjoin(ServiceCatalog, _financial_service_catalog_join())
        .where(_service_paid_filters(start, end, company_id, staff_id, allowed_company_ids=allowed_company_ids))
        .group_by(paid_group_key)
        .order_by(paid_revenue.desc())
        .limit(limit)
    )
    count_rows = (await db.execute(count_stmt)).all()
    paid_rows = (await db.execute(paid_stmt)).all()
    counts_by_key = {str(r.group_key): r for r in count_rows}
    out = []
    for r in paid_rows:
        key = str(r.group_key)
        counts = counts_by_key.get(key)
        out.append({
            'service_id': counts.service_id if counts is not None else r.service_id,
            'title': (counts.service_title if counts is not None else r.service_title) or '',
            'sold': int((counts.sold if counts is not None else 0) or 0),
            'revenue': float(r.revenue or 0),
            'service_count': max(
                int((counts.service_count if counts is not None else 0) or 0),
                int(r.service_count or 0),
            ),
            'branch_count': max(
                int((counts.branch_count if counts is not None else 0) or 0),
                int(r.branch_count or 0),
            ),
        })
    return out


async def fetch_extra_services(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int] = None,
    limit: Optional[int] = None,
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
) -> list[dict[str, Any]]:
    title_expr = func.trim(func.coalesce(func.nullif(Transaction.service_title, ''), ServiceCatalog.title, ''))
    group_key = _service_group_key(title_expr, Transaction.service_id)
    count_stmt = (
        select(
            group_key.label('group_key'),
            func.min(Transaction.service_id).label('service_id'),
            func.min(title_expr).label('service_title'),
            func.coalesce(func.sum(Transaction.amount), 0).label('sold'),
            func.count(func.distinct(Transaction.service_id)).label('service_count'),
            func.count(func.distinct(Appointment.company_id)).label('branch_count'),
        )
        .select_from(Transaction)
        .join(Appointment, Appointment.id == Transaction.appointment_id)
        .join(ServiceLabel, _transaction_service_label_join())
        .outerjoin(ServiceCatalog, _transaction_service_catalog_join())
        .where(
            _appt_revenue_filters(start, end, company_id, staff_id, allowed_company_ids=allowed_company_ids),
            ServiceLabel.is_extra.is_(True),
        )
        .group_by(group_key)
    )
    tx_titles = (
        select(
            Transaction.appointment_id.label('record_id'),
            Transaction.service_id.label('service_id'),
            func.min(func.nullif(Transaction.service_title, '')).label('service_title'),
        )
        .group_by(Transaction.appointment_id, Transaction.service_id)
        .subquery()
    )
    paid_title_expr = func.trim(
        func.coalesce(
            tx_titles.c.service_title,
            func.nullif(ServiceCatalog.title, ''),
            cast(FinancialTransaction.sold_item_id, String),
        )
    )
    paid_group_key = _service_group_key(paid_title_expr, FinancialTransaction.sold_item_id)
    paid_stmt = (
        select(
            paid_group_key.label('group_key'),
            func.coalesce(func.sum(FinancialTransaction.amount), 0.0).label('revenue'),
        )
        .select_from(FinancialTransaction)
        .join(Appointment, Appointment.id == FinancialTransaction.record_id)
        .outerjoin(
            tx_titles,
            and_(
                tx_titles.c.record_id == FinancialTransaction.record_id,
                tx_titles.c.service_id == FinancialTransaction.sold_item_id,
            ),
        )
        .join(ServiceLabel, _financial_service_label_join())
        .outerjoin(ServiceCatalog, _financial_service_catalog_join())
        .where(
            _service_paid_filters(start, end, company_id, staff_id, allowed_company_ids=allowed_company_ids),
            ServiceLabel.is_extra.is_(True),
        )
        .group_by(paid_group_key)
    )
    count_rows = (await db.execute(count_stmt)).all()
    paid_by_key = {str(r.group_key): float(r.revenue or 0) for r in (await db.execute(paid_stmt)).all()}
    rows = [
        {
            'service_id': r.service_id,
            'title': r.service_title or '',
            'sold': int(r.sold or 0),
            'revenue': paid_by_key.get(str(r.group_key), 0.0),
            'service_count': int(r.service_count or 0),
            'branch_count': int(r.branch_count or 0),
        }
        for r in count_rows
    ]
    rows.sort(key=lambda item: (item['sold'], item['revenue']), reverse=True)
    return rows[:limit] if limit is not None else rows


async def _service_group_counts(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_id: Optional[int] = None,
) -> dict[str, float]:
    title_expr = func.lower(func.coalesce(Transaction.service_title, ServiceCatalog.title, ''))
    stmt = (
        select(
            _service_qty_sum(title_expr, WAX_TITLE_PARTS).label('wax_qty'),
            _service_qty_sum(title_expr, CAMOUFLAGE_TITLE_PARTS).label('camouflage_qty'),
            _service_qty_sum(title_expr, FACE_CARE_TITLE_PARTS).label('face_care_qty'),
            _service_qty_sum(title_expr, HEAD_CARE_TITLE_PARTS).label('head_care_qty'),
        )
        .select_from(Transaction)
        .join(Appointment, Appointment.id == Transaction.appointment_id)
        .outerjoin(ServiceCatalog, _transaction_service_catalog_join())
        .where(_appt_revenue_filters(start, end, company_id, staff_id))
    )
    row = (await db.execute(stmt)).one()
    return {
        'wax_qty': float(row.wax_qty or 0),
        'camouflage_qty': float(row.camouflage_qty or 0),
        'face_care_qty': float(row.face_care_qty or 0),
        'head_care_qty': float(row.head_care_qty or 0),
    }


async def _goods_sales_metrics(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_id: Optional[int] = None,
) -> dict[str, float]:
    # YClients stores goods sales as negative stock movements.
    sold_qty = func.sum(-func.coalesce(GoodTransaction.amount, 0.0))
    sold_sum = func.sum(func.coalesce(GoodTransaction.cost, 0.0))
    stmt = (
        select(
            func.coalesce(sold_qty, 0.0).label('qty'),
            func.coalesce(sold_sum, 0.0).label('revenue'),
        )
        .where(_goods_revenue_filters(start, end, company_id, staff_id))
    )
    row = (await db.execute(stmt)).one()
    return {
        'cosmo_qty': float(row.qty or 0),
        'cosmo_sum': float(row.revenue or 0),
    }


async def _opz_events(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    created_user_id: Optional[int] = None,
) -> list[OpzEvent]:
    create_start = datetime.combine(start, time.min)
    create_end = datetime.combine(end + timedelta(days=1), time.min)
    candidate_filters = [
        Appointment.company_id == company_id,
        Appointment.client_id.is_not(None),
        Appointment.date.is_not(None),
        Appointment.create_date.is_not(None),
        Appointment.create_date >= create_start,
        Appointment.create_date < create_end,
    ]
    if created_user_id is not None:
        candidate_filters.append(Appointment.created_user_id == created_user_id)
    candidates_stmt = (
        select(
            Appointment.id,
            Appointment.company_id,
            Appointment.client_id,
            Appointment.date,
            Appointment.create_date,
            Appointment.created_user_id,
        )
        .where(*candidate_filters)
        .order_by(Appointment.create_date.asc(), Appointment.id.asc())
    )
    candidates = (await db.execute(candidates_stmt)).all()
    if not candidates:
        return []

    client_ids = sorted({candidate.client_id for candidate in candidates if candidate.client_id is not None})
    visit_filters = [
        Appointment.company_id == company_id,
        Appointment.attendance == COMPLETED_ATTENDANCE,
        Appointment.client_id.in_(client_ids),
        Appointment.date.is_not(None),
        Appointment.date <= end,
    ]
    visits_stmt = (
        select(
            Appointment.company_id,
            Appointment.client_id,
            Appointment.staff_id,
            Appointment.date,
            Appointment.datetime,
            Appointment.id,
        )
        .where(*visit_filters)
    )
    visits = (await db.execute(visits_stmt)).all()
    visits_by_client: dict[tuple[int, int], list[Any]] = {}
    for visit in visits:
        visits_by_client.setdefault((visit.company_id, visit.client_id), []).append(visit)

    events: list[OpzEvent] = []
    booked_clients: set[tuple[int, int]] = set()
    for candidate in candidates:
        create_day = candidate.create_date.date()
        last_visits = [
            visit
            for visit in visits_by_client.get((candidate.company_id, candidate.client_id), [])
            if visit.date <= create_day
        ]
        if not last_visits:
            continue
        last_visit = max(
            last_visits,
            key=lambda visit: (
                _coerce_date(visit.date),
                visit.datetime or datetime.combine(_coerce_date(visit.date), time.min),
                int(visit.id or 0),
            ),
        )
        last_visit_date = _coerce_date(last_visit.date)
        if candidate.date <= last_visit_date:
            continue
        if create_day not in {last_visit_date, last_visit_date + timedelta(days=1)}:
            continue
        client_key = (int(candidate.company_id), int(candidate.client_id))
        if client_key in booked_clients:
            continue
        booked_clients.add(client_key)
        events.append(
            OpzEvent(
                event_id=int(candidate.id),
                company_id=int(candidate.company_id),
                client_id=int(candidate.client_id),
                create_date=candidate.create_date,
                appointment_date=_coerce_date(candidate.date),
                barber_staff_id=int(last_visit.staff_id) if last_visit.staff_id is not None else None,
                created_user_id=(
                    int(candidate.created_user_id)
                    if candidate.created_user_id is not None
                    else None
                ),
            )
        )

    return events


async def _opz_count(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_id: Optional[int] = None,
    created_user_id: Optional[int] = None,
) -> float:
    events = await _opz_events(db, start, end, company_id, created_user_id=created_user_id)
    if staff_id is not None:
        events = [event for event in events if event.barber_staff_id == staff_id]
    return float(len(events))


async def _opz_count_scope(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int],
    company_ids: Optional[list[int]] = None,
) -> float:
    if company_ids is None:
        company_ids = await _appointment_company_ids(db, company_id, staff_id)
    total = 0.0
    for item_company_id in company_ids:
        total += await _opz_count(db, start, end, item_company_id, staff_id=staff_id)
    return total


async def _admin_event_counts(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_ids: list[int],
    user_id_by_staff: dict[int, Optional[int]],
    events: list[AdminAssignmentEvent],
) -> dict[int, int]:
    if not staff_ids:
        return {}
    ordered_staff_ids = sorted({int(staff_id) for staff_id in staff_ids})
    counts = {staff_id: 0 for staff_id in ordered_staff_ids}
    if not events:
        return counts

    user_to_staff = {
        int(user_id): staff_id
        for staff_id, user_id in user_id_by_staff.items()
        if staff_id in counts and user_id is not None
    }

    schedule_rows = (
        await db.execute(
            select(
                StaffSchedule.staff_id,
                StaffSchedule.date,
                StaffSchedule.slot_from,
                StaffSchedule.slot_to,
            )
            .where(
                StaffSchedule.staff_id.in_(ordered_staff_ids),
                StaffSchedule.company_id == company_id,
                StaffSchedule.date >= start,
                StaffSchedule.date <= end,
            )
            .order_by(StaffSchedule.date.asc(), StaffSchedule.slot_from.asc(), StaffSchedule.staff_id.asc())
        )
    ).all()
    schedules_by_date: dict[date, list[Any]] = {}
    for row in schedule_rows:
        schedules_by_date.setdefault(_coerce_date(row.date), []).append(row)

    for event in sorted(events, key=lambda item: (item.event_date, item.event_moment or datetime.min, item.event_id)):
        creator_staff_id = (
            user_to_staff.get(int(event.created_user_id))
            if event.created_user_id is not None
            else None
        )
        scheduled_staff_ids = {
            int(schedule.staff_id)
            for schedule in schedules_by_date.get(event.event_date, [])
            if _schedule_slot_covers_datetime(
                schedule.slot_from,
                schedule.slot_to,
                event.event_moment,
            )
        }
        if creator_staff_id in scheduled_staff_ids:
            assigned_staff_id = creator_staff_id
        elif scheduled_staff_ids:
            assigned_staff_id = min(scheduled_staff_ids, key=lambda sid: (counts[sid], sid))
        elif creator_staff_id in counts:
            assigned_staff_id = creator_staff_id
        else:
            assigned_staff_id = min(ordered_staff_ids, key=lambda sid: (counts[sid], sid))
        counts[assigned_staff_id] += 1

    return counts


async def _admin_opz_by_created_appointments(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_ids: list[int],
    user_id_by_staff: dict[int, Optional[int]],
    barber_staff_ids: list[int] | None = None,
) -> dict[int, int]:
    if not staff_ids:
        return {}
    opz_events = await _opz_events(db, start, end, company_id)
    if barber_staff_ids:
        allowed_barbers = {int(staff_id) for staff_id in barber_staff_ids}
        opz_events = [
            event for event in opz_events
            if event.barber_staff_id in allowed_barbers
        ]
    events = [
        AdminAssignmentEvent(
            event_id=event.event_id,
            event_date=event.create_date.date(),
            event_moment=event.create_date,
            created_user_id=event.created_user_id,
        )
        for event in opz_events
    ]
    return await _admin_event_counts(db, start, end, company_id, staff_ids, user_id_by_staff, events)


async def _admin_clients_by_finished_appointments(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_ids: list[int],
    user_id_by_staff: dict[int, Optional[int]],
    barber_staff_ids: list[int] | None = None,
) -> dict[int, int]:
    """Assign each finished appointment to at most one admin, keeping admin totals aligned with barbers."""
    if not staff_ids:
        return {}

    appointment_filters = [
        Appointment.company_id == company_id,
        Appointment.date >= start,
        Appointment.date <= end,
        Appointment.attendance == COMPLETED_ATTENDANCE,
    ]
    if barber_staff_ids:
        appointment_filters.append(Appointment.staff_id.in_(barber_staff_ids))

    appointment_rows = (
        await db.execute(
            select(
                Appointment.id,
                Appointment.date,
                Appointment.datetime,
                Appointment.created_user_id,
            )
            .where(*appointment_filters)
            .order_by(Appointment.date.asc(), Appointment.datetime.asc(), Appointment.id.asc())
        )
    ).all()
    events = [
        AdminAssignmentEvent(
            event_id=int(appointment.id),
            event_date=_coerce_date(appointment.date),
            event_moment=appointment.datetime,
            created_user_id=(
                int(appointment.created_user_id)
                if appointment.created_user_id is not None
                else None
            ),
        )
        for appointment in appointment_rows
    ]
    return await _admin_event_counts(db, start, end, company_id, staff_ids, user_id_by_staff, events)


async def _fact_metric_components(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_id: Optional[int] = None,
    created_user_id: Optional[int] = None,
    clients_override: Optional[float] = None,
    opz_override: Optional[float] = None,
) -> dict[str, float]:
    average_check = await _average_check_block(
        db,
        DateRange(start, end),
        company_id,
        staff_id,
        created_user_id=created_user_id,
    )
    opz_staff_id = None if created_user_id is not None else staff_id
    goods_metrics = await _goods_sales_metrics(db, start, end, company_id, staff_id)
    clients_count = float(clients_override) if clients_override is not None else float(
        await db.scalar(
            select(func.count(func.distinct(Appointment.id))).where(
                _appt_revenue_filters(
                    start,
                    end,
                    company_id,
                    staff_id,
                    created_user_id=created_user_id,
                )
            )
        )
        or 0
    )
    values: dict[str, float] = {
        'revenue': float(average_check['numerator'] or 0),
        'clients': clients_count,
        'avg_check_denominator': float(average_check['denominator'] or 0),
        'opz_qty': (
            float(opz_override)
            if opz_override is not None
            else await _opz_count(
                db, start, end, company_id,
                staff_id=opz_staff_id,
                created_user_id=created_user_id,
            )
        ),
    }
    values.update(await _service_group_counts(db, start, end, company_id, staff_id))
    values.update(goods_metrics)
    return _derive_metric_values(values, include_zero_derived=True, prefer_explicit=False)


async def _plan_metric_components_by_company(
    db: AsyncSession,
    start: date,
    end: date,
    company_ids: list[int],
) -> dict[int, dict[str, float]]:
    if not company_ids:
        return {}
    metric_codes = {metric['code'] for metric in PLAN_FACT_METRICS}
    stmt = (
        select(PlanMetric.company_id, PlanMetric.metric_code, PlanMetric.value)
        .where(
            PlanMetric.period_start == start,
            PlanMetric.period_end == end,
            PlanMetric.company_id.in_(company_ids),
            PlanMetric.staff_id.is_(None),
            PlanMetric.metric_code.in_(metric_codes),
        )
    )
    rows = (await db.execute(stmt)).all()
    out: dict[int, dict[str, float]] = {company_id: {} for company_id in company_ids}
    for row in rows:
        out.setdefault(row.company_id, {})[row.metric_code] = float(row.value or 0)
    return {
        company_id: _derive_metric_values(values, include_zero_derived=False)
        for company_id, values in out.items()
    }


async def _plan_metric_components_by_staff(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_ids: list[int],
) -> tuple[dict[int, dict[str, float]], dict[int, str]]:
    if not staff_ids:
        return {}, {}
    metric_codes = {metric['code'] for metric in PLAN_FACT_METRICS}
    stmt = (
        select(PlanMetric.staff_id, PlanMetric.staff_category, PlanMetric.metric_code, PlanMetric.value)
        .where(
            PlanMetric.period_start == start,
            PlanMetric.period_end == end,
            PlanMetric.company_id == company_id,
            PlanMetric.staff_id.in_(staff_ids),
            PlanMetric.metric_code.in_(metric_codes),
        )
    )
    rows = (await db.execute(stmt)).all()
    out: dict[int, dict[str, float]] = {staff_id: {} for staff_id in staff_ids}
    categories: dict[int, str] = {}
    for row in rows:
        if row.staff_id is None:
            continue
        staff_id = int(row.staff_id)
        out.setdefault(staff_id, {})[row.metric_code] = float(row.value or 0)
        if row.staff_category in STAFF_CATEGORY_METRIC_CODES:
            categories[staff_id] = row.staff_category
    return {
        staff_id: _derive_metric_values(values, include_zero_derived=False)
        for staff_id, values in out.items()
    }, categories


async def _manual_review_fact_values_by_staff(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: int,
    staff_ids: list[int],
) -> dict[int, float]:
    if not staff_ids:
        return {}
    stmt = (
        select(
            ManualFactMetric.staff_id,
            func.coalesce(func.sum(ManualFactMetric.value), 0.0).label('value'),
        )
        .where(
            ManualFactMetric.period_start >= start,
            ManualFactMetric.period_end <= end,
            ManualFactMetric.company_id == company_id,
            ManualFactMetric.staff_id.in_(staff_ids),
            ManualFactMetric.metric_code == REVIEWS_QTY_CODE,
        )
        .group_by(ManualFactMetric.staff_id)
    )
    rows = (await db.execute(stmt)).all()
    return {int(row.staff_id): float(row.value or 0.0) for row in rows}


async def _manual_review_fact_values_by_company(
    db: AsyncSession,
    start: date,
    end: date,
    company_ids: list[int],
) -> dict[int, float]:
    if not company_ids:
        return {}
    stmt = (
        select(
            ManualFactMetric.company_id,
            func.coalesce(func.sum(ManualFactMetric.value), 0.0).label('value'),
        )
        .where(
            ManualFactMetric.period_start >= start,
            ManualFactMetric.period_end <= end,
            ManualFactMetric.company_id.in_(company_ids),
            ManualFactMetric.metric_code == REVIEWS_QTY_CODE,
        )
        .group_by(ManualFactMetric.company_id)
    )
    rows = (await db.execute(stmt)).all()
    return {int(row.company_id): float(row.value or 0.0) for row in rows}


async def _resolve_plan_period(
    db: AsyncSession,
    start: date,
    end: date,
    company_ids: list[int],
) -> tuple[date, date]:
    if not company_ids:
        return start, end

    if start.year == end.year and start.month == end.month:
        month_start = date(start.year, start.month, 1)
        month_end = date(start.year, start.month, monthrange(start.year, start.month)[1])
        month_count = await db.scalar(
            select(func.count())
            .select_from(PlanMetric)
            .where(
                PlanMetric.period_start == month_start,
                PlanMetric.period_end == month_end,
                PlanMetric.company_id.in_(company_ids),
            )
        )
        if month_count:
            return month_start, month_end

    exact_count = await db.scalar(
        select(func.count())
        .select_from(PlanMetric)
        .where(
            PlanMetric.period_start == start,
            PlanMetric.period_end == end,
            PlanMetric.company_id.in_(company_ids),
        )
    )
    if exact_count:
        return start, end

    return start, end


def _metric_cells(
    plan_values: dict[str, float],
    fact_values: dict[str, float],
    metrics: tuple[dict[str, str], ...] = PLAN_FACT_METRICS,
) -> list[dict[str, Any]]:
    cells = []
    for metric in metrics:
        code = metric['code']
        fmt = metric['format']
        plan = _round_metric_value(plan_values.get(code), fmt)
        fact = _round_metric_value(fact_values.get(code, 0.0), fmt)
        remaining = None if plan is None else _round_metric_value(
            (plan or 0.0) - (fact or 0.0), fmt
        )
        if plan is None:
            completion_pct = None
        elif plan == 0:
            completion_pct = 100.0 if (fact or 0) >= 0 else None
        else:
            completion_pct = 100.0 * (fact or 0.0) / plan
        cells.append({
            'code': code,
            'plan': plan,
            'fact': fact,
            'remaining': remaining,
            'completion_pct': _round_metric_value(completion_pct, 'percent'),
            'status': _completion_status(completion_pct),
        })
    return cells


def _has_plan_values(plan_values: dict[str, float]) -> bool:
    return bool(plan_values)


def _metric_sets_payload() -> dict[str, list[dict[str, str]]]:
    return {
        'branch': list(PLAN_FACT_METRICS),
        **{
            category: list(metrics_for_category(category))
            for category in STAFF_CATEGORY_METRIC_CODES
        },
    }


def _metric_fact_value(group: dict[str, Any], code: str) -> float:
    for cell in group.get('metrics') or []:
        if cell.get('code') == code:
            return float(cell.get('fact') or 0.0)
    return 0.0


def _metric_plan_value(group: dict[str, Any], code: str) -> float | None:
    for cell in group.get('metrics') or []:
        if cell.get('code') == code:
            value = cell.get('plan')
            return None if value is None else float(value or 0.0)
    return None


def _staff_rankings_payload(groups: list[dict[str, Any]], limit: int = 5) -> dict[str, list[dict[str, Any]]]:
    def ranking(metric_code: str) -> list[dict[str, Any]]:
        rows = [
            {
                'company_id': group.get('company_id'),
                'staff_id': group.get('staff_id'),
                'title': group.get('title'),
                'position': group.get('position'),
                'category': group.get('category'),
                'category_label': group.get('category_label'),
                'value': _round_optional(_metric_fact_value(group, metric_code)) or 0.0,
            }
            for group in groups
            if group.get('staff_id') is not None
        ]
        rows.sort(key=lambda item: (-float(item['value'] or 0.0), str(item.get('title') or '')))
        return rows[:limit]

    return {
        'revenue_top': ranking('revenue'),
        'avg_check_top': ranking('avg_check_total'),
    }


def _goods_kpi_execution_payload(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    metric_by_code = {metric['code']: metric for metric in PLAN_FACT_METRICS}
    out = []
    for code in GOODS_KPI_CODES:
        metric = metric_by_code[code]
        fmt = metric['format']
        plan_values = [
            plan
            for plan in (_metric_plan_value(group, code) for group in groups)
            if plan is not None
        ]
        plan_raw = sum(plan_values) if plan_values else None
        fact_raw = sum(_metric_fact_value(group, code) for group in groups)
        plan = _round_metric_value(plan_raw, fmt)
        fact = _round_metric_value(fact_raw, fmt)
        if plan is None:
            completion_pct = None
        elif plan == 0:
            completion_pct = 100.0 if (fact or 0) >= 0 else None
        else:
            completion_pct = 100.0 * (fact or 0.0) / plan
        out.append({
            'code': code,
            'label': metric['label'],
            'format': fmt,
            'plan': plan,
            'fact': fact,
            'remaining': None if plan is None else _round_metric_value(
                (plan or 0.0) - (fact or 0.0), fmt
            ),
            'completion_pct': _round_metric_value(completion_pct, 'percent'),
            'status': _completion_status(completion_pct),
        })
    return out


def _selected_staff_plan_payload(
    selected_staff: dict[str, Any] | None,
    groups: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not selected_staff:
        return None
    staff_id = int(selected_staff['id'])
    group = next((item for item in groups if item.get('staff_id') == staff_id), None)
    if group is None:
        return None
    cells_by_code = {cell['code']: cell for cell in group.get('metrics') or []}
    metrics = []
    for metric in metrics_for_category(group.get('category')):
        cell = cells_by_code.get(metric['code'], {})
        metrics.append({
            'code': metric['code'],
            'label': metric['label'],
            'format': metric['format'],
            'plan': cell.get('plan'),
            'fact': cell.get('fact'),
            'remaining': cell.get('remaining'),
            'completion_pct': cell.get('completion_pct'),
            'status': cell.get('status'),
        })
    return {
        'company_id': group.get('company_id'),
        'staff_id': staff_id,
        'title': group.get('title') or selected_staff.get('name'),
        'position': group.get('position') or selected_staff.get('position'),
        'category': group.get('category'),
        'category_label': group.get('category_label'),
        'metrics': metrics,
    }


async def _client_fact_diagnostics(
    db: AsyncSession,
    start: date,
    end: date,
    branch_id: int,
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    barber_groups = [group for group in groups if group.get('category') == 'barber']
    admin_groups = [group for group in groups if group.get('category') == 'administrator']
    if not barber_groups or not admin_groups:
        return []

    barber_clients = sum(_metric_fact_value(group, 'clients') for group in barber_groups)
    admin_clients = sum(_metric_fact_value(group, 'clients') for group in admin_groups)
    if abs(barber_clients - admin_clients) <= 0.01:
        return []

    admin_staff_ids = [int(group['staff_id']) for group in admin_groups if group.get('staff_id') is not None]
    staff_rows = (
        await db.execute(
            select(Staff.id, Staff.user_id)
            .where(
                Staff.company_id == branch_id,
                Staff.id.in_(admin_staff_ids),
                Staff.fired == 0,
            )
        )
    ).all()
    valid_admin_user_ids = {
        int(row.user_id)
        for row in staff_rows
        if row.user_id is not None
    }
    missing_user_staff_ids = [
        int(row.id)
        for row in staff_rows
        if row.user_id is None
    ]

    unassigned_filters = [_appt_revenue_filters(start, end, branch_id)]
    if valid_admin_user_ids:
        unassigned_filters.append(
            or_(
                Appointment.created_user_id.is_(None),
                ~Appointment.created_user_id.in_(valid_admin_user_ids),
            )
        )

    unassigned_count = await db.scalar(
        select(func.count(func.distinct(Appointment.id)))
        .select_from(Appointment)
        .where(*unassigned_filters)
    )
    sample_rows = (
        await db.execute(
            select(Appointment.id)
            .where(*unassigned_filters)
            .order_by(Appointment.date.asc(), Appointment.id.asc())
            .limit(20)
        )
    ).all()

    return [{
        'code': 'admin_barber_clients_mismatch',
        'severity': 'warning',
        'message': (
            'Сумма клиентов в факте у администраторов не равна сумме клиентов '
            'в факте у барберов'
        ),
        'company_id': branch_id,
        'barber_clients_fact': _round_half_up_int(barber_clients or 0.0),
        'administrator_clients_fact': _round_half_up_int(admin_clients or 0.0),
        'unassigned_records_count': int(unassigned_count or 0),
        'sample_record_ids': [int(row.id) for row in sample_rows],
        'administrator_staff_without_user_id': missing_user_staff_ids,
    }]


def _staff_category(staff_row: Any, plan_category: str | None, plan_values: dict[str, float] | None = None) -> str:
    if plan_category in STAFF_CATEGORY_METRIC_CODES:
        return plan_category
    category = normalize_staff_category(getattr(staff_row, 'position', None))
    if category in STAFF_CATEGORY_METRIC_CODES:
        return category
    return 'barber' if plan_values else 'unknown'


async def _fetch_company_staff(
    db: AsyncSession,
    company_id: int,
    staff_id: Optional[int] = None,
) -> list[Any]:
    stmt = (
        select(Staff.id, Staff.name, Staff.position, Staff.user_id, Staff.fired)
        .where(Staff.company_id == company_id, Staff.fired == 0)
        .order_by(Staff.position.asc(), Staff.name.asc())
    )
    if staff_id is not None:
        stmt = stmt.where(Staff.id == staff_id)
    return [
        row for row in (await db.execute(stmt)).all()
        if (
            not _is_waitlist_staff_name(row.name)
            and not _is_admin_placeholder_staff_name(row.name)
        )
    ]


async def _staff_plan_groups_for_branch(
    db: AsyncSession,
    start: date,
    end: date,
    plan_start: date,
    plan_end: date,
    branch_id: int,
    staff_id: Optional[int] = None,
    include_all_when_branch_planned: bool = False,
) -> list[dict[str, Any]]:
    # Calculate administrator attribution against the same complete staff scope
    # used by the branch view. Applying staff_id before attribution assigns every
    # unclaimed event to the only remaining administrator and changes their fact.
    staff_rows = await _fetch_company_staff(db, branch_id)
    staff_ids = [int(row.id) for row in staff_rows]
    plans_by_staff, categories_by_staff = await _plan_metric_components_by_staff(
        db,
        plan_start,
        plan_end,
        branch_id,
        staff_ids,
    )
    has_staff_plans = any(_has_plan_values(plans_by_staff.get(int(staff.id), {})) for staff in staff_rows)
    if has_staff_plans or not include_all_when_branch_planned:
        staff_rows = [
            staff for staff in staff_rows
            if is_visible_staff_plan(plans_by_staff.get(int(staff.id), {}))
            and (
                _has_plan_values(plans_by_staff.get(int(staff.id), {}))
                or _staff_category(
                    staff,
                    categories_by_staff.get(int(staff.id)),
                    plans_by_staff.get(int(staff.id), {}),
                ) == 'administrator'
            )
        ]
    else:
        staff_rows = [
            staff for staff in staff_rows
            if is_visible_staff_plan(plans_by_staff.get(int(staff.id), {}))
        ]
    staff_ids = [int(row.id) for row in staff_rows]
    categories_by_staff_id: dict[int, str] = {}
    for staff in staff_rows:
        sid = int(staff.id)
        plan_values = plans_by_staff.get(sid, {})
        categories_by_staff_id[sid] = _staff_category(staff, categories_by_staff.get(sid), plan_values)

    user_id_by_staff: dict[int, Optional[int]] = {
        int(staff.id): getattr(staff, 'user_id', None) for staff in staff_rows
    }

    admin_staff_ids = [sid for sid in staff_ids if categories_by_staff_id.get(sid) == 'administrator']
    barber_staff_ids = [sid for sid in staff_ids if categories_by_staff_id.get(sid) == 'barber']
    admin_clients_by_staff = await _admin_clients_by_finished_appointments(
        db,
        start,
        end,
        branch_id,
        admin_staff_ids,
        user_id_by_staff,
        barber_staff_ids or None,
    )
    admin_opz_by_staff = await _admin_opz_by_created_appointments(
        db,
        start,
        end,
        branch_id,
        admin_staff_ids,
        user_id_by_staff,
        barber_staff_ids or None,
    )
    admin_review_facts_by_staff = await _manual_review_fact_values_by_staff(
        db,
        start,
        end,
        branch_id,
        admin_staff_ids,
    )

    facts_by_staff: dict[int, dict[str, float]] = {}
    for sid in staff_ids:
        is_admin = categories_by_staff_id.get(sid) == 'administrator'
        admin_user_id = user_id_by_staff.get(sid) if is_admin else None
        facts_by_staff[sid] = await _fact_metric_components(
            db,
            start,
            end,
            branch_id,
            sid,
            created_user_id=admin_user_id,
            clients_override=admin_clients_by_staff.get(sid) if is_admin else None,
            opz_override=admin_opz_by_staff.get(sid) if is_admin else None,
        )
        if is_admin:
            facts_by_staff[sid][REVIEWS_QTY_CODE] = admin_review_facts_by_staff.get(sid, 0.0)

    groups: list[dict[str, Any]] = []
    for staff in staff_rows:
        sid = int(staff.id)
        plan_values = plans_by_staff.get(sid, {})
        category = categories_by_staff_id[sid]
        metrics = metrics_for_category(category)
        groups.append({
            'company_id': branch_id,
            'staff_id': sid,
            'title': staff.name,
            'position': staff.position,
            'scope': 'staff',
            'category': category,
            'category_label': STAFF_CATEGORY_LABELS.get(category, STAFF_CATEGORY_LABELS['unknown']),
            'metrics': _metric_cells(
                plan_values,
                facts_by_staff.get(sid, {}),
                metrics,
            ),
        })
    if staff_id is not None:
        return [group for group in groups if group.get('staff_id') == staff_id]
    return groups


async def fetch_staff(
    db: AsyncSession,
    company_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> list[dict[str, Any]]:
    if force_allowed:
        allowed = allowed_company_ids or []
    elif allowed_company_ids is not None:
        allowed = allowed_company_ids
    else:
        allowed = await branch_company_ids(db)
    stmt = (
        select(
            Staff.id,
            Staff.name,
            Staff.position,
            Staff.user_id,
            Staff.company_id,
            Company.title.label('company_title'),
        )
        .select_from(Staff)
        .join(Company, Company.id == Staff.company_id)
        .where(Staff.fired == 0)
        .order_by(Company.title.asc(), Staff.name.asc(), Staff.id.asc())
    )
    if allowed is not None:
        stmt = stmt.where(Company.id.in_(allowed))
    if company_id is not None:
        stmt = stmt.where(Company.id == company_id)

    rows = (await db.execute(stmt)).all()
    return [
        {
            'id': row.id,
            'name': row.name,
            'position': row.position,
            'user_id': row.user_id,
            'company_id': row.company_id,
            'company_title': row.company_title,
        }
        for row in rows
        if (
            not _is_waitlist_staff_name(row.name)
            and not _is_admin_placeholder_staff_name(row.name)
        )
    ]


def _plan_month_range(month: str) -> tuple[date, date]:
    raw = str(month or '').strip()
    try:
        parsed = datetime.strptime(raw, '%Y-%m')
    except ValueError:
        raise ValueError('month must be in YYYY-MM format') from None
    start = date(parsed.year, parsed.month, 1)
    return start, date(parsed.year, parsed.month, monthrange(parsed.year, parsed.month)[1])


def _month_value(period_start: date) -> str:
    return f'{period_start.year:04d}-{period_start.month:02d}'


def _parse_setting_number(value: Any, field: str, *, percent: bool = False) -> float | None:
    if value is None or value == '':
        return None
    if isinstance(value, str):
        normalized = value.strip().replace('\xa0', '').replace(' ', '').replace('%', '')
        if not normalized:
            return None
        normalized = normalized.replace(',', '.')
        try:
            number = float(normalized)
        except ValueError:
            raise ValueError(f'{field} must be a number') from None
    else:
        try:
            number = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'{field} must be a number') from None
    if not math.isfinite(number):
        raise ValueError(f'{field} must be a finite number')
    if percent:
        if number < 1 or number > 100:
            raise ValueError(f'{field} must be between 1 and 100')
        return number / 100.0
    if number < 0:
        raise ValueError(f'{field} cannot be negative')
    return number


def _plan_staff_category(position: Any, fallback: Any = None) -> str:
    category = normalize_staff_category(fallback) or normalize_staff_category(position)
    return category if category == 'administrator' else 'barber'


def _setting_has_values(values: dict[str, float | None], fields: tuple[str, ...]) -> bool:
    return any(values.get(field) is not None for field in fields)


def _payload_branch_setting(branch: dict[str, Any], setting: PlanBranchSetting | None) -> dict[str, Any]:
    values = {
        field: (
            _round_optional(
                float(getattr(setting, field)) * 100.0
                if field in PERCENT_SETTING_FIELDS and getattr(setting, field, None) is not None
                else getattr(setting, field, None)
            )
            if setting is not None
            else None
        )
        for field in BRANCH_SETTING_FIELDS
    }
    return {
        'company_id': int(branch['id']),
        'company_title': branch['title'],
        **values,
        'updated_at': setting.updated_at.isoformat() if setting is not None else None,
    }


def _payload_staff_input(staff: dict[str, Any], item: PlanStaffInput | None) -> dict[str, Any]:
    category = item.staff_category if item is not None else _plan_staff_category(staff.get('position'))
    return {
        'company_id': int(staff['company_id']),
        'company_title': staff.get('company_title'),
        'staff_id': int(staff['id']),
        'staff_name': staff.get('name'),
        'position': staff.get('position'),
        'user_id': staff.get('user_id'),
        'staff_category': category if category in STAFF_CATEGORY_METRIC_CODES else 'barber',
        'clients': _round_metric_value(item.clients, 'number') if item is not None else None,
        'avg_check_total': _round_optional(item.avg_check_total) if item is not None else None,
        'reviews_qty': _round_metric_value(item.reviews_qty, 'number') if item is not None else None,
        'cosmo_qty': _round_metric_value(item.cosmo_qty, 'number') if item is not None else None,
        'updated_at': item.updated_at.isoformat() if item is not None else None,
    }


async def _plan_settings_snapshot(
    db: AsyncSession,
    period_start: date,
    period_end: date,
) -> tuple[dict[int, PlanBranchSetting], dict[int, PlanStaffInput]]:
    branch_rows = (
        await db.execute(
            select(PlanBranchSetting).where(
                PlanBranchSetting.period_start == period_start,
                PlanBranchSetting.period_end == period_end,
            )
        )
    ).scalars().all()
    staff_rows = (
        await db.execute(
            select(PlanStaffInput).where(
                PlanStaffInput.period_start == period_start,
                PlanStaffInput.period_end == period_end,
            )
        )
    ).scalars().all()
    return (
        {int(row.company_id): row for row in branch_rows},
        {int(row.staff_id): row for row in staff_rows},
    )


def _staff_metric_values_from_input(
    staff_input: dict[str, Any],
    branch_setting: dict[str, float | None],
) -> dict[str, float]:
    category = staff_input.get('staff_category')
    clients = float(staff_input.get('clients') or 0.0)
    avg_check = float(staff_input.get('avg_check_total') or 0.0)
    reviews_qty = float(staff_input.get('reviews_qty') or 0.0)
    cosmo_qty_input = float(staff_input.get('cosmo_qty') or 0.0)
    cosmo_price = float(branch_setting.get('cosmo_price') or 0.0)

    if category == 'administrator':
        values: dict[str, float] = {}
        if clients > 0:
            values['clients'] = _round_half_up_int(clients)
        if reviews_qty > 0:
            values[REVIEWS_QTY_CODE] = _round_half_up_int(reviews_qty)
        if cosmo_qty_input > 0:
            rounded_cosmo = _round_half_up_int(cosmo_qty_input)
            values['cosmo_qty'] = rounded_cosmo
            values['cosmo_sum'] = rounded_cosmo * cosmo_price
        return values

    if clients <= 0 or avg_check <= 0:
        return {}

    wax_qty = _round_half_up_int(clients * float(branch_setting.get('wax_pct') or 0.0))
    head_care_qty = _round_half_up_int(clients * float(branch_setting.get('head_care_pct') or 0.0))
    face_care_qty = _round_half_up_int(clients * float(branch_setting.get('face_care_pct') or 0.0))
    camouflage_qty = _round_half_up_int(clients * float(branch_setting.get('camouflage_pct') or 0.0))
    cosmo_qty = _round_half_up_int(clients * float(branch_setting.get('cosmo_pct') or 0.0))
    opz_qty = _round_half_up_int(clients * float(branch_setting.get('opz_pct') or 0.0))
    return {
        'revenue': clients * avg_check,
        'avg_check_total': avg_check,
        'clients': _round_half_up_int(clients),
        'wax_qty': wax_qty,
        'head_care_qty': head_care_qty,
        'face_care_qty': face_care_qty,
        'camouflage_qty': camouflage_qty,
        'cosmo_qty': cosmo_qty,
        'cosmo_sum': cosmo_qty * cosmo_price,
        'opz_qty': opz_qty,
    }


def _branch_metric_values_from_staff(
    staff_metric_rows: list[tuple[str, dict[str, float]]],
) -> dict[str, float]:
    values: dict[str, float] = {}
    for category, metrics in staff_metric_rows:
        if category == 'barber':
            for code in ('revenue', 'clients', 'wax_qty', 'head_care_qty', 'face_care_qty', 'camouflage_qty', 'cosmo_qty', 'cosmo_sum', 'opz_qty'):
                if code in metrics:
                    values[code] = values.get(code, 0.0) + float(metrics[code] or 0.0)
        elif category == 'administrator' and REVIEWS_QTY_CODE in metrics:
            values[REVIEWS_QTY_CODE] = values.get(REVIEWS_QTY_CODE, 0.0) + float(metrics[REVIEWS_QTY_CODE] or 0.0)
    return _derive_metric_values(values, include_zero_derived=False, prefer_explicit=False)


def _metric_preview(metrics: dict[str, float]) -> list[dict[str, Any]]:
    cells = []
    for metric in PLAN_FACT_METRICS:
        value = metrics.get(metric['code'])
        if value is None:
            continue
        cells.append({
            'code': metric['code'],
            'label': metric['label'],
            'format': metric['format'],
            'value': _round_metric_value(value, metric['format']),
        })
    return cells


def _calculate_plan_settings_metrics(
    branches: list[dict[str, Any]],
    staff_inputs: list[dict[str, Any]],
) -> tuple[dict[int, dict[str, float]], dict[int, dict[str, float]]]:
    branch_settings = {int(branch['company_id']): branch for branch in branches}
    staff_metrics: dict[int, dict[str, float]] = {}
    staff_rows_by_company: dict[int, list[tuple[str, dict[str, float]]]] = {}
    for item in staff_inputs:
        company_id = int(item['company_id'])
        staff_id = int(item['staff_id'])
        values = _staff_metric_values_from_input(item, branch_settings.get(company_id, {}))
        if not values:
            continue
        category = item.get('staff_category') if item.get('staff_category') in STAFF_CATEGORY_METRIC_CODES else 'barber'
        staff_metrics[staff_id] = _derive_metric_values(values, include_zero_derived=False)
        staff_rows_by_company.setdefault(company_id, []).append((category, values))

    branch_metrics = {
        int(branch['company_id']): _branch_metric_values_from_staff(staff_rows_by_company.get(int(branch['company_id']), []))
        for branch in branches
    }
    return branch_metrics, staff_metrics


async def fetch_plan_settings(
    db: AsyncSession,
    month: str,
    copy_from: Optional[str] = None,
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> dict[str, Any]:
    period_start, period_end = _plan_month_range(month)
    source_start, source_end = _plan_month_range(copy_from) if copy_from else (period_start, period_end)
    branches = await fetch_branches(db, allowed_company_ids, force_allowed=force_allowed)
    staff_rows = await fetch_staff(db, allowed_company_ids=allowed_company_ids, force_allowed=force_allowed)
    branch_settings, staff_inputs = await _plan_settings_snapshot(db, source_start, source_end)

    payload_branches = [_payload_branch_setting(branch, branch_settings.get(int(branch['id']))) for branch in branches]
    payload_staff = [
        _payload_staff_input(staff, staff_inputs.get(int(staff['id'])))
        for staff in staff_rows
        if any(int(branch['id']) == int(staff['company_id']) for branch in branches)
    ]
    calculation_branches = [
        {
            **branch,
            **{
                field: (
                    float(branch[field]) / 100.0
                    if branch.get(field) is not None
                    else None
                )
                for field in PERCENT_SETTING_FIELDS
            },
        }
        for branch in payload_branches
    ]
    branch_metrics, staff_metrics = _calculate_plan_settings_metrics(
        calculation_branches,
        payload_staff,
    )
    saved_at_candidates = [
        value
        for row in [*branch_settings.values(), *staff_inputs.values()]
        if (value := getattr(row, 'updated_at', None)) is not None
    ]
    last_saved_at = max(saved_at_candidates).isoformat() if saved_at_candidates and not copy_from else None

    return {
        'month': _month_value(period_start),
        'period': {'start': period_start.isoformat(), 'end': period_end.isoformat()},
        'copy_from': _month_value(source_start) if copy_from else None,
        'last_saved_at': last_saved_at,
        'branches': [
            {
                **branch,
                'preview': _metric_preview(branch_metrics.get(int(branch['company_id']), {})),
            }
            for branch in payload_branches
        ],
        'staff': [
            {
                **staff,
                'preview': _metric_preview(staff_metrics.get(int(staff['staff_id']), {})),
            }
            for staff in payload_staff
        ],
    }


def _normalize_plan_settings_payload(
    month: str,
    branches: list[dict[str, Any]],
    staff: list[dict[str, Any]],
) -> tuple[date, date, list[dict[str, Any]], list[dict[str, Any]]]:
    period_start, period_end = _plan_month_range(month)
    normalized_branches: list[dict[str, Any]] = []
    for row in branches:
        try:
            company_id = int(row.get('company_id'))
        except (TypeError, ValueError):
            raise ValueError('company_id is required for every branch setting') from None
        values = {
            field: _parse_setting_number(row.get(field), field, percent=field in PERCENT_SETTING_FIELDS)
            for field in BRANCH_SETTING_FIELDS
        }
        normalized_branches.append({'company_id': company_id, **values})

    normalized_staff: list[dict[str, Any]] = []
    for row in staff:
        try:
            company_id = int(row.get('company_id'))
            staff_id = int(row.get('staff_id'))
        except (TypeError, ValueError):
            raise ValueError('company_id and staff_id are required for every staff input') from None
        category = str(row.get('staff_category') or '').strip()
        if category not in STAFF_CATEGORY_METRIC_CODES:
            raise ValueError(f'invalid staff category for staff {staff_id}')
        values = {
            field: _parse_setting_number(row.get(field), field)
            for field in STAFF_INPUT_FIELDS
        }
        if category == 'barber' and values.get('clients') and not values.get('avg_check_total'):
            raise ValueError(f'avg_check_total is required for barber {staff_id} when clients is greater than zero')
        normalized_staff.append({
            'company_id': company_id,
            'staff_id': staff_id,
            'staff_category': category,
            **values,
        })
    return period_start, period_end, normalized_branches, normalized_staff


async def save_plan_settings(
    db: AsyncSession,
    month: str,
    branches: list[dict[str, Any]],
    staff: list[dict[str, Any]],
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> dict[str, Any]:
    period_start, period_end, normalized_branches, normalized_staff = _normalize_plan_settings_payload(month, branches, staff)
    branch_ids = sorted({int(row['company_id']) for row in normalized_branches})
    if not branch_ids:
        raise ValueError('at least one branch setting is required')

    allowed = (allowed_company_ids or []) if force_allowed else (allowed_company_ids if allowed_company_ids is not None else await branch_company_ids(db))
    if allowed is not None:
        invalid = sorted(set(branch_ids) - set(allowed))
        if invalid:
            raise ValueError(f'company is not allowed: {invalid[0]}')

    company_rows = (
        await db.execute(select(Company.id).where(Company.id.in_(branch_ids)))
    ).all()
    existing_company_ids = {int(row.id) for row in company_rows}
    missing_company_ids = sorted(set(branch_ids) - existing_company_ids)
    if missing_company_ids:
        raise ValueError(f'company does not exist: {missing_company_ids[0]}')

    staff_ids = sorted({int(row['staff_id']) for row in normalized_staff})
    if staff_ids:
        staff_rows = (
            await db.execute(
                select(Staff.id, Staff.company_id, Staff.name, Staff.position, Staff.fired)
                .where(Staff.id.in_(staff_ids), Staff.fired == 0)
            )
        ).all()
        valid_staff: dict[int, Any] = {int(row.id): row for row in staff_rows}
        for row in normalized_staff:
            staff_row = valid_staff.get(int(row['staff_id']))
            if staff_row is None:
                raise ValueError(f'staff does not exist or is fired: {row["staff_id"]}')
            if int(staff_row.company_id) != int(row['company_id']):
                raise ValueError(f'staff {row["staff_id"]} does not belong to company {row["company_id"]}')
            expected_category = _plan_staff_category(staff_row.position)
            if row['staff_category'] != expected_category:
                raise ValueError(f'staff {row["staff_id"]} category must be {expected_category}')

    now = datetime.now()
    await db.execute(
        delete(PlanBranchSetting).where(
            PlanBranchSetting.period_start == period_start,
            PlanBranchSetting.period_end == period_end,
            PlanBranchSetting.company_id.in_(branch_ids),
        )
    )
    await db.execute(
        delete(PlanStaffInput).where(
            PlanStaffInput.period_start == period_start,
            PlanStaffInput.period_end == period_end,
            PlanStaffInput.company_id.in_(branch_ids),
        )
    )
    await db.execute(
        delete(PlanMetric).where(
            PlanMetric.period_start == period_start,
            PlanMetric.period_end == period_end,
            PlanMetric.company_id.in_(branch_ids),
        )
    )

    for row in normalized_branches:
        if not _setting_has_values(row, BRANCH_SETTING_FIELDS):
            continue
        db.add(
            PlanBranchSetting(
                period_start=period_start,
                period_end=period_end,
                company_id=row['company_id'],
                wax_pct=row.get('wax_pct'),
                head_care_pct=row.get('head_care_pct'),
                face_care_pct=row.get('face_care_pct'),
                camouflage_pct=row.get('camouflage_pct'),
                cosmo_pct=row.get('cosmo_pct'),
                opz_pct=row.get('opz_pct'),
                cosmo_price=row.get('cosmo_price'),
                updated_at=now,
            )
        )

    for row in normalized_staff:
        if not _setting_has_values(row, STAFF_INPUT_FIELDS):
            continue
        db.add(
            PlanStaffInput(
                period_start=period_start,
                period_end=period_end,
                company_id=row['company_id'],
                staff_id=row['staff_id'],
                staff_category=row['staff_category'],
                clients=row.get('clients'),
                avg_check_total=row.get('avg_check_total'),
                reviews_qty=row.get('reviews_qty'),
                cosmo_qty=row.get('cosmo_qty'),
                updated_at=now,
            )
        )

    branch_metric_values, staff_metric_values = _calculate_plan_settings_metrics(normalized_branches, normalized_staff)
    staff_categories = {int(row['staff_id']): row['staff_category'] for row in normalized_staff}

    for company_id, values in branch_metric_values.items():
        for metric_code, value in values.items():
            db.add(
                PlanMetric(
                    period_start=period_start,
                    period_end=period_end,
                    company_id=company_id,
                    staff_id=None,
                    staff_category=None,
                    metric_code=metric_code,
                    value=value,
                    source=PLAN_SETTINGS_SOURCE,
                    updated_at=now,
                )
            )

    staff_company = {int(row['staff_id']): int(row['company_id']) for row in normalized_staff}
    for staff_id, values in staff_metric_values.items():
        for metric_code, value in values.items():
            db.add(
                PlanMetric(
                    period_start=period_start,
                    period_end=period_end,
                    company_id=staff_company[staff_id],
                    staff_id=staff_id,
                    staff_category=staff_categories[staff_id],
                    metric_code=metric_code,
                    value=value,
                    source=PLAN_SETTINGS_SOURCE,
                    updated_at=now,
                )
            )

    await db.commit()
    return await fetch_plan_settings(
        db,
        _month_value(period_start),
        allowed_company_ids=allowed_company_ids,
        force_allowed=force_allowed,
    )


def _is_manual_review_admin_row(row: Any) -> bool:
    name = getattr(row, 'name', None) or getattr(row, 'staff_name', None)
    return (
        normalize_staff_category(getattr(row, 'position', None)) == 'administrator'
        and not _is_waitlist_staff_name(name)
        and not _is_admin_placeholder_staff_name(name)
    )


def _manual_review_payload_row(
    row: Any,
    values_by_staff: dict[tuple[int, int], list[ManualFactMetric]],
) -> dict[str, Any]:
    company_id = int(row.company_id)
    staff_id = int(row.staff_id)
    items = values_by_staff.get((company_id, staff_id), [])
    total_value = sum(float(item.value or 0.0) for item in items)
    updated_at_values = [item.updated_at for item in items if item.updated_at is not None]

    return {
        'company_id': company_id,
        'company_title': row.company_title,
        'staff_id': staff_id,
        'staff_name': row.staff_name,
        'position': row.position,
        'value': _round_half_up_int(total_value or 0.0),
        'updated_at': max(updated_at_values).isoformat() if updated_at_values else None,
    }


async def fetch_manual_review_facts(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> dict[str, Any]:
    branches = await fetch_branches(db, allowed_company_ids, force_allowed=force_allowed)
    if company_id is not None:
        branches = [branch for branch in branches if int(branch['id']) == company_id]
    company_ids = [int(branch['id']) for branch in branches]
    if not company_ids:
        return {
            'period': {'start': start.isoformat(), 'end': end.isoformat()},
            'metric_code': REVIEWS_QTY_CODE,
            'total_value': 0.0,
            'rows': [],
        }

    stmt = (
        select(
            Staff.id.label('staff_id'),
            Staff.name.label('staff_name'),
            Staff.position,
            Staff.company_id,
            Company.title.label('company_title'),
        )
        .select_from(Staff)
        .join(Company, Company.id == Staff.company_id)
        .where(
            Staff.company_id.in_(company_ids),
            Staff.fired == 0,
        )
        .order_by(Company.title.asc(), Staff.name.asc(), Staff.id.asc())
    )
    if staff_id is not None:
        stmt = stmt.where(Staff.id == int(staff_id))

    rows = [
        row for row in (await db.execute(stmt)).all()
        if _is_manual_review_admin_row(row)
    ]
    staff_ids = [int(row.staff_id) for row in rows]
    values_by_staff: dict[tuple[int, int], list[ManualFactMetric]] = {}
    if staff_ids:
        manual_rows = (
            await db.execute(
                select(ManualFactMetric).where(
                    ManualFactMetric.period_start >= start,
                    ManualFactMetric.period_end <= end,
                    ManualFactMetric.company_id.in_(company_ids),
                    ManualFactMetric.staff_id.in_(staff_ids),
                    ManualFactMetric.metric_code == REVIEWS_QTY_CODE,
                )
            )
        ).scalars().all()
        for item in manual_rows:
            values_by_staff.setdefault(
                (int(item.company_id), int(item.staff_id)),
                [],
            ).append(item)

    payload_rows = [_manual_review_payload_row(row, values_by_staff) for row in rows]
    return {
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'metric_code': REVIEWS_QTY_CODE,
        'total_value': _round_half_up_int(sum(float(row['value'] or 0.0) for row in payload_rows)),
        'rows': payload_rows,
    }


async def save_manual_review_facts(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int],
    staff_id: Optional[int],
    items: list[dict[str, Any]],
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> dict[str, Any]:
    scoped_company_id = int(company_id) if company_id is not None else None
    scoped_staff_id = int(staff_id) if staff_id is not None else None
    normalized_items: dict[tuple[int, int], float | None] = {}
    for item in items:
        try:
            item_company_id = int(item.get('company_id'))
            item_staff_id = int(item.get('staff_id'))
        except (TypeError, ValueError):
            raise ValueError('company_id and staff_id are required for every row') from None
        if scoped_company_id is not None and item_company_id != scoped_company_id:
            raise ValueError(f'staff {item_staff_id} does not belong to selected company {scoped_company_id}')
        if scoped_staff_id is not None and item_staff_id != scoped_staff_id:
            raise ValueError(f'staff {item_staff_id} does not match selected staff {scoped_staff_id}')

        raw_value = item.get('value')
        if raw_value in (None, ''):
            value = None
        else:
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                raise ValueError(f'invalid reviews fact for staff {item_staff_id}') from None
            if value < 0:
                raise ValueError(f'reviews fact cannot be negative for staff {item_staff_id}')
        normalized_items[(item_company_id, item_staff_id)] = value

    if not normalized_items:
        return await fetch_manual_review_facts(
            db,
            start,
            end,
            scoped_company_id,
            scoped_staff_id,
            allowed_company_ids=allowed_company_ids,
            force_allowed=force_allowed,
        )

    allowed = (allowed_company_ids or []) if force_allowed else (
        allowed_company_ids if allowed_company_ids is not None else await branch_company_ids(db)
    )
    if allowed is not None:
        invalid_company_ids = sorted({company_id for company_id, _ in normalized_items} - set(allowed))
        if invalid_company_ids:
            raise ValueError(f'company is not allowed: {invalid_company_ids[0]}')

    staff_ids = sorted({staff_id for _, staff_id in normalized_items})
    staff_rows = (
        await db.execute(
            select(Staff.id, Staff.name, Staff.position, Staff.company_id, Staff.fired)
            .where(
                Staff.id.in_(staff_ids),
                Staff.fired == 0,
            )
        )
    ).all()
    valid_staff_keys = {
        (int(row.company_id), int(row.id))
        for row in staff_rows
        if _is_manual_review_admin_row(row)
    }
    item_staff_keys = set(normalized_items)
    invalid_keys = sorted(item_staff_keys - valid_staff_keys)
    if invalid_keys:
        invalid_company_id, invalid_staff_id = invalid_keys[0]
        raise ValueError(f'staff {invalid_staff_id} is not an active administrator in company {invalid_company_id}')

    now = datetime.now()
    for (item_company_id, item_staff_id), value in normalized_items.items():
        await db.execute(
            delete(ManualFactMetric).where(
                ManualFactMetric.period_start >= start,
                ManualFactMetric.period_end <= end,
                ManualFactMetric.company_id == item_company_id,
                ManualFactMetric.staff_id == item_staff_id,
                ManualFactMetric.metric_code == REVIEWS_QTY_CODE,
            )
        )
        if value is None:
            continue
        db.add(
            ManualFactMetric(
                period_start=start,
                period_end=end,
                company_id=item_company_id,
                staff_id=item_staff_id,
                metric_code=REVIEWS_QTY_CODE,
                value=value,
                source='dashboard',
                updated_at=now,
            )
        )

    await db.commit()
    return await fetch_manual_review_facts(
        db,
        start,
        end,
        scoped_company_id,
        scoped_staff_id,
        allowed_company_ids=allowed_company_ids,
        force_allowed=force_allowed,
    )


async def fetch_plan_fact(
    db: AsyncSession,
    start: date,
    end: date,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> dict[str, Any]:
    branches = await fetch_branches(db, allowed_company_ids, force_allowed=force_allowed)
    selected_staff: dict[str, Any] | None = None
    if staff_id is not None:
        staff_rows = await fetch_staff(
            db,
            allowed_company_ids=allowed_company_ids,
            force_allowed=force_allowed,
        )
        selected_staff = next((staff for staff in staff_rows if staff['id'] == staff_id), None)
        if selected_staff is not None:
            if company_id is None:
                company_id = int(selected_staff['company_id'])
            elif int(selected_staff['company_id']) != company_id:
                selected_staff = None
        if selected_staff is None and company_id is None:
            company_id = -1

    if company_id is not None:
        branches = [branch for branch in branches if branch['id'] == company_id]

    company_ids = [int(branch['id']) for branch in branches]
    plan_start, plan_end = await _resolve_plan_period(db, start, end, company_ids)
    plans_by_company = await _plan_metric_components_by_company(db, plan_start, plan_end, company_ids)

    if company_id is not None:
        if not company_ids:
            return {
                'period': {'start': start.isoformat(), 'end': end.isoformat()},
                'plan_period': {'start': plan_start.isoformat(), 'end': plan_end.isoformat()},
                'view_scope': 'staff',
                'selected_staff': selected_staff,
                'selected_staff_plan': None,
                'metrics': list(PLAN_FACT_METRICS),
                'metric_sets': _metric_sets_payload(),
                'diagnostics': [],
                'staff_rankings': _staff_rankings_payload([]),
                'goods_kpi_execution': _goods_kpi_execution_payload([]),
                'groups': [],
            }

        branch_id = company_ids[0]
        branch = branches[0]
        branch_fact = await _fact_metric_components(db, start, end, branch_id)
        branch_review_facts = await _manual_review_fact_values_by_company(db, start, end, [branch_id])
        branch_fact[REVIEWS_QTY_CODE] = branch_review_facts.get(branch_id, 0.0)
        groups = await _staff_plan_groups_for_branch(
            db,
            start,
            end,
            plan_start,
            plan_end,
            branch_id,
            staff_id,
            include_all_when_branch_planned=_has_plan_values(plans_by_company.get(branch_id, {})),
        )
        parent_group = {
            'company_id': branch_id,
            'title': branch['title'],
            'scope': 'branch',
            'metrics': _metric_cells(plans_by_company.get(branch_id, {}), branch_fact),
        }
        selected_staff_plan = _selected_staff_plan_payload(selected_staff, groups)
        diagnostics = await _client_fact_diagnostics(db, start, end, branch_id, groups)

        return {
            'period': {'start': start.isoformat(), 'end': end.isoformat()},
            'plan_period': {'start': plan_start.isoformat(), 'end': plan_end.isoformat()},
            'view_scope': 'staff',
            'branch': branch,
            'selected_staff': selected_staff,
            'selected_staff_plan': selected_staff_plan,
            'parent_group': parent_group,
            'metrics': list(PLAN_FACT_METRICS),
            'metric_sets': _metric_sets_payload(),
            'diagnostics': diagnostics,
            'staff_rankings': _staff_rankings_payload(groups),
            'goods_kpi_execution': _goods_kpi_execution_payload(groups),
            'groups': groups,
        }

    facts_by_company: dict[int, dict[str, float]] = {}
    staff_groups_by_company: dict[int, list[dict[str, Any]]] = {}
    for branch_id in company_ids:
        facts_by_company[branch_id] = await _fact_metric_components(db, start, end, branch_id)
    review_facts_by_company = await _manual_review_fact_values_by_company(db, start, end, company_ids)
    for branch_id in company_ids:
        facts_by_company.setdefault(branch_id, {})[REVIEWS_QTY_CODE] = review_facts_by_company.get(branch_id, 0.0)
        staff_groups_by_company[branch_id] = await _staff_plan_groups_for_branch(
            db,
            start,
            end,
            plan_start,
            plan_end,
            branch_id,
            None,
            include_all_when_branch_planned=_has_plan_values(plans_by_company.get(branch_id, {})),
        )

    groups: list[dict[str, Any]] = []
    if company_id is None and company_ids:
        network_plan = _sum_metric_components([plans_by_company.get(branch_id, {}) for branch_id in company_ids])
        network_fact = _sum_metric_components([facts_by_company.get(branch_id, {}) for branch_id in company_ids])
        network_average_check = await _average_check_block(
            db,
            DateRange(start, end),
            None,
            company_ids=company_ids,
        )
        network_fact['revenue'] = float(network_average_check['numerator'] or 0.0)
        network_fact['avg_check_denominator'] = float(network_average_check['denominator'] or 0.0)
        network_fact = _derive_metric_values(
            network_fact,
            include_zero_derived=True,
            prefer_explicit=False,
        )
        groups.append({
            'company_id': None,
            'title': 'Сеть',
            'scope': 'network',
            'metrics': _metric_cells(network_plan, network_fact),
        })

    for branch in branches:
        branch_id = int(branch['id'])
        groups.append({
            'company_id': branch_id,
            'title': branch['title'],
            'scope': 'branch',
            'metrics': _metric_cells(
                plans_by_company.get(branch_id, {}),
                facts_by_company.get(branch_id, {}),
            ),
        })

    all_staff_groups = [
        group
        for branch_id in company_ids
        for group in staff_groups_by_company.get(branch_id, [])
    ]

    return {
        'period': {'start': start.isoformat(), 'end': end.isoformat()},
        'plan_period': {'start': plan_start.isoformat(), 'end': plan_end.isoformat()},
        'view_scope': 'branch',
        'metrics': list(PLAN_FACT_METRICS),
        'metric_sets': _metric_sets_payload(),
        'diagnostics': [],
        'staff_rankings': _staff_rankings_payload(all_staff_groups),
        'goods_kpi_execution': _goods_kpi_execution_payload(all_staff_groups),
        'groups': groups,
    }


async def branch_company_ids(db: AsyncSession) -> Optional[list[int]]:
    """If portal_branches has rows, return allowed company ids; else None (all companies)."""
    try:
        cnt = await db.scalar(select(func.count()).select_from(PortalBranch))
    except (OperationalError, ProgrammingError, DBAPIError):
        return None
    if not cnt:
        return None
    r = await db.execute(select(PortalBranch.company_id).order_by(PortalBranch.id.asc()))
    return [row[0] for row in r.all()]


async def fetch_staff_directory(
    db: AsyncSession,
    include_fired: bool = False,
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> list[dict[str, Any]]:
    if force_allowed:
        allowed = allowed_company_ids or []
    elif allowed_company_ids is not None:
        allowed = allowed_company_ids
    else:
        allowed = await branch_company_ids(db)
    stmt = (
        select(
            Company.id.label('company_id'),
            Company.title.label('company_title'),
            Staff.id.label('staff_id'),
            Staff.name.label('staff_name'),
            Staff.position,
            Staff.user_id,
            Staff.fired,
            Staff.bookable,
        )
        .select_from(Staff)
        .join(Company, Company.id == Staff.company_id)
        .order_by(Company.title.asc(), Staff.name.asc(), Staff.id.asc())
    )
    if allowed is not None:
        stmt = stmt.where(Company.id.in_(allowed))
    if not include_fired:
        stmt = stmt.where(Staff.fired == 0)

    rows = (await db.execute(stmt)).all()
    return [
        {
            'company_id': row.company_id,
            'company_title': row.company_title,
            'staff_id': row.staff_id,
            'staff_name': row.staff_name,
            'position': row.position,
            'user_id': row.user_id,
            'fired': int(row.fired or 0),
            'working': int((row.fired or 0) == 0),
            'bookable': int(bool(row.bookable)),
        }
        for row in rows
        if (
            not _is_waitlist_staff_name(row.staff_name)
            and not _is_admin_placeholder_staff_name(row.staff_name)
        )
    ]


async def fetch_branches(
    db: AsyncSession,
    allowed_company_ids: Optional[list[int]] = None,
    force_allowed: bool = False,
) -> list[dict[str, Any]]:
    if force_allowed:
        allowed = allowed_company_ids or []
    elif allowed_company_ids is not None:
        allowed = allowed_company_ids
    else:
        allowed = await branch_company_ids(db)
    stmt = select(Company).order_by(Company.id.asc())
    if allowed is not None:
        stmt = stmt.where(Company.id.in_(allowed))
    rows = (await db.execute(stmt)).scalars().all()
    return [{'id': c.id, 'title': c.title, 'group_id': c.group_id} for c in rows]
