"""Move manual facts entered on a portal account's mirror `staff` row to the person's real YClients row.

Mirror rows (`source_type = 'portal'`; those of owners, branch admins and managers are not staff at all,
`models.PORTAL_MIRROR_POSITIONS`) stop counting once the account is tied to the real row, so a manual value left on
one is lost. `apply_moves` adds each non-zero value of the mirror to the same period (a day, or a legacy whole
month) and metric of the real row in the same branch. Used by the admin «link to YClients» action
(`portal_staff_link`, which then deletes the mirror's values) and by `scripts/merge_mirror_manual_facts.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from clock import utcnow
from models import PORTAL_MIRROR_POSITIONS, ManualFactMetric, PortalAuditEvent, Staff
from portal_audit import log_portal_audit
from portal_staff_sync import is_mirror_row

AUDIT_ACTION = 'manual_fact.merged_from_mirror'
AUDIT_TARGET_TYPE = 'manual_fact_metric'


def _is_mirror(row: Staff) -> bool:
    return is_mirror_row(row) or row.position in PORTAL_MIRROR_POSITIONS


class MergeRefused(Exception):
    """The mapping or the data does not allow a safe merge; nothing is written."""


@dataclass(frozen=True)
class Move:
    source_row_id: int
    source_staff_id: int
    target_staff_id: int
    company_id: int
    metric_code: str
    period_start: date
    period_end: date
    value: float
    target_row_id: int | None
    target_before: float | None

    @property
    def target_after(self) -> float:
        return (self.target_before or 0.0) + self.value


def _is_daily(row: ManualFactMetric) -> bool:
    return row.period_start == row.period_end


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def parse_mappings(raw: list[str]) -> dict[int, int]:
    """Parse ``MIRROR:REAL`` pairs; a staff id may appear once on each side."""
    mappings: dict[int, int] = {}
    for item in raw:
        source, separator, target = item.partition(':')
        try:
            source_id, target_id = int(source), int(target)
        except ValueError:
            separator = ''
        if not separator:
            raise MergeRefused(f'Expected MIRROR_STAFF_ID:REAL_STAFF_ID, got {item!r}')
        if source_id == target_id:
            raise MergeRefused(f'Staff {source_id} cannot be merged into itself')
        if source_id in mappings:
            raise MergeRefused(f'Staff {source_id} is mapped more than once')
        mappings[source_id] = target_id
    if set(mappings) & set(mappings.values()):
        raise MergeRefused('A staff id cannot be both a mirror and a target')
    if len(set(mappings.values())) != len(mappings):
        raise MergeRefused('Two mirror rows cannot be merged into the same target')
    return mappings


async def _validated_staff(db: AsyncSession, mappings: dict[int, int]) -> dict[int, Staff]:
    ids = sorted(set(mappings) | set(mappings.values()))
    rows = {int(row.id): row for row in (await db.execute(select(Staff).where(Staff.id.in_(ids)))).scalars()}
    for source_id, target_id in sorted(mappings.items()):
        source, target = rows.get(source_id), rows.get(target_id)
        if source is None or target is None:
            raise MergeRefused(f'Staff {source_id if source is None else target_id} not found')
        if not _is_mirror(source):
            raise MergeRefused(f'Staff {source_id} is not a portal mirror row (position {source.position!r})')
        if _is_mirror(target):
            raise MergeRefused(f'Staff {target_id} is itself a portal mirror row (position {target.position!r})')
        if int(source.company_id) != int(target.company_id):
            raise MergeRefused(
                f'Staff {source_id} (company {source.company_id}) and {target_id} (company {target.company_id}) '
                'are in different branches'
            )
    return rows


async def plan_moves(db: AsyncSession, mappings: dict[int, int], *, lock: bool = False) -> list[Move]:
    """Non-zero values of the mirror rows with the row each one is added to. `lock` takes `FOR UPDATE`."""
    await _validated_staff(db, mappings)
    source_stmt = (
        select(ManualFactMetric)
        .where(ManualFactMetric.staff_id.in_(sorted(mappings)), ManualFactMetric.value != 0)
        .order_by(ManualFactMetric.staff_id, ManualFactMetric.period_start, ManualFactMetric.metric_code,
                  ManualFactMetric.id)
    )
    if lock:
        source_stmt = source_stmt.with_for_update()
    sources = (await db.execute(source_stmt)).scalars().all()

    already_merged = (
        await db.execute(
            select(PortalAuditEvent.target_id).where(
                PortalAuditEvent.action == AUDIT_ACTION,
                PortalAuditEvent.target_id.in_([str(source.id) for source in sources]),
            )
        )
    ).scalars().all()
    if already_merged:
        raise MergeRefused(f'Source rows already merged: {", ".join(sorted(already_merged, key=int))}')

    moves: list[Move] = []
    seen_targets: set[tuple[int, str, date, date | None]] = set()
    source_kinds: dict[tuple[int, str, date], bool] = {}
    for source in sources:
        target_id = mappings[int(source.staff_id)]
        daily = _is_daily(source)
        month_start = source.period_start.replace(day=1)
        label = f'{source.metric_code} {month_start:%Y-%m}'
        if source_kinds.setdefault((target_id, source.metric_code, month_start), daily) != daily:
            raise MergeRefused(
                f'Staff {source.staff_id} has both a whole-month row and daily rows for {label}; resolve them first'
            )
        # A legacy month is one period whatever its end; a day is exactly its own date.
        period_key = (source.period_start, source.period_end) if daily else (source.period_start, None)
        target_key = (target_id, source.metric_code, *period_key)
        if target_key in seen_targets:
            # A legacy month split over several rows: the second add would be computed from the same stale target.
            raise MergeRefused(
                f'Staff {source.staff_id} has several rows for {source.metric_code} {source.period_start}; '
                'resolve them first'
            )
        seen_targets.add(target_key)
        target_stmt = select(ManualFactMetric).where(
            ManualFactMetric.company_id == source.company_id,
            ManualFactMetric.staff_id == target_id,
            ManualFactMetric.metric_code == source.metric_code,
            ManualFactMetric.period_start >= month_start,
            ManualFactMetric.period_start < _next_month(month_start),
        )
        if lock:
            target_stmt = target_stmt.with_for_update()
        month_rows = (await db.execute(target_stmt)).scalars().all()
        # A zero row adds nothing, so it cannot double count: the editor ignores a zero legacy row too.
        if any(_is_daily(row) != daily and row.value for row in month_rows):
            raise MergeRefused(
                f'Staff {target_id} holds {"whole-month" if daily else "daily"} rows for {label}, the mirror '
                f'{"daily" if daily else "whole-month"} ones: a month must not mix both; resolve them first'
            )
        targets = [row for row in month_rows if _is_daily(row) == daily and row.period_start == source.period_start
                   and (not daily or row.period_end == source.period_end)]
        if len(targets) > 1:
            raise MergeRefused(
                f'Staff {target_id} has {len(targets)} rows for {source.metric_code} {source.period_start}; '
                'resolve them first'
            )
        target = targets[0] if targets else None
        moves.append(Move(
            source_row_id=int(source.id),
            source_staff_id=int(source.staff_id),
            target_staff_id=target_id,
            company_id=int(source.company_id),
            metric_code=source.metric_code,
            period_start=source.period_start,
            period_end=source.period_end,
            value=float(source.value),
            target_row_id=int(target.id) if target is not None else None,
            target_before=float(target.value) if target is not None else None,
        ))
    return moves


async def apply_moves(
    db: AsyncSession,
    mappings: dict[int, int],
    *,
    actor_user_id: int | None = None,
    portal_account_id: int | None = None,
    delete_sources: bool = False,
) -> list[Move]:
    """Add every planned value to its target row in the caller's transaction; the caller commits.

    `delete_sources` removes every manual value of the mirror rows afterwards (zero ones too): the caller retires the
    mirror, and a value left on it would be counted, or shown in the editor, as a person who does not exist.
    """
    moves = await plan_moves(db, mappings, lock=True)
    now = utcnow()
    for move in moves:
        if move.target_row_id is None:
            db.add(ManualFactMetric(
                period_start=move.period_start,
                period_end=move.period_end,
                company_id=move.company_id,
                staff_id=move.target_staff_id,
                metric_code=move.metric_code,
                value=move.target_after,
                source='merge_mirror',
                updated_at=now,
                updated_by_user_id=None,
            ))
        else:
            target = await db.get(ManualFactMetric, move.target_row_id)
            target.value = move.target_after
            target.updated_at = now
            target.updated_by_user_id = None
        await log_portal_audit(
            db,
            actor_user_id=actor_user_id,
            portal_account_id=portal_account_id,
            action=AUDIT_ACTION,
            target_type=AUDIT_TARGET_TYPE,
            target_id=str(move.source_row_id),
            metadata={
                'company_id': move.company_id,
                'metric_code': move.metric_code,
                'period_start': move.period_start.isoformat(),
                'period_end': move.period_end.isoformat(),
                'source_staff_id': move.source_staff_id,
                'target_staff_id': move.target_staff_id,
                'value': move.value,
                'target_before': move.target_before,
                'target_after': move.target_after,
            },
        )
    if delete_sources:
        await db.execute(delete(ManualFactMetric).where(ManualFactMetric.staff_id.in_(sorted(mappings))))
    await db.flush()
    return moves
