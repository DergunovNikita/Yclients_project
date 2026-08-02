"""
Generate synthetic BI data in PostgreSQL for local dashboard development.
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import init_database
from models import (
    Account,
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    Good,
    GoodCategory,
    GoodTransaction,
    Group,
    Service,
    ServiceCategory,
    ServiceCategoryCatalog,
    ServiceCatalog,
    Staff,
    StaffPosition,
    StaffSchedule,
    Storage,
    Transaction,
)
from setup_analytics import refresh_analytics_views


@dataclass
class CompanyRefs:
    company: Company
    services: list[Service]
    staff: list[Staff]
    clients: list[Client]
    goods: list[Good]
    accounts: list[Account]
    storages: list[Storage]


FIRST_NAMES = [
    "Anna", "Maria", "Sofia", "Elena", "Polina", "Olga", "Daria", "Irina", "Alina", "Nina",
]
LAST_NAMES = [
    "Ivanova", "Petrova", "Sidorova", "Kuznetsova", "Smirnova", "Fedorova", "Pavlova", "Morozova",
]
SERVICE_NAMES = [
    "Haircut", "Coloring", "Manicure", "Pedicure", "Massage", "Facial", "Brow Design", "Styling",
]
GOOD_NAMES = [
    "Shampoo", "Mask", "Hair Oil", "Nail Polish", "Cream", "Serum", "Brush", "Peeling",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Seed synthetic YClients BI data")
    parser.add_argument("--companies", type=int, default=3, help="Number of companies")
    parser.add_argument("--days", type=int, default=120, help="How many days back to generate")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible data")
    parser.add_argument("--appointments-per-day-min", type=int, default=6, help="Min appointments per day")
    parser.add_argument("--appointments-per-day-max", type=int, default=16, help="Max appointments per day")
    parser.add_argument("--clients-per-company", type=int, default=240, help="Clients per company")
    parser.add_argument("--staff-per-company", type=int, default=8, help="Staff members per company")
    parser.add_argument("--goods-per-company", type=int, default=30, help="Goods per company")
    parser.add_argument("--wipe", action="store_true", help="Delete existing business data before seeding")
    parser.add_argument(
        "--skip-refresh-views",
        action="store_true",
        help="Skip setup_analytics refresh after seed",
    )
    args = parser.parse_args()
    if args.days < 7:
        parser.error("--days should be >= 7")
    if args.appointments_per_day_min < 1:
        parser.error("--appointments-per-day-min should be >= 1")
    if args.appointments_per_day_max < args.appointments_per_day_min:
        parser.error("--appointments-per-day-max should be >= --appointments-per-day-min")
    return args


WIPE_TABLES = [
    'transactions',
    'appointments',
    'financial_transactions',
    'goods_transactions',
    'comments',
    'staff_schedules',
    'analytics_overall',
    'analytics_daily_metrics',
    'analytics_record_sources',
    'analytics_record_statuses',
    'z_report_payments',
    'z_reports',
    'services',
    'service_categories',
    'staff',
    'staff_positions',
    'clients',
    'accounts',
    'storages',
    'goods',
    'good_categories',
    'companies',
    'groups',
]


def quote_identifier(db, value: str) -> str:
    if not value.replace("_", "").isalnum() or not value[0].isalpha():
        raise ValueError(f"Unsafe SQL identifier: {value!r}")
    return db.bind.dialect.identifier_preparer.quote(value)


def maybe_wipe_data(db) -> None:
    table_names = ", ".join(quote_identifier(db, table_name) for table_name in WIPE_TABLES)
    wipe_sql = f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE;"
    db.connection().exec_driver_sql(wipe_sql)
    db.commit()


def pick_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def next_pk(db, model, column_name: str = "id", start_from: int = 1) -> int:
    column = getattr(model, column_name)
    max_value = db.query(func.max(column)).scalar()
    if max_value is None:
        return start_from
    return int(max_value) + 1


def next_negative_pk(db, model, column_name: str = "id") -> int:
    column = getattr(model, column_name)
    min_value = db.query(func.min(column)).scalar()
    if min_value is None or int(min_value) >= 0:
        return -1
    return int(min_value) - 1


def advance_pk(value: int, *, negative: bool = False) -> int:
    return value - 1 if negative else value + 1


def seed_companies(
    db,
    rng: random.Random,
    companies_count: int,
    clients_per_company: int,
    staff_per_company: int,
    goods_per_company: int,
    source_type: str = "yclients",
    portal_account_id: int | None = None,
    negative_ids: bool = False,
    negative_catalog_ids: bool = False,
) -> list[CompanyRefs]:
    negative_catalog_ids = negative_catalog_ids or negative_ids
    group_id = next_negative_pk(db, Group, "id") if negative_ids else next_pk(db, Group, "id", 1)
    group = Group(
        id=group_id,
        portal_account_id=portal_account_id,
        external_id=1,
        title=f"Synthetic Group {group_id}",
        access={"mode": "fake"},
    )
    db.add(group)
    db.flush()

    refs: list[CompanyRefs] = []
    company_id = next_negative_pk(db, Company, "id") if negative_ids else max(1000, next_pk(db, Company, "id", 1000))
    if negative_catalog_ids:
        service_cat_id = next_negative_pk(db, ServiceCategory, "id")
        service_id = next_negative_pk(db, Service, "id")
        position_id = next_negative_pk(db, StaffPosition, "id")
        account_id = next_negative_pk(db, Account, "id")
        good_category_id = next_negative_pk(db, GoodCategory, "id")
        storage_id = next_negative_pk(db, Storage, "id")
        good_id = next_negative_pk(db, Good, "good_id")
    else:
        service_cat_id = next_pk(db, ServiceCategory, "id", 1)
        service_id = next_pk(db, Service, "id", 1)
        position_id = next_pk(db, StaffPosition, "id", 1)
        account_id = next_pk(db, Account, "id", 1)
        good_category_id = next_pk(db, GoodCategory, "id", 1)
        storage_id = next_pk(db, Storage, "id", 1)
        good_id = next_pk(db, Good, "good_id", 1)
    staff_id = next_negative_pk(db, Staff, "id") if negative_ids else next_pk(db, Staff, "id", 1)
    client_id = next_negative_pk(db, Client, "id") if negative_ids else next_pk(db, Client, "id", 1)
    shared_service_categories = ["Hair", "Nails", "Cosmetology", "Body"]
    shared_positions = ["Master", "Senior Master", "Top Master", "Administrator"]

    for idx in range(companies_count):
        current_company_id = company_id
        company_id = advance_pk(company_id, negative=negative_ids)
        external_company_id = idx + 1
        company = Company(
            id=current_company_id,
            portal_account_id=portal_account_id,
            external_id=external_company_id,
            title=f"Demo Branch {idx + 1}",
            group_id=group.id,
            source_type=source_type,
        )
        db.add(company)
        db.flush()

        categories: list[ServiceCategory] = []
        now = datetime.now()
        for c_idx, category_name in enumerate(shared_service_categories):
            current_category_id = service_cat_id
            cat = ServiceCategory(
                id=current_category_id,
                title=category_name,
                weight=c_idx + 1,
                api_id=f"demo-cat-{company.id}-{c_idx + 1}",
                company_id=company.id,
            )
            db.add(cat)
            db.add(
                ServiceCategoryCatalog(
                    company_id=company.id,
                    category_id=current_category_id,
                    title=category_name,
                    weight=c_idx + 1,
                    api_id=cat.api_id,
                    updated_at=now,
                )
            )
            categories.append(cat)
            service_cat_id = advance_pk(service_cat_id, negative=negative_catalog_ids)

        services: list[Service] = []
        for s_idx in range(max(8, len(SERVICE_NAMES))):
            category = categories[s_idx % len(categories)]
            duration = rng.choice([1800, 2700, 3600, 5400])
            current_service_id = service_id
            title = f"{SERVICE_NAMES[s_idx % len(SERVICE_NAMES)]} #{s_idx + 1}"
            price_min = round(rng.uniform(20, 120), 2)
            service = Service(
                id=current_service_id,
                title=title,
                price_min=price_min,
                duration=duration,
                category_title=category.title,
                company_id=company.id,
            )
            db.add(service)
            db.add(
                ServiceCatalog(
                    company_id=company.id,
                    service_id=current_service_id,
                    title=title,
                    price_min=price_min,
                    duration=duration,
                    category_id=category.id,
                    category_title=category.title,
                    updated_at=now,
                )
            )
            services.append(service)
            service_id = advance_pk(service_id, negative=negative_catalog_ids)

        for p_idx, title in enumerate(shared_positions):
            db.add(
                StaffPosition(
                    id=position_id,
                    title=title,
                    company_id=company.id,
                )
            )
            position_id = advance_pk(position_id, negative=negative_catalog_ids)

        staff: list[Staff] = []
        for st_idx in range(staff_per_company):
            staff_member = Staff(
                id=staff_id,
                external_id=st_idx + 1,
                source_type=source_type,
                name=pick_name(rng),
                specialization=rng.choice(["Hair", "Nails", "Cosmetology", "Massage"]),
                position=rng.choice(shared_positions),
                rating=round(rng.uniform(4.0, 5.0), 2),
                votes_count=rng.randint(10, 400),
                bookable=True,
                company_id=company.id,
            )
            db.add(staff_member)
            staff.append(staff_member)
            staff_id = advance_pk(staff_id, negative=negative_ids)

        clients: list[Client] = []
        for cl_idx in range(clients_per_company):
            external_client_id = cl_idx + 1
            years = rng.randint(18, 65)
            birthday = date.today() - timedelta(days=years * 365 + rng.randint(0, 364))
            visits = rng.randint(0, 18)
            last_visit = date.today() - timedelta(days=rng.randint(0, 90))
            client = Client(
                id=client_id,
                external_id=external_client_id,
                source_type=source_type,
                name=pick_name(rng),
                phone=f"+7900{company_id % 100:02d}{cl_idx:06d}"[:12],
                email=f"client{company_id}_{cl_idx}@demo.local",
                birth_date=birthday,
                visits_count=visits,
                last_visit_date=last_visit,
                discount=rng.choice([0, 0, 0, 5, 10, 15]),
                company_id=company.id,
            )
            db.add(client)
            clients.append(client)
            client_id = advance_pk(client_id, negative=negative_ids)

        accounts: list[Account] = []
        for acc_idx, acc_title in enumerate(["Main Cash", "Card", "Online"]):
            account = Account(
                id=account_id,
                title=acc_title,
                type=acc_idx + 1,
                comment="synthetic",
                company_id=company.id,
            )
            db.add(account)
            accounts.append(account)
            account_id = advance_pk(account_id, negative=negative_catalog_ids)

        company_good_category_ids: list[int] = []
        for s_idx, storage_title in enumerate(["Retail", "Care"]):
            company_good_category_ids.append(good_category_id)
            db.add(
                GoodCategory(
                    id=good_category_id,
                    title=storage_title,
                    parent_category_id=None,
                    company_id=company.id,
                )
            )
            good_category_id = advance_pk(good_category_id, negative=negative_catalog_ids)

        storages: list[Storage] = []
        for s_idx, storage_title in enumerate(["Main Storage", "Retail Shelf"]):
            storage = Storage(
                id=storage_id,
                title=storage_title,
                for_services=True,
                for_sale=True,
                comment="synthetic",
                company_id=company.id,
            )
            db.add(storage)
            storages.append(storage)
            storage_id = advance_pk(storage_id, negative=negative_catalog_ids)

        goods: list[Good] = []
        for g_idx in range(goods_per_company):
            good = Good(
                good_id=good_id,
                title=f"{GOOD_NAMES[g_idx % len(GOOD_NAMES)]} #{g_idx + 1}",
                cost=round(rng.uniform(5, 60), 2),
                actual_cost=round(rng.uniform(4, 50), 2),
                barcode=f"{company_id}{g_idx:08d}"[:13],
                unit_short_title=rng.choice(["pcs", "ml", "g"]),
                category_id=company_good_category_ids[g_idx % len(company_good_category_ids)],
                last_change_date=datetime.now() - timedelta(days=rng.randint(0, 45)),
                company_id=company.id,
            )
            db.add(good)
            goods.append(good)
            good_id = advance_pk(good_id, negative=negative_catalog_ids)

        refs.append(
            CompanyRefs(
                company=company,
                services=services,
                staff=staff,
                clients=clients,
                goods=goods,
                accounts=accounts,
                storages=storages,
            )
        )

    db.commit()
    return refs


def seed_activity(
    db,
    rng: random.Random,
    refs: list[CompanyRefs],
    days: int,
    appt_min: int,
    appt_max: int,
    negative_ids: bool = False,
) -> None:
    start = date.today() - timedelta(days=days)
    end = date.today()
    appointment_id = next_negative_pk(db, Appointment, "id") if negative_ids else next_pk(db, Appointment, "id", 1)
    transaction_id = next_negative_pk(db, Transaction, "id") if negative_ids else next_pk(db, Transaction, "id", 1)
    financial_id = (
        next_negative_pk(db, FinancialTransaction, "id")
        if negative_ids
        else next_pk(db, FinancialTransaction, "id", 1)
    )
    goods_tx_id = (
        next_negative_pk(db, GoodTransaction, "id")
        if negative_ids
        else next_pk(db, GoodTransaction, "id", 1)
    )
    comment_id = next_negative_pk(db, Comment, "id") if negative_ids else next_pk(db, Comment, "id", 1)
    staff_schedule_id = (
        next_negative_pk(db, StaffSchedule, "id") if negative_ids else next_pk(db, StaffSchedule, "id", 1)
    )

    for company_ref in refs:
        company = company_ref.company
        staff = company_ref.staff
        clients = company_ref.clients
        services = company_ref.services
        goods = company_ref.goods
        account_ids = [item.id for item in company_ref.accounts]
        storage_ids = [item.id for item in company_ref.storages]
        source_type = company.source_type or "yclients"
        appointment_external_id = 1
        financial_external_id = 1
        goods_tx_external_id = 1
        comment_external_id = 1

        day = start
        while day <= end:
            daily_appointments = rng.randint(appt_min, appt_max)
            for _ in range(daily_appointments):
                master = rng.choice(staff)
                client = rng.choice(clients)
                attendance = rng.choices([1, 0, -1], weights=[78, 12, 10], k=1)[0]
                start_hour = rng.randint(9, 19)
                start_minute = rng.choice([0, 15, 30, 45])
                dt = datetime.combine(day, time(start_hour, start_minute))
                duration = rng.choice([1800, 2700, 3600, 5400])

                appointment = Appointment(
                    id=appointment_id,
                    external_id=appointment_external_id,
                    source_type=source_type,
                    company_id=company.id,
                    staff_id=master.id,
                    client_id=client.id,
                    date=day,
                    datetime=dt,
                    create_date=dt - timedelta(days=rng.randint(0, 20)),
                    seance_length=duration,
                    attendance=attendance,
                    comment=None if rng.random() < 0.7 else "synthetic appointment",
                )
                db.add(appointment)
                current_appointment_id = appointment.id

                visit_total = 0.0
                first_service_id = None
                for _tx in range(rng.randint(1, 3)):
                    srv = rng.choice(services)
                    if first_service_id is None:
                        first_service_id = srv.id
                    cost = round(float(srv.price_min or 0) * rng.uniform(0.9, 1.25), 2)
                    first_cost = round(cost * rng.uniform(1.05, 1.2), 2)
                    db.add(
                        Transaction(
                            id=transaction_id,
                            appointment_id=current_appointment_id,
                            service_id=srv.id,
                            service_title=srv.title,
                            cost=cost,
                            first_cost=first_cost,
                            amount=1,
                            company_id=company.id,
                        )
                    )
                    transaction_id = advance_pk(transaction_id, negative=negative_ids)
                    visit_total += cost

                db.add(
                    FinancialTransaction(
                        id=financial_id,
                        external_id=financial_external_id,
                        source_type=source_type,
                        document_id=current_appointment_id,
                        expense_id=None,
                        date=dt + timedelta(minutes=duration // 60),
                        amount=round(visit_total, 2) if attendance > 0 else 0.0,
                        comment="visit payment",
                        account_id=rng.choice(account_ids),
                        client_id=client.id,
                        master_id=master.id,
                        record_id=appointment.external_id,
                        visit_id=appointment.external_id,
                        sold_item_id=first_service_id,
                        sold_item_type="service",
                        company_id=company.id,
                    )
                )
                financial_id = advance_pk(financial_id, negative=negative_ids)
                financial_external_id += 1

                if rng.random() < 0.35:
                    good = rng.choice(goods)
                    qty = round(rng.uniform(1, 3), 2)
                    unit_cost = round(float(good.actual_cost or good.cost or 0) * rng.uniform(1.1, 1.6), 2)
                    db.add(
                        GoodTransaction(
                            id=goods_tx_id,
                            external_id=goods_tx_external_id,
                            source_type=source_type,
                            document_id=appointment.id,
                            # Goods reports filter on date; without it every seeded
                            # sale is invisible and the goods reports render empty.
                            date=dt + timedelta(minutes=duration // 60),
                            type_id=1,
                            good_id=good.good_id,
                            storage_id=rng.choice(storage_ids),
                            amount=qty,
                            cost_per_unit=unit_cost,
                            cost=round(unit_cost * qty, 2),
                            discount=rng.choice([0, 0, 5, 10]),
                            master_id=master.id,
                            client_id=client.id,
                            company_id=company.id,
                        )
                    )
                    goods_tx_id = advance_pk(goods_tx_id, negative=negative_ids)
                    goods_tx_external_id += 1

                if attendance > 0 and rng.random() < 0.25:
                    db.add(
                        Comment(
                            id=comment_id,
                            external_id=comment_external_id,
                            source_type=source_type,
                            type="review",
                            master_id=master.id,
                            text="Synthetic feedback",
                            date=dt + timedelta(hours=2),
                            rating=rng.choices([3, 4, 5], weights=[10, 35, 55], k=1)[0],
                            user_id=client.id,
                            user_name=client.name,
                            record_id=appointment.id,
                            company_id=company.id,
                        )
                    )
                    comment_id = advance_pk(comment_id, negative=negative_ids)
                    comment_external_id += 1

                appointment_id = advance_pk(appointment_id, negative=negative_ids)
                appointment_external_id += 1

            for staff_member in staff:
                slot_start = datetime.combine(day, time(9, 0))
                for _ in range(18):
                    slot_end = slot_start + timedelta(minutes=30)
                    db.add(
                        StaffSchedule(
                            id=staff_schedule_id,
                            staff_id=staff_member.id,
                            date=day,
                            slot_from=slot_start.time(),
                            slot_to=slot_end.time(),
                            company_id=company.id,
                        )
                    )
                    staff_schedule_id = advance_pk(staff_schedule_id, negative=negative_ids)
                    slot_start = slot_end

            day += timedelta(days=1)

        db.commit()


def main() -> int:
    args = parse_args()
    rng = random.Random(args.seed)

    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    db = database.get_db()
    try:
        existing_companies = db.query(func.count(Company.id)).scalar() or 0
        if existing_companies and not args.wipe:
            print(
                f'Refusing to seed: database already has {existing_companies} companies. '
                'Use --wipe only on a local/dev database with synthetic data.'
            )
            return 1

        if args.wipe:
            print("Cleaning existing business data...")
            maybe_wipe_data(db)

        print(
            f"Generating synthetic data: companies={args.companies}, days={args.days}, "
            f"staff/company={args.staff_per_company}"
        )
        refs = seed_companies(
            db=db,
            rng=rng,
            companies_count=args.companies,
            clients_per_company=args.clients_per_company,
            staff_per_company=args.staff_per_company,
            goods_per_company=args.goods_per_company,
        )
        seed_activity(
            db=db,
            rng=rng,
            refs=refs,
            days=args.days,
            appt_min=args.appointments_per_day_min,
            appt_max=args.appointments_per_day_max,
        )

        if not args.skip_refresh_views:
            print("Refreshing analytics views...")
            refresh_analytics_views(verbose=True)

        print("Synthetic data generated successfully")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
