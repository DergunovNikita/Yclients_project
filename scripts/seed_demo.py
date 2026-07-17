"""Provision a single shared, read-only demo tenant with synthetic data.

Idempotent: re-running never duplicates companies, branches or the demo user.
The demo tenant is a portal account (``is_demo=True``) whose branches are
synthetic companies marked ``source_type='demo'`` and owned by a single
passwordless demo user (``demo@portal.local``, ``is_demo=True``). Login happens
only via the passwordless demo endpoint, never with the stored password.

Run: ``python -m scripts.seed_demo`` (or ``python scripts/seed_demo.py``).
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime
from pathlib import Path

# Allow ``python scripts/seed_demo.py`` in addition to ``python -m scripts.seed_demo``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from auth_service import generate_bootstrap_password, hash_password, normalize_email  # noqa: E402
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER  # noqa: E402
from database import init_database  # noqa: E402
from models import (  # noqa: E402
    Appointment,
    Company,
    FinancialTransaction,
    PortalAccount,
    PortalBranch,
    PortalUser,
    Service,
    ServiceCatalog,
    ServiceCategory,
    ServiceCategoryCatalog,
    Transaction,
)
from seed_fake_data import seed_activity, seed_companies  # noqa: E402
from setup_analytics import refresh_analytics_views  # noqa: E402

DEMO_EMAIL = 'demo@portal.local'
DEMO_SOURCE_TYPE = 'demo'
DEMO_ACCOUNT_LABEL = 'Demo'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Provision the shared demo tenant')
    parser.add_argument('--companies', type=int, default=3, help='Demo branches to generate')
    parser.add_argument('--days', type=int, default=120, help='How many days of activity to generate')
    parser.add_argument('--seed', type=int, default=42, help='Random seed for reproducible data')
    parser.add_argument('--clients-per-company', type=int, default=240)
    parser.add_argument('--staff-per-company', type=int, default=8)
    parser.add_argument('--goods-per-company', type=int, default=30)
    parser.add_argument('--appointments-per-day-min', type=int, default=6)
    parser.add_argument('--appointments-per-day-max', type=int, default=16)
    parser.add_argument('--skip-refresh-views', action='store_true', help='Skip analytics views refresh')
    args = parser.parse_args()
    if args.companies < 1:
        parser.error('--companies must be >= 1')
    if args.days < 1:
        parser.error('--days must be >= 1')
    if args.appointments_per_day_max < args.appointments_per_day_min:
        parser.error('--appointments-per-day-max must be >= --appointments-per-day-min')
    return args


def _demo_company_ids(db, account_id: int) -> list[int]:
    rows = db.execute(
        select(Company.id)
        .where(
            Company.source_type == DEMO_SOURCE_TYPE,
            (Company.portal_account_id == account_id) | (Company.portal_account_id.is_(None)),
        )
        .order_by(Company.id.asc())
    ).all()
    return [row[0] for row in rows]


def generate_demo_data(db, args: argparse.Namespace, account_id: int) -> list[int]:
    """Create synthetic companies + activity, marking them as demo. Returns their ids.

    Companies are inserted with ``source_type='demo'`` in ``seed_companies``' own
    committed transaction (before activity generation), so a mid-run failure
    leaves them discoverable by the idempotency guard on retry instead of being
    re-seeded as duplicates.
    """
    rng = random.Random(args.seed)
    refs = seed_companies(
        db,
        rng,
        companies_count=args.companies,
        clients_per_company=args.clients_per_company,
        staff_per_company=args.staff_per_company,
        goods_per_company=args.goods_per_company,
        source_type=DEMO_SOURCE_TYPE,
        portal_account_id=account_id,
        negative_ids=True,
    )
    company_ids = [ref.company.id for ref in refs]

    seed_activity(
        db,
        rng,
        refs=refs,
        days=args.days,
        appt_min=args.appointments_per_day_min,
        appt_max=args.appointments_per_day_max,
        negative_ids=True,
    )
    return company_ids


def get_or_create_demo_account(db) -> PortalAccount:
    account = (
        db.execute(
            select(PortalAccount)
            .where(PortalAccount.is_demo.is_(True))
            .order_by(PortalAccount.id.asc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if account is None:
        account = PortalAccount(label=DEMO_ACCOUNT_LABEL, is_demo=True, created_at=datetime.utcnow())
        db.add(account)
        db.flush()
    else:
        account.label = account.label or DEMO_ACCOUNT_LABEL
        account.is_demo = True
    return account


def ensure_branches(db, account_id: int, company_ids: list[int]) -> None:
    """Link each demo company to the demo account (UNIQUE on company_id)."""
    companies = db.execute(select(Company).where(Company.id.in_(company_ids))).scalars().all()
    for company in companies:
        if company.source_type != DEMO_SOURCE_TYPE:
            raise RuntimeError(f'Company {company.id} is not a demo company')
        if company.portal_account_id not in (None, account_id):
            raise RuntimeError(f'Demo company {company.id} is linked to another tenant')
        company.portal_account_id = account_id

    branches = {
        row.company_id: row
        for row in db.execute(select(PortalBranch).where(PortalBranch.company_id.in_(company_ids))).scalars().all()
    }
    for company_id in company_ids:
        branch = branches.get(company_id)
        if branch is None:
            db.add(PortalBranch(portal_account_id=account_id, company_id=company_id))
        elif branch.portal_account_id != account_id:
            raise RuntimeError(f'Demo company {company_id} already belongs to another tenant')


def repair_demo_data(db, company_ids: list[int]) -> None:
    """Backfill dashboard-facing demo rows and repair older demo payment links."""
    now = datetime.utcnow()
    categories = (
        db.execute(select(ServiceCategory).where(ServiceCategory.company_id.in_(company_ids))).scalars().all()
    )
    for category in categories:
        catalog = db.get(
            ServiceCategoryCatalog,
            {'company_id': category.company_id, 'category_id': category.id},
        )
        if catalog is None:
            db.add(
                ServiceCategoryCatalog(
                    company_id=category.company_id,
                    category_id=category.id,
                    title=category.title,
                    weight=category.weight,
                    api_id=category.api_id,
                    updated_at=now,
                )
            )
        else:
            catalog.title = category.title
            catalog.weight = category.weight
            catalog.api_id = category.api_id
            catalog.updated_at = now

    category_by_company_title = {
        (category.company_id, category.title): category.id
        for category in categories
        if category.title
    }
    services = db.execute(select(Service).where(Service.company_id.in_(company_ids))).scalars().all()
    for service in services:
        category_id = category_by_company_title.get((service.company_id, service.category_title))
        catalog = db.get(
            ServiceCatalog,
            {'company_id': service.company_id, 'service_id': service.id},
        )
        if catalog is None:
            db.add(
                ServiceCatalog(
                    company_id=service.company_id,
                    service_id=service.id,
                    title=service.title,
                    price_min=service.price_min,
                    duration=service.duration,
                    category_id=category_id,
                    category_title=service.category_title,
                    updated_at=now,
                )
            )
        else:
            catalog.title = service.title
            catalog.price_min = service.price_min
            catalog.duration = service.duration
            catalog.category_id = category_id
            catalog.category_title = service.category_title
            catalog.updated_at = now

    appointments = (
        db.execute(select(Appointment).where(Appointment.company_id.in_(company_ids))).scalars().all()
    )
    appointment_by_company_pk = {
        (appointment.company_id, appointment.id): appointment
        for appointment in appointments
    }
    appointment_by_company_external = {
        (appointment.company_id, appointment.external_id): appointment
        for appointment in appointments
        if appointment.external_id is not None
    }
    first_service_by_appointment = {
        row[0]: row[1]
        for row in db.execute(
            select(Transaction.appointment_id, Transaction.service_id)
            .where(Transaction.company_id.in_(company_ids))
            .order_by(Transaction.appointment_id.asc(), Transaction.id.asc())
        ).all()
        if row[1] is not None
    }
    financial_rows = (
        db.execute(
            select(FinancialTransaction).where(
                FinancialTransaction.company_id.in_(company_ids),
                FinancialTransaction.sold_item_type == 'service',
            )
        )
        .scalars()
        .all()
    )
    for payment in financial_rows:
        appointment = None
        for candidate_id in (payment.record_id, payment.document_id, payment.visit_id):
            if candidate_id is None:
                continue
            appointment = appointment_by_company_external.get((payment.company_id, candidate_id))
            if appointment is None:
                appointment = appointment_by_company_pk.get((payment.company_id, candidate_id))
            if appointment is not None:
                break
        if appointment is None:
            continue
        if appointment.external_id is not None:
            payment.record_id = appointment.external_id
            payment.visit_id = appointment.external_id
        service_id = first_service_by_appointment.get(appointment.id)
        if service_id is not None:
            payment.sold_item_id = service_id


def ensure_demo_user(db, account_id: int) -> PortalUser:
    email = normalize_email(DEMO_EMAIL)
    now = datetime.utcnow()
    user = db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none()
    if user is None:
        user = PortalUser(
            email=email,
            password_hash=hash_password(generate_bootstrap_password()),
            full_name='Demo',
            role='owner',
            is_active=True,
            is_demo=True,
            portal_account_id=account_id,
            email_verified_at=now,
            onboarding_completed_at=now,
            created_at=now,
        )
        db.add(user)
    else:
        if not user.is_demo:
            raise RuntimeError(f'Cannot reuse non-demo portal user as demo owner: {email}')
        user.portal_account_id = account_id
        user.role = 'owner'
        user.is_active = True
        user.is_demo = True
        user.email_verified_at = user.email_verified_at or now
        user.onboarding_completed_at = user.onboarding_completed_at or now
    return user


def main() -> int:
    args = parse_args()

    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    db = database.get_db()
    try:
        account = get_or_create_demo_account(db)
        db.flush()

        company_ids = _demo_company_ids(db, account.id)
        if company_ids:
            print(f'Demo data already present ({len(company_ids)} branches) — reusing.')
        else:
            print(f'Generating demo data: companies={args.companies}, days={args.days}')
            company_ids = generate_demo_data(db, args, account.id)

        ensure_branches(db, account.id, company_ids)
        repair_demo_data(db, company_ids)
        user = ensure_demo_user(db, account.id)
        db.commit()
        print(f'Demo tenant ready: account_id={account.id}, user={user.email}, branches={len(company_ids)}')

        if not args.skip_refresh_views:
            print('Refreshing analytics views...')
            refresh_analytics_views(verbose=True)

        print('Demo tenant provisioned successfully')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
