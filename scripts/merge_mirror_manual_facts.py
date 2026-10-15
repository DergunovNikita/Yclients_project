"""Move manual facts entered on a portal account's mirror `staff` row to the person's real YClients row.

Mirror rows of owners, branch admins and managers are not staff (`models.PORTAL_MIRROR_POSITIONS`),
so a manual value left on one stops counting. This adds each non-zero value of the mirror to the
same month and metric of the real row in the same branch; the mirror rows themselves stay untouched.

Run: ``python -m scripts.merge_mirror_manual_facts --map <mirror_staff_id>:<real_staff_id> [--map ...]``
Prints a plan and writes nothing; add ``--apply`` to write. One transaction, every moved value leaves a
``manual_fact.merged_from_mirror`` audit event, and a source row already merged is refused (run once).
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from clock import utcnow
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_async_database_url
from models import PORTAL_MIRROR_POSITIONS, ManualFactMetric, PortalAuditEvent, Staff
from portal_audit import log_portal_audit

AUDIT_ACTION = 'manual_fact.merged_from_mirror'
AUDIT_TARGET_TYPE = 'manual_fact_metric'


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
        if source.position not in PORTAL_MIRROR_POSITIONS:
            raise MergeRefused(f'Staff {source_id} is not a portal mirror row (position {source.position!r})')
        if target.position in PORTAL_MIRROR_POSITIONS:
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
    seen_targets: set[tuple[int, str, date]] = set()
    for source in sources:
        target_id = mappings[int(source.staff_id)]
        target_key = (target_id, source.metric_code, source.period_start)
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
            ManualFactMetric.period_start == source.period_start,
        )
        if lock:
            target_stmt = target_stmt.with_for_update()
        targets = (await db.execute(target_stmt)).scalars().all()
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


async def apply_moves(db: AsyncSession, mappings: dict[int, int]) -> list[Move]:
    """Add every planned value to its target row in the caller's transaction; the caller commits."""
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
            actor_user_id=None,
            portal_account_id=None,
            action=AUDIT_ACTION,
            target_type=AUDIT_TARGET_TYPE,
            target_id=str(move.source_row_id),
            metadata={
                'company_id': move.company_id,
                'metric_code': move.metric_code,
                'period_start': move.period_start.isoformat(),
                'source_staff_id': move.source_staff_id,
                'target_staff_id': move.target_staff_id,
                'value': move.value,
                'target_before': move.target_before,
                'target_after': move.target_after,
            },
        )
    await db.flush()
    return moves


def format_moves(moves: list[Move]) -> str:
    header = f'{"source row":>10}  {"mirror":>9}  {"target":>9}  {"company":>9}  {"metric":<12}  {"month":<10}  ' \
             f'{"value":>8}  {"target before":>13}  {"target after":>12}'
    lines = [header]
    for move in moves:
        before = '(new row)' if move.target_before is None else f'{move.target_before:g}'
        lines.append(
            f'{move.source_row_id:>10}  {move.source_staff_id:>9}  {move.target_staff_id:>9}  {move.company_id:>9}  '
            f'{move.metric_code:<12}  {move.period_start:%Y-%m}      {move.value:>8g}  {before:>13}  '
            f'{move.target_after:>12g}'
        )
    return '\n'.join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        '--map', action='append', default=[], required=True, metavar='MIRROR_STAFF_ID:REAL_STAFF_ID',
        help='Mirror staff row and the real row of the same person in the same branch; repeatable',
    )
    parser.add_argument('--apply', action='store_true', help='Write the changes (default: print the plan only)')
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        mappings = parse_mappings(args.map)
    except MergeRefused as error:
        print(error, file=sys.stderr)
        return 2
    engine = create_async_engine(build_async_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with session_factory() as db:
            try:
                moves = await (apply_moves if args.apply else plan_moves)(db, mappings)
            except MergeRefused as error:
                await db.rollback()
                print(f'Refused: {error}', file=sys.stderr)
                return 1
            print(format_moves(moves) if moves else 'Nothing to move: no non-zero values on the mirror rows.')
            if args.apply:
                await db.commit()
                print(f'Applied {len(moves)} move(s).')
            else:
                print('Dry run - nothing written. Pass --apply to write.')
    finally:
        await engine.dispose()
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
