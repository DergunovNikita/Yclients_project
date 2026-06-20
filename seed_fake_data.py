"""
Generate synthetic BI data in PostgreSQL for local dashboard development.
"""
from __future__ import annotations

import argparse
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, text

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


def maybe_wipe_data(db) -> None:
    wipe_sql = "TRUNCATE TABLE " + ", ".join(WIPE_TABLES) + " RESTART IDENTITY CASCADE;"
    db.execute(text(wipe_sql))
    db.commit()


def pick_name(rng: random.Random) -> str:
    return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"


def next_pk(db, model, column_name: str = "id", start_from: int = 1) -> int:
    column = getattr(model, column_name)
    max_value = db.query(func.max(column)).scalar()
    if max_value is None:
        return start_from
    return int(max_value) + 1


def seed_companies(
    db,
    rng: random.Random,
    companies_count: int,
    clients_per_company: int,
    staff_per_company: int,
    goods_per_company: int,
) -> list[CompanyRefs]:
    group_id = next_pk(db, Group, "id", 1)
    group = Group(id=group_id, title=f"Synthetic Group {group_id}", access={"mode": "fake"})
    db.add(group)
    db.flush()

    refs: list[CompanyRefs] = []
    company_base_id = max(1000, next_pk(db, Company, "id", 1000))
    shared_service_categories = ["Hair", "Nails", "Cosmetology", "Body"]
    shared_positions = ["Master", "Senior Master", "Top Master", "Administrator"]

    for idx in range(companies_count):
        company_id = company_base_id + idx
        company = Company(id=company_id, title=f"Demo Branch {idx + 1}", group_id=group.id)
        db.add(company)
        db.flush()

        categories: list[ServiceCategory] = []
        service_cat_base = max(company_id * 10, next_pk(db, ServiceCategory, "id", company_id * 10))
        for c_idx, category_name in enumerate(shared_service_categories):
            cat_id = service_cat_base + c_idx
            cat = ServiceCategory(
                id=cat_id,
                title=category_name,
                weight=c_idx + 1,
                api_id=f"demo-cat-{cat_id}",
                company_id=company.id,
            )
            db.add(cat)
            categories.append(cat)

        services: list[Service] = []
        service_base = max(company_id * 100, next_pk(db, Service, "id", company_id * 100))
        for s_idx in range(max(8, len(SERVICE_NAMES))):
            srv_id = service_base + s_idx
            category = categories[s_idx % len(categories)]
            duration = rng.choice([1800, 2700, 3600, 5400])
            service = Service(
                id=srv_id,
                title=f"{SERVICE_NAMES[s_idx % len(SERVICE_NAMES)]} #{s_idx + 1}",
                price_min=round(rng.uniform(20, 120), 2),
                duration=duration,
                category_title=category.title,
                company_id=company.id,
            )
            db.add(service)
            services.append(service)

        position_base = max(company_id * 10, next_pk(db, StaffPosition, "id", company_id * 10))
        for p_idx, title in enumerate(shared_positions):
            db.add(
                StaffPosition(
                    id=position_base + p_idx,
                    title=title,
                    company_id=company.id,
                )
            )

        staff: list[Staff] = []
        staff_base = max(company_id * 1000, next_pk(db, Staff, "id", company_id * 1000))
        for st_idx in range(staff_per_company):
            staff_member = Staff(
                id=staff_base + st_idx,
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

        clients: list[Client] = []
        client_base = max(company_id * 100000, next_pk(db, Client, "id", company_id * 100000))
        for cl_idx in range(clients_per_company):
            client_id = client_base + cl_idx
            years = rng.randint(18, 65)
            birthday = date.today() - timedelta(days=years * 365 + rng.randint(0, 364))
            visits = rng.randint(0, 18)
            last_visit = date.today() - timedelta(days=rng.randint(0, 90))
            client = Client(
                id=client_id,
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

        accounts: list[Account] = []
        account_base = max(company_id * 10, next_pk(db, Account, "id", company_id * 10))
        for acc_idx, acc_title in enumerate(["Main Cash", "Card", "Online"]):
            account = Account(
                id=account_base + acc_idx,
                title=acc_title,
                type=acc_idx + 1,
                comment="synthetic",
                company_id=company.id,
            )
            db.add(account)
            accounts.append(account)

        good_category_base = max(company_id * 100, next_pk(db, GoodCategory, "id", company_id * 100))
        for s_idx, storage_title in enumerate(["Retail", "Care"]):
            db.add(
                GoodCategory(
                    id=good_category_base + s_idx,
                    title=storage_title,
                    parent_category_id=None,
                    company_id=company.id,
                )
            )

        storages: list[Storage] = []
        storage_base = max(company_id * 10, next_pk(db, Storage, "id", company_id * 10))
        for s_idx, storage_title in enumerate(["Main Storage", "Retail Shelf"]):
            storage = Storage(
                id=storage_base + s_idx,
                title=storage_title,
                for_services=True,
                for_sale=True,
                comment="synthetic",
                company_id=company.id,
            )
            db.add(storage)
            storages.append(storage)

        goods: list[Good] = []
        good_base = max(company_id * 10000, next_pk(db, Good, "good_id", company_id * 10000))
        for g_idx in range(goods_per_company):
            good = Good(
                good_id=good_base + g_idx,
                title=f"{GOOD_NAMES[g_idx % len(GOOD_NAMES)]} #{g_idx + 1}",
                cost=round(rng.uniform(5, 60), 2),
                actual_cost=round(rng.uniform(4, 50), 2),
                barcode=f"{company_id}{g_idx:08d}"[:13],
                unit_short_title=rng.choice(["pcs", "ml", "g"]),
                category_id=good_category_base + (g_idx % 2),
                last_change_date=datetime.now() - timedelta(days=rng.randint(0, 45)),
                company_id=company.id,
            )
            db.add(good)
            goods.append(good)

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
) -> None:
    start = date.today() - timedelta(days=days)
    end = date.today()
    appointment_id = next_pk(db, Appointment, "id", 1)
    financial_id = next_pk(db, FinancialTransaction, "id", 1)
    goods_tx_id = next_pk(db, GoodTransaction, "id", 1)
    comment_id = next_pk(db, Comment, "id", 1)

    for company_ref in refs:
        company = company_ref.company
        staff = company_ref.staff
        clients = company_ref.clients
        services = company_ref.services
        goods = company_ref.goods
        account_ids = [item.id for item in company_ref.accounts]
        storage_ids = [item.id for item in company_ref.storages]

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

                visit_total = 0.0
                for _tx in range(rng.randint(1, 3)):
                    srv = rng.choice(services)
                    cost = round(float(srv.price_min or 0) * rng.uniform(0.9, 1.25), 2)
                    first_cost = round(cost * rng.uniform(1.05, 1.2), 2)
                    db.add(
                        Transaction(
                            appointment_id=appointment.id,
                            service_id=srv.id,
                            service_title=srv.title,
                            cost=cost,
                            first_cost=first_cost,
                            amount=1,
                            company_id=company.id,
                        )
                    )
                    visit_total += cost

                db.add(
                    FinancialTransaction(
                        id=financial_id,
                        document_id=appointment.id,
                        expense_id=None,
                        date=dt + timedelta(minutes=duration // 60),
                        amount=round(visit_total, 2) if attendance > 0 else 0.0,
                        comment="visit payment",
                        account_id=rng.choice(account_ids),
                        client_id=client.id,
                        master_id=master.id,
                        record_id=appointment.id,
                        visit_id=appointment.id,
                        sold_item_id=appointment.id,
                        sold_item_type="service",
                        company_id=company.id,
                    )
                )
                financial_id += 1

                if rng.random() < 0.35:
                    good = rng.choice(goods)
                    qty = round(rng.uniform(1, 3), 2)
                    unit_cost = round(float(good.actual_cost or good.cost or 0) * rng.uniform(1.1, 1.6), 2)
                    db.add(
                        GoodTransaction(
                            id=goods_tx_id,
                            document_id=appointment.id,
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
                    goods_tx_id += 1

                if attendance > 0 and rng.random() < 0.25:
                    db.add(
                        Comment(
                            id=comment_id,
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
                    comment_id += 1

                appointment_id += 1

            for staff_member in staff:
                slot_start = datetime.combine(day, time(9, 0))
                for _ in range(18):
                    slot_end = slot_start + timedelta(minutes=30)
                    db.add(
                        StaffSchedule(
                            staff_id=staff_member.id,
                            date=day,
                            slot_from=slot_start.time(),
                            slot_to=slot_end.time(),
                            company_id=company.id,
                        )
                    )
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
