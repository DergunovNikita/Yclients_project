"""Backfill historical YClients staff schedules used by administrator KPIs.

By default, every real branch is loaded from its reporting start date. If that
date is not configured, the first stored appointment is used instead.

Run: ``python -m scripts.backfill_staff_schedules``
Limit the range: ``python -m scripts.backfill_staff_schedules --start 2026-01-01 --end 2026-08-31``
Limit branches: ``python -m scripts.backfill_staff_schedules --company-id 123 --company-id 456``
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta

from sqlalchemy import func

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, LOGIN, PARTNER_TOKEN, PASSWORD
from database import init_database
from models import Appointment, Company
from sync_control import SyncControlService
from sync_pipeline import _build_api_for_credential, sync_staff_schedules
from yclients_credentials import YClientsCredentialValue, load_credentials_for_companies_sync

DEMO_SOURCE_TYPE = 'demo'


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f'Expected YYYY-MM-DD, got {value!r}') from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Backfill historical staff schedules from YClients')
    parser.add_argument('--start', type=_parse_date, help='Override the start date for every branch')
    parser.add_argument('--end', type=_parse_date, default=date.today(), help='End date (default: today)')
    parser.add_argument('--company-id', action='append', type=int, dest='company_ids', help='Internal branch id')
    parser.add_argument('--chunk-days', type=int, default=31, help='Days per YClients request (default: 31)')
    args = parser.parse_args()
    if args.chunk_days < 1 or args.chunk_days > 366:
        parser.error('--chunk-days must be between 1 and 366')
    if args.start and args.start > args.end:
        parser.error('--start must not be after --end')
    return args


def _fallback_credential(company_ids: list[int]) -> YClientsCredentialValue | None:
    if not all((PARTNER_TOKEN.strip(), LOGIN.strip(), PASSWORD.strip())):
        return None
    return YClientsCredentialValue(
        id=None,
        title='Environment credentials',
        partner_token=PARTNER_TOKEN,
        login=LOGIN,
        password=PASSWORD,
        company_ids=tuple(company_ids),
    )


def _company_start(company: Company, first_appointment: date | None, override: date | None) -> date:
    if override is not None:
        first_reportable_date = override
    elif company.reporting_start_date is not None:
        first_reportable_date = company.reporting_start_date
    else:
        first_reportable_date = first_appointment or date.today()
    return first_reportable_date - timedelta(days=1)


def _windows(start: date, end: date, chunk_days: int):
    cursor = end
    while cursor >= start:
        window_start = max(start, cursor - timedelta(days=chunk_days - 1))
        yield window_start, cursor
        cursor = window_start - timedelta(days=1)


def main() -> int:
    args = parse_args()
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    db = database.get_db()
    control = SyncControlService()
    lock_acquired = False
    try:
        if not control.acquire_lock(db):
            print('Another synchronization is already running; backfill was not started.')
            return 1
        lock_acquired = True

        query = db.query(Company).filter(Company.source_type != DEMO_SOURCE_TYPE)
        if args.company_ids:
            query = query.filter(Company.id.in_(sorted(set(args.company_ids))))
        companies = query.order_by(Company.id.asc()).all()
        if not companies:
            print('No matching real branches found.')
            return 1

        company_ids = [int(company.id) for company in companies]
        credential_by_company = load_credentials_for_companies_sync(db, company_ids)
        fallback = _fallback_credential(company_ids)
        first_appointments = dict(
            db.query(Appointment.company_id, func.min(Appointment.date))
            .filter(Appointment.company_id.in_(company_ids))
            .group_by(Appointment.company_id)
            .all()
        )

        api_by_credential: dict[tuple[int | None, str, str], object] = {}
        failed = 0
        completed = 0
        for company in companies:
            credential = credential_by_company.get(int(company.id)) or fallback
            if credential is None:
                print(f'[{company.id}] skipped: no active YClients credentials')
                failed += 1
                continue

            cache_key = (credential.id, credential.partner_token, credential.login)
            if cache_key not in api_by_credential:
                api = _build_api_for_credential(credential)
                if api is None:
                    print(f'[{company.id}] skipped: YClients authentication failed')
                    failed += 1
                    continue
                api_by_credential[cache_key] = api
            api = api_by_credential[cache_key]

            start = _company_start(company, first_appointments.get(int(company.id)), args.start)
            if start > args.end:
                print(f'[{company.id}] skipped: start {start} is after end {args.end}')
                continue

            external_id = int(company.external_id or company.id)
            label = (company.title or '').strip() or str(company.id)
            print(f'\n[{company.id}] {label}: {start} .. {args.end}')
            company_ok = True
            for window_start, window_end in _windows(start, args.end, args.chunk_days):
                print(f'  window {window_start} .. {window_end}')
                if not sync_staff_schedules(
                    api,
                    db,
                    str(external_id),
                    start_date=window_start.isoformat(),
                    end_date=window_end.isoformat(),
                    db_company_id=int(company.id),
                ):
                    company_ok = False
                    break
            if company_ok:
                completed += 1
            else:
                failed += 1

        print(f'\nDone. Completed branches: {completed}; failed: {failed}.')
        return 1 if failed else 0
    finally:
        if lock_acquired:
            control.release_lock(db)
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
