"""Report usage analytics: capture of report openings and the platform-admin report built from them.

Capture is fire-and-forget. The write runs in its own task on its own session, so a slow or failing
insert can neither delay nor break the report that triggered it.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterator

from fastapi import HTTPException
from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.ext.asyncio import AsyncSession

import dashboard_service
from auth_scope import AccessContext
from dashboard_reports import REPORT_REGISTRY, REPORT_USAGE_ID
from database import get_async_session_factory
from models import ReportUsageEvent
from report_payload import NUMBER_FORMAT, PERCENT_FORMAT, ReportRequest, card, table

logger = logging.getLogger('yclients.report_usage')

WRITE_TIMEOUT_SECONDS = 5
# Each pending write waits for a pooled connection next to the requests it records. A slow database makes them
# pile up for the whole timeout, so past this many in flight further events are dropped instead of queued.
MAX_PENDING_WRITES = 50
UNUSED_WINDOW_DAYS = 60
# The platform operator opens tenants to support them; their clicks say nothing about how the tenant works.
EXCLUDED_ROLES = ('platform_admin',)

_pending: set[asyncio.Task] = set()


@dataclass(frozen=True)
class ReportUsage:
    """One row of `report_usage_events`."""

    portal_account_id: int
    user_id: int
    role: str
    report_id: str
    requested_report_id: str | None
    created_at: datetime
    usage_day: date
    duration_ms: int
    status_code: int
    source_status: str | None
    compare_used: bool
    staff_filter: bool
    company_filter: bool
    granularity: str | None
    period_days: int
    period_preset: str | None


# --- Capture ---


async def _insert(fields: ReportUsage) -> None:
    async with get_async_session_factory()() as session:
        session.add(ReportUsageEvent(**asdict(fields)))
        await session.commit()


async def _write(fields: ReportUsage) -> None:
    try:
        await asyncio.wait_for(_insert(fields), WRITE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - analytics must never surface
        # Class name only: driver errors embed the row's parameters.
        logger.warning('report usage event not recorded: %s', type(exc).__name__)


def record_report_usage(fields: ReportUsage) -> None:
    """Schedule the insert of one usage event. Never raises and never waits for the database."""
    if len(_pending) >= MAX_PENDING_WRITES:
        logger.warning('report usage event dropped: %d writes already pending', len(_pending))
        return
    try:
        task = asyncio.get_running_loop().create_task(_write(fields))
    except Exception as exc:  # noqa: BLE001
        logger.warning('report usage event not scheduled: %s', type(exc).__name__)
        return
    # The loop keeps only weak references to tasks; without this set a pending write can be collected mid-flight.
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for scheduled writes; for tests."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


class UsageProbe:
    """Filled in by the route while the report runs."""

    def __init__(self) -> None:
        self.source_status: str | None = None


def _is_tracked(ctx: AccessContext, *, is_demo: bool, report_id: str) -> bool:
    return not (
        is_demo
        or ctx.user_id is None
        or ctx.portal_account_id is None
        or ctx.role is None
        or report_id == REPORT_USAGE_ID
    )


@contextmanager
def track_report_usage(
    ctx: AccessContext,
    *,
    is_demo: bool,
    report_id: str,
    requested_report_id: str,
    start: date,
    end: date,
    granularity: str,
    period_preset: str | None,
    compare_used: bool,
    staff_filter: bool,
    company_filter: bool,
) -> Iterator[UsageProbe]:
    """Record the outcome of the wrapped report computation.

    Only HTTPException and ordinary failures are outcomes; a cancelled request (client gone) is not recorded.
    """
    probe = UsageProbe()
    if not _is_tracked(ctx, is_demo=is_demo, report_id=report_id):
        yield probe
        return
    began = asyncio.get_running_loop().time()
    moment = dashboard_service.factual_now()
    status_code: int | None = None
    try:
        yield probe
    except HTTPException as exc:
        status_code = exc.status_code
        raise
    except Exception:
        status_code = 500
        raise
    else:
        status_code = 200
    finally:
        if status_code is not None:
            try:
                record_report_usage(
                    ReportUsage(
                        portal_account_id=ctx.portal_account_id,
                        user_id=ctx.user_id,
                        role=ctx.role,
                        report_id=report_id,
                        requested_report_id=requested_report_id if requested_report_id != report_id else None,
                        created_at=moment,
                        usage_day=dashboard_service.factual_branch_date(moment),
                        duration_ms=max(0, round((asyncio.get_running_loop().time() - began) * 1000)),
                        status_code=status_code,
                        source_status=probe.source_status,
                        compare_used=compare_used and REPORT_REGISTRY[report_id].compare,
                        staff_filter=staff_filter,
                        company_filter=company_filter,
                        granularity=granularity if REPORT_REGISTRY[report_id].granularity else None,
                        period_days=(end - start).days + 1,
                        period_preset=period_preset,
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning('report usage event not built: %s', type(exc).__name__)


# --- Read side ---


async def build_report_usage(req: ReportRequest) -> dict[str, Any]:
    """Registry entry point. The report needs the tenant, which a `ReportRequest` does not carry,
    so the route calls `fetch_report_usage` directly and this must never run in its place."""
    raise RuntimeError('report_usage is built by fetch_report_usage with an explicit tenant')


def _catalog() -> list[tuple[str, str]]:
    return [(definition.id, definition.title) for definition in REPORT_REGISTRY.values() if definition.id != REPORT_USAGE_ID]


async def fetch_report_usage(
    db: AsyncSession,
    portal_account_id: int | None,
    start: date,
    end: date,
    granularity: str = 'day',
) -> dict[str, Any]:
    """Usage of the tenant's reports for usage days in [start, end]."""
    if portal_account_id is None:
        raise ValueError('report usage needs a tenant')
    event = ReportUsageEvent
    tenant = and_(event.portal_account_id == portal_account_id, event.role.not_in(EXCLUDED_ROLES))
    in_period = and_(tenant, event.usage_day >= start, event.usage_day <= end)

    totals = {
        row.report_id: row
        for row in (
            await db.execute(
                select(
                    event.report_id,
                    func.count().label('calls'),
                    func.count(distinct(event.user_id)).label('users'),
                    func.sum(case((event.compare_used, 1), else_=0)).label('compared'),
                    func.sum(case((event.status_code >= 500, 1), else_=0)).label('errors'),
                )
                .where(in_period)
                .group_by(event.report_id)
            )
        ).all()
    }

    user_days_source = select(event.report_id, event.user_id, event.usage_day).where(in_period).distinct().subquery()
    user_days = {
        row.report_id: row.user_days
        for row in (
            await db.execute(
                select(user_days_source.c.report_id, func.count().label('user_days')).group_by(user_days_source.c.report_id)
            )
        ).all()
    }

    # Median by window functions: the two middle rows of each report's sorted durations satisfy n <= 2*rank <= n+2
    # (one row for an odd count, two for an even one), and SQLite has no percentile_cont.
    ranked = (
        select(
            event.report_id,
            event.duration_ms,
            func.row_number().over(partition_by=event.report_id, order_by=(event.duration_ms, event.id)).label('rank'),
            func.count().over(partition_by=event.report_id).label('n'),
        )
        .where(in_period, event.status_code == 200)
        .subquery()
    )
    medians = {
        row.report_id: float(row.median)
        for row in (
            await db.execute(
                select(ranked.c.report_id, func.avg(ranked.c.duration_ms).label('median'))
                .where(and_(2 * ranked.c.rank >= ranked.c.n, 2 * ranked.c.rank <= ranked.c.n + 2))
                .group_by(ranked.c.report_id)
            )
        ).all()
    }

    last_used = {
        row.report_id: row.last_day
        for row in (
            await db.execute(
                select(event.report_id, func.max(event.usage_day).label('last_day'))
                .where(tenant, event.usage_day <= end)
                .group_by(event.report_id)
            )
        ).all()
    }
    people = (await db.execute(select(func.count(distinct(event.user_id))).where(in_period))).scalar_one()
    tracking_since = (
        await db.execute(select(func.min(event.usage_day)).where(event.portal_account_id == portal_account_id))
    ).scalar_one()

    catalog = _catalog()
    rows = []
    for report_id, title in catalog:
        total = totals.get(report_id)
        calls = int(total.calls) if total else 0
        rows.append(
            {
                'title': title,
                'user_days': int(user_days.get(report_id, 0)),
                'users': int(total.users) if total else 0,
                'calls': calls,
                'compare_share': round(100 * int(total.compared) / calls, 1) if calls else None,
                'median_ms': round(medians[report_id]) if report_id in medians else None,
                'errors': int(total.errors) if total else 0,
                'last_used': last_used[report_id].isoformat() if report_id in last_used else None,
            }
        )
    # The unique title closes the ordering, so equal call counts keep a stable order.
    rows.sort(key=lambda row: (-row['calls'], row['title']))

    unused_since = end - timedelta(days=UNUSED_WINDOW_DAYS - 1)
    unused = [
        {'title': title, 'last_used': last_used[report_id].isoformat() if report_id in last_used else None}
        for report_id, title in catalog
        if report_id not in last_used or last_used[report_id] < unused_since
    ]
    unused.sort(key=lambda row: (row['last_used'] is not None, row['last_used'] or '', row['title']))

    base = {
        'report_id': REPORT_USAGE_ID,
        'title': REPORT_REGISTRY[REPORT_USAGE_ID].title,
        'period': {'start': start.isoformat(), 'end': end.isoformat(), 'granularity': granularity},
        'source_status': 'ready',
        'missing_sources': [],
        'cards': [
            card('Отчётов открывали', sum(1 for row in rows if row['calls']), NUMBER_FORMAT),
            card('Пользователей', int(people), NUMBER_FORMAT),
            card('Вызовов', sum(row['calls'] for row in rows), NUMBER_FORMAT),
        ],
        'charts': [],
        'tables': [
            table(
                'report_usage',
                'Использование отчётов',
                [
                    ('title', 'Отчёт', 'text'),
                    ('user_days', 'Дней-пользователей', NUMBER_FORMAT),
                    ('users', 'Уникальных пользователей', NUMBER_FORMAT),
                    ('calls', 'Вызовов', NUMBER_FORMAT),
                    ('compare_share', 'Доля со сравнением', PERCENT_FORMAT),
                    ('median_ms', 'Медиана времени ответа, мс', NUMBER_FORMAT),
                    ('errors', 'Ошибок', NUMBER_FORMAT),
                    ('last_used', 'Последний запуск', 'date'),
                ],
                rows,
                row_key='title',
            ),
            table(
                'report_usage_unused',
                f'Не открывали {UNUSED_WINDOW_DAYS} дней',
                [('title', 'Отчёт', 'text'), ('last_used', 'Последний запуск', 'date')],
                unused,
                hide_when_empty=True,
                row_key='title',
            ),
        ],
        'notes': [
            {
                'kind': 'info',
                'title': 'Как считается',
                'text': (
                    'День-пользователь — один человек, открывший отчёт в течение дня. Время ответа — медиана '
                    'по успешным запросам; ошибки — ответы сервера с кодом 5xx. Действия администраторов '
                    'платформы не учитываются.'
                ),
            }
        ],
        'raw': {'tracking_since': tracking_since.isoformat() if tracking_since else None},
    }
    if tracking_since is None:
        base['notes'].append(
            {'kind': 'warning', 'title': 'Данных пока нет', 'text': 'Учёт открытий отчётов ещё не накопил ни одной записи.'}
        )
    elif min(start, unused_since) < tracking_since:
        # The «not opened» list reads UNUSED_WINDOW_DAYS back from the period end, so it can reach before
        # the log even when the period itself does not.
        base['notes'].append(
            {
                'kind': 'warning',
                'title': 'Учёт начат позже начала периода',
                'text': (
                    f'Открытия отчётов записываются с {tracking_since.strftime("%d.%m.%Y")}: за более ранние дни '
                    f'периода и окна «Не открывали {UNUSED_WINDOW_DAYS} дней» нулевые значения означают '
                    'отсутствие учёта, а не отсутствие интереса.'
                ),
            }
        )
    return base
