"""Move manual facts entered on a portal account's mirror `staff` row to the person's real YClients row.

Mirror rows (`source_type = 'portal'`; those of owners, branch admins and managers are not staff at all,
`models.PORTAL_MIRROR_POSITIONS`) stop counting once the account is tied to the real row, so a manual value left on
one is lost. This adds each non-zero value of the mirror to the same period (a day, or a legacy whole month)
and metric of the real row in the same branch; the mirror rows themselves stay untouched.
The admin «link to YClients» action (`portal_staff_link`) uses the same `apply_moves` (`manual_fact_merge`),
and there the mirror's values are deleted once moved.

Run: ``python -m scripts.merge_mirror_manual_facts --map <mirror_staff_id>:<real_staff_id> [--map ...]``
Prints a plan and writes nothing; add ``--apply`` to write. One transaction, every moved value leaves a
``manual_fact.merged_from_mirror`` audit event, and a source row already merged is refused (run once).
"""
from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_async_database_url
from manual_fact_merge import MergeRefused, Move, apply_moves, parse_mappings, plan_moves


def _period_label(move: Move) -> str:
    return f'{move.period_start:%Y-%m-%d}' if move.period_start == move.period_end else f'{move.period_start:%Y-%m}'


def format_moves(moves: list[Move]) -> str:
    header = f'{"source row":>10}  {"mirror":>9}  {"target":>9}  {"company":>9}  {"metric":<12}  {"period":<10}  ' \
             f'{"value":>8}  {"target before":>13}  {"target after":>12}'
    lines = [header]
    for move in moves:
        before = '(new row)' if move.target_before is None else f'{move.target_before:g}'
        lines.append(
            f'{move.source_row_id:>10}  {move.source_staff_id:>9}  {move.target_staff_id:>9}  {move.company_id:>9}  '
            f'{move.metric_code:<12}  {_period_label(move):<10}  {move.value:>8g}  {before:>13}  '
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
