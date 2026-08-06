"""Set or clear the reporting start date of branches.

Facts dated before a branch's reporting start are excluded from every dashboard
metric, so this is how a branch that carries upstream records from before it
opened (test bookings, a previous location on the same YClients id) gets its
history trimmed to the real opening.

Ids are internal ``companies.id``, which equals the YClients id only in the
legacy single-tenant layout; on a tenant-scoped install the YClients id lives in
``companies.external_id``. Always run ``--list`` first and take the id from the
first column.

Run: ``python -m scripts.set_reporting_start --list``
Then: ``python -m scripts.set_reporting_start <id>=2022-05-01 <id>=2020-08-08``
Clear one with ``python -m scripts.set_reporting_start <id>=none``.
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Set the reporting start date of branches')
    parser.add_argument(
        'assignments',
        nargs='*',
        metavar='COMPANY_ID=YYYY-MM-DD',
        help=f'Company id and date, or one of {CLEAR_KEYWORDS} to clear it',
    )
    parser.add_argument('--list', action='store_true', help='Print current values and exit')
    parser.add_argument('--dry-run', action='store_true', help='Report changes without writing')
    args = parser.parse_args()
    if not args.assignments and not args.list:
        parser.error('pass at least one COMPANY_ID=YYYY-MM-DD, or --list')
    if args.assignments and args.list:
        parser.error('--list prints current values and exits; do not pass assignments with it')
    return args


def parse_assignments(raw: list[str]) -> dict[int, date | None]:
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
            start = date.fromisoformat(value.strip())
        except ValueError:
            raise SystemExit(f'Expected an ISO date for company {key}, got {value!r}') from None
        # A typo in the year silently empties the branch's reports instead of erroring.
        if start > date.today():
            raise SystemExit(f'Reporting start for company {key} is in the future: {start}')
        parsed[key] = start
    return parsed


async def _print_current(db: AsyncSession) -> None:
    companies = (
        await db.execute(
            select(Company)
            .where(Company.source_type != DEMO_SOURCE_TYPE)
            .order_by(Company.id.asc())
        )
    ).scalars().all()
    print(f'{"id":>10}  {"external_id":>11}  {"tenant":>6}  {"start":<12}  title')
    for company in companies:
        current = company.reporting_start_date.isoformat() if company.reporting_start_date else '-'
        external_id = company.external_id if company.external_id is not None else '-'
        tenant = company.portal_account_id if company.portal_account_id is not None else '-'
        print(f'{company.id:>10}  {external_id:>11}  {tenant:>6}  {current:<12}  {company.title}')


async def main() -> int:
    args = parse_args()
    assignments = parse_assignments(args.assignments)

    engine = create_async_engine(build_async_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD))
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    exit_code = 0

    async with session_factory() as db:
        if args.list:
            await _print_current(db)
            await engine.dispose()
            return 0

        companies = (
            await db.execute(select(Company).where(Company.id.in_(list(assignments))))
        ).scalars().all()
        by_id = {int(company.id): company for company in companies}
        for company_id in sorted(set(assignments) - set(by_id)):
            print(f'company id={company_id} not found', file=sys.stderr)
            exit_code = 1

        for company_id, value in sorted(assignments.items(), key=lambda item: item[0]):
            company = by_id.get(company_id)
            if company is None:
                continue
            if company.source_type == DEMO_SOURCE_TYPE:
                print(f'company id={company_id} is a demo branch and stays read-only', file=sys.stderr)
                exit_code = 1
                continue
            before = company.reporting_start_date
            if before == value:
                print(f'{company_id} ({company.title}): already {value or "unset"}')
                continue
            company.reporting_start_date = value
            print(
                f'{company_id} ({company.title}): '
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
