"""Set or clear the reporting window of branches.

Facts dated outside a branch's reporting window are excluded from every dashboard
metric. The lower bound trims a branch that carries upstream records from before it
opened (test bookings, a previous location on the same YClients id). The upper bound
trims a branch that stopped belonging to the tenant: its history stays readable, its
facts after that day stop counting.

Ids are internal ``companies.id``, which equals the YClients id only in the
legacy single-tenant layout; on a tenant-scoped install the YClients id lives in
``companies.external_id``. Always run ``--list`` first and take the id from the
first column.

Run: ``python -m scripts.set_reporting_window --list``
Then: ``python -m scripts.set_reporting_window --start <id>=2022-05-01``
Or:   ``python -m scripts.set_reporting_window --end <id>=2026-08-31``
Clear one with ``--start <id>=none`` / ``--end <id>=none``.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import build_async_database_url
from models import Company

CLEAR_KEYWORDS = ('none', 'null', 'clear')
DEMO_SOURCE_TYPE = 'demo'
BOUNDS = ('start', 'end')


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Set the reporting window of branches')
    parser.add_argument(
        '--start',
        nargs='+',
        default=[],
        metavar='COMPANY_ID=YYYY-MM-DD',
        help=f'First reportable day, or one of {CLEAR_KEYWORDS} to clear it',
    )
    parser.add_argument(
        '--end',
        nargs='+',
        default=[],
        metavar='COMPANY_ID=YYYY-MM-DD',
        help=f'Last reportable day, inclusive, or one of {CLEAR_KEYWORDS} to clear it',
    )
    parser.add_argument('--list', action='store_true', help='Print current values and exit')
    parser.add_argument('--dry-run', action='store_true', help='Report changes without writing')
    args = parser.parse_args()
    if not args.start and not args.end and not args.list:
        parser.error('pass --start and/or --end assignments, or --list')
    if (args.start or args.end) and args.list:
        parser.error('--list prints current values and exits; do not pass assignments with it')
    return args


def parse_assignments(raw: list[str], bound: str = 'start') -> dict[int, date | None]:
    """Parse ``COMPANY_ID=YYYY-MM-DD`` pairs for one bound of the window."""
    if bound not in BOUNDS:
        raise SystemExit(f'Unknown bound {bound!r}')
    parsed: dict[int, date | None] = {}
    for item in raw:
        company_id, separator, value = item.partition('=')
        if not separator:
            raise SystemExit(f'Expected COMPANY_ID=YYYY-MM-DD, got {item!r}')
        try:
            key = int(company_id)
        except ValueError:
            raise SystemExit(f'Company id must be an integer, got {company_id!r}') from None
        if key in parsed:
            raise SystemExit(f'Company {key} given more than once')
        if value.strip().lower() in CLEAR_KEYWORDS:
            parsed[key] = None
            continue
        try:
            day = date.fromisoformat(value.strip())
        except ValueError:
            raise SystemExit(f'Expected an ISO date for company {key}, got {value!r}') from None
        # A typo in the year silently empties the branch's reports instead of erroring.
        if bound == 'start' and day > date.today():
            raise SystemExit(f'Reporting start for company {key} is in the future: {day}')
        parsed[key] = day
    return parsed


async def _print_current(db: AsyncSession) -> None:
    companies = (
        await db.execute(
            select(Company)
            .where(Company.source_type != DEMO_SOURCE_TYPE)
            .order_by(Company.id.asc())
        )
    ).scalars().all()
    print(f'{"id":>10}  {"external_id":>11}  {"tenant":>6}  {"start":<12}  {"end":<12}  title')
    for company in companies:
        start = company.reporting_start_date.isoformat() if company.reporting_start_date else '-'
        end = company.reporting_end_date.isoformat() if company.reporting_end_date else '-'
        external_id = company.external_id if company.external_id is not None else '-'
        tenant = company.portal_account_id if company.portal_account_id is not None else '-'
        print(
            f'{company.id:>10}  {external_id:>11}  {tenant:>6}  '
            f'{start:<12}  {end:<12}  {company.title}'
        )


def _resulting_window(
    company: Company,
    assignments: dict[str, dict[int, date | None]],
) -> tuple[date | None, date | None]:
    company_id = int(company.id)
    start = company.reporting_start_date
    end = company.reporting_end_date
    if company_id in assignments['start']:
        start = assignments['start'][company_id]
    if company_id in assignments['end']:
        end = assignments['end'][company_id]
    return start, end


async def main() -> int:
    args = parse_args()
    assignments = {bound: parse_assignments(getattr(args, bound), bound) for bound in BOUNDS}
    touched = sorted(set(assignments['start']) | set(assignments['end']))

    engine = create_async_engine(build_async_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    exit_code = 0

    async with session_factory() as db:
        if args.list:
            await _print_current(db)
            await engine.dispose()
            return 0

        companies = (
            await db.execute(select(Company).where(Company.id.in_(touched)))
        ).scalars().all()
        by_id = {int(company.id): company for company in companies}
        for company_id in sorted(set(touched) - set(by_id)):
            print(f'company id={company_id} not found', file=sys.stderr)
            exit_code = 1

        for company_id in touched:
            company = by_id.get(company_id)
            if company is None:
                continue
            if company.source_type == DEMO_SOURCE_TYPE:
                print(f'company id={company_id} is a demo branch and stays read-only', file=sys.stderr)
                exit_code = 1
                continue
            start, end = _resulting_window(company, assignments)
            # An inverted window reports nothing at all, which is never what an operator
            # means — a mistyped year is the likely cause and it would be silent.
            if start is not None and end is not None and end < start:
                print(
                    f'company id={company_id}: reporting end {end} is before start {start}',
                    file=sys.stderr,
                )
                exit_code = 1
                continue
            for bound, column in (('start', 'reporting_start_date'), ('end', 'reporting_end_date')):
                if company_id not in assignments[bound]:
                    continue
                value = assignments[bound][company_id]
                before = getattr(company, column)
                if before == value:
                    print(f'{company_id} ({company.title}) {bound}: already {value or "unset"}')
                    continue
                setattr(company, column, value)
                print(
                    f'{company_id} ({company.title}) {bound}: '
                    f'{before or "unset"} -> {value or "unset"}'
                )

        if args.dry_run:
            await db.rollback()
            print('Dry run — nothing written.')
        else:
            await db.commit()

    await engine.dispose()
    return exit_code


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
