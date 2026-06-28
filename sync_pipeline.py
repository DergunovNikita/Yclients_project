"""
Production ETL pipeline for syncing YClients data into PostgreSQL.
"""
import time
from datetime import date, timedelta, datetime
from typing import Iterable
from config import (
    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD,
    SYNC_DAYS, SCHEDULE_DAYS, ANALYTICS_DAYS, DB_BATCH_SIZE,
    SYNC_HISTORY_START_DATE, SYNC_INCREMENTAL, SYNC_LOOKBACK_DAYS,
    YCLIENTS_REQUEST_DELAY, YCLIENTS_TIMEOUT,
    YCLIENTS_RETRY_TOTAL, YCLIENTS_RETRY_BACKOFF,
)
from data_sources import SOURCE_YCLIENTS, adapter_from_payload
from yclients_api import YClientsAPI
from yclients_credentials import (
    YClientsCredentialValue,
    load_active_credentials_sync,
    mark_credential_failure_sync,
    mark_credential_success_sync,
)
from database import init_database
from models import (
    Group, Company,
    ServiceCategory, ServiceCategoryCatalog, Service,
    ServiceCatalog, StaffPosition, StaffPositionCatalog, Staff, Client,
    Account, AccountCatalog, Storage, StorageCatalog,
    GoodCategory, GoodCategoryCatalog, Good, GoodCatalog,
    Appointment, Transaction, FinancialTransaction, GoodTransaction,
    Comment, StaffSchedule,
    AnalyticsOverall, AnalyticsDailyMetric, AnalyticsSourceMetric,
    AnalyticsStatusMetric, ZReport, ZReportPayment, SyncState, SyncSourceState,
)
from sync_parsing import parse_date, parse_datetime, parse_datetime_end, parse_datetime_start, parse_time

TRANSACTIONAL_STATE_KEY = 'transactions_last_success_date'
PERSONAL_ACCOUNT_SOURCE = 'financial_transactions_detail'


def format_duration(seconds: float) -> str:
    total_seconds = int(seconds)
    minutes, secs = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин {secs} сек"
    if minutes:
        return f"{minutes} мин {secs} сек"
    return f"{seconds:.2f} сек"


def chunked(items: Iterable[int], size: int):
    items = list(items)
    batch_size = max(1, size)
    for idx in range(0, len(items), batch_size):
        yield items[idx:idx + batch_size]


def load_existing_map(db, model, ids, pk_column):
    existing = {}
    unique_ids = [item_id for item_id in dict.fromkeys(ids) if item_id is not None]
    for batch in chunked(unique_ids, DB_BATCH_SIZE):
        for obj in db.query(model).filter(pk_column.in_(batch)).all():
            existing[getattr(obj, pk_column.key)] = obj
    return existing


def load_existing_branch_map(db, model, company_id: int, ids, id_column):
    existing = {}
    unique_ids = [item_id for item_id in dict.fromkeys(ids) if item_id is not None]
    for batch in chunked(unique_ids, DB_BATCH_SIZE):
        for obj in (
            db.query(model)
            .filter(model.company_id == company_id, id_column.in_(batch))
            .all()
        ):
            existing[getattr(obj, id_column.key)] = obj
    return existing


def bulk_delete_by_ids(db, model, column, ids) -> int:
    deleted = 0
    unique_ids = [item_id for item_id in dict.fromkeys(ids) if item_id is not None]
    for batch in chunked(unique_ids, DB_BATCH_SIZE):
        deleted += (
            db.query(model)
            .filter(column.in_(batch))
            .delete(synchronize_session=False)
        )
    return deleted


def run_sync_step(results, name: str, fn, *args, **kwargs):
    step_key = kwargs.pop('step_key', name)
    progress_callback = kwargs.pop('progress_callback', None)
    progress_context = kwargs.pop('progress_context', {}) or {}
    progress_pct = kwargs.pop('progress_pct', None)
    started_at = time.perf_counter()
    success = False
    try:
        success = bool(fn(*args, **kwargs))
        return success
    finally:
        elapsed = time.perf_counter() - started_at
        results.append({
            'name': name,
            'key': step_key,
            'success': success,
            'elapsed': elapsed,
        })
        if progress_callback is not None:
            progress_callback({
                **progress_context,
                'stage_key': step_key,
                'status': 'success' if success else 'warning',
                'elapsed_seconds': elapsed,
                'message': name,
                'progress_pct': progress_pct,
                'step_results': list(results),
            })
        status = 'OK' if success else 'WARN'
        print(f"  [{status}] {name}: {format_duration(elapsed)}")


def print_sync_summary(results):
    print("\n" + "=" * 60)
    print("  Итоги по этапам")
    print("=" * 60)
    for item in results:
        status = 'OK' if item['success'] else 'WARN'
        print(f"  [{status}] {item['name']:<28} {format_duration(item['elapsed'])}")


def get_sync_state_value(db, key: str):
    state = db.get(SyncState, key)
    return state.value if state else None


def set_sync_state_value(db, key: str, value: str):
    state = db.get(SyncState, key)
    if not state:
        state = SyncState(key=key)
        db.add(state)
    state.value = value
    state.updated_at = datetime.now()
    db.commit()


def full_sync_start_date(end_date: date) -> date:
    if SYNC_DAYS and SYNC_DAYS > 0:
        return end_date - timedelta(days=SYNC_DAYS)
    return min(SYNC_HISTORY_START_DATE, end_date)


def resolve_transaction_window(db, end_date: date):
    full_start = full_sync_start_date(end_date)
    if not SYNC_INCREMENTAL:
        return full_start, 'full'

    raw_value = get_sync_state_value(db, TRANSACTIONAL_STATE_KEY)
    if not raw_value:
        return full_start, 'full'

    try:
        last_success = date.fromisoformat(raw_value)
    except ValueError:
        return full_start, 'full'

    incremental_start = last_success - timedelta(days=max(0, SYNC_LOOKBACK_DAYS))
    return max(full_start, incremental_start), 'incremental'


def resolve_sync_window(db, end_date: date, requested_mode: str):
    normalized_mode = (requested_mode or 'incremental').strip().lower()
    if normalized_mode == 'full':
        return full_sync_start_date(end_date), 'full'
    return resolve_transaction_window(db, end_date)


def purge_full_refresh_window(db, company_id: int, start_date: str, end_date: str, schedule_end_date: str):
    start_bound = parse_date(start_date)
    end_bound = parse_date(end_date)
    schedule_end_bound = parse_date(schedule_end_date)
    try:
        appointment_ids = [
            row[0]
            for row in (
                db.query(Appointment.id)
                .filter(
                    Appointment.company_id == company_id,
                    Appointment.date >= start_bound,
                    Appointment.date <= end_bound,
                )
                .all()
            )
        ]
        deleted_transactions = 0
        if appointment_ids:
            deleted_transactions = bulk_delete_by_ids(db, Transaction, Transaction.appointment_id, appointment_ids)

        deleted_appointments = (
            db.query(Appointment)
            .filter(
                Appointment.company_id == company_id,
                Appointment.date >= start_bound,
                Appointment.date <= end_bound,
            )
            .delete(synchronize_session=False)
        )
        deleted_financial = (
            db.query(FinancialTransaction)
            .filter(
                FinancialTransaction.company_id == company_id,
                FinancialTransaction.date >= parse_datetime_start(start_date),
                FinancialTransaction.date <= parse_datetime_end(end_date),
            )
            .delete(synchronize_session=False)
        )
        deleted_goods = (
            db.query(GoodTransaction)
            .filter(GoodTransaction.company_id == company_id)
            .delete(synchronize_session=False)
        )
        deleted_comments = (
            db.query(Comment)
            .filter(
                Comment.company_id == company_id,
                Comment.date >= parse_datetime_start(start_date),
                Comment.date <= parse_datetime_end(end_date),
            )
            .delete(synchronize_session=False)
        )
        deleted_schedules = (
            db.query(StaffSchedule)
            .filter(
                StaffSchedule.company_id == company_id,
                StaffSchedule.date >= end_bound,
                StaffSchedule.date <= schedule_end_bound,
            )
            .delete(synchronize_session=False)
        )
        db.commit()
        print(
            "  ✓ Full refresh cleanup: "
            f"appointments={deleted_appointments}, "
            f"transactions={deleted_transactions}, "
            f"financial={deleted_financial}, "
            f"goods={deleted_goods}, "
            f"comments={deleted_comments}, "
            f"schedules={deleted_schedules}"
        )
        return True
    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка очистки перед full refresh: {e}")
        return False


def steps_successful(results, step_names) -> bool:
    named_steps = [item for item in results if item['key'] in step_names]
    return bool(named_steps) and all(item['success'] for item in named_steps)


def get_target_companies(db, company_ids: Iterable[int] | None = None):
    query = db.query(Company).filter(Company.id.isnot(None))
    if company_ids is not None:
        ids = [int(item) for item in company_ids]
        if not ids:
            return []
        query = query.filter(Company.id.in_(ids))
    return query.order_by(Company.title.asc(), Company.id.asc()).all()


def _build_api_for_credential(credential: YClientsCredentialValue) -> YClientsAPI | None:
    adapter = adapter_from_payload(
        SOURCE_YCLIENTS,
        partner_token=credential.partner_token,
        login=credential.login,
        password=credential.password,
        request_delay=YCLIENTS_REQUEST_DELAY,
        timeout=YCLIENTS_TIMEOUT,
        retry_total=YCLIENTS_RETRY_TOTAL,
        retry_backoff=YCLIENTS_RETRY_BACKOFF,
    )
    if not adapter.authenticate():
        return None
    return adapter.build_sync_client()


def format_company_label(company: Company) -> str:
    title = (company.title or '').strip() or f'Company {company.id}'
    return f"{title} ({company.id})"


# ===================================================================
# 1. Сети и компании
# ===================================================================

def sync_groups_and_companies(api: YClientsAPI, db):
    print("\n── Сети и компании ──")

    groups = api.get_groups()
    if not groups:
        return False

    print(f"  Найдено сетей: {len(groups)}")

    try:
        group_ids = [group_data.get('id') for group_data in groups if group_data.get('id') is not None]
        company_ids = [
            company_data.get('id')
            for group_data in groups
            for company_data in (group_data.get('companies') or [])
            if company_data.get('id') is not None
        ]
        existing_groups = load_existing_map(db, Group, group_ids, Group.id)
        existing_companies = load_existing_map(db, Company, company_ids, Company.id)

        for group_data in groups:
            group_id = group_data.get('id')
            if group_id is None:
                continue

            group = existing_groups.get(group_id)
            if not group:
                group = Group(
                    id=group_id,
                    title=group_data.get('title', ''),
                    access=group_data.get('access')
                )
                db.add(group)
            else:
                group.title = group_data.get('title', '')
                group.access = group_data.get('access')

            if 'companies' in group_data and group_data['companies']:
                for company_data in group_data['companies']:
                    company_id = company_data.get('id')
                    if company_id is None:
                        continue

                    company = existing_companies.get(company_id)
                    if not company:
                        company = Company(
                            id=company_id,
                            title=company_data.get('title', ''),
                            group_id=group_id
                        )
                        db.add(company)
                    else:
                        company.title = company_data.get('title', '')
                        company.group_id = group_id

        db.commit()
        print("  ✓ Сети и компании сохранены")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 2. Категории услуг
# ===================================================================

def sync_service_categories(api: YClientsAPI, db, company_id: str):
    print("\n── Категории услуг ──")

    categories = api.get_service_categories(company_id)
    if not categories:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(categories)}")

    try:
        cid = int(company_id)
        now = datetime.now()
        existing_categories = load_existing_map(
            db,
            ServiceCategory,
            (category.get('id') for category in categories),
            ServiceCategory.id,
        )
        existing_catalog = load_existing_branch_map(
            db,
            ServiceCategoryCatalog,
            cid,
            (category.get('id') for category in categories),
            ServiceCategoryCatalog.category_id,
        )
        for c in categories:
            cat_id = c.get('id')
            if cat_id is None:
                continue
            catalog_obj = existing_catalog.get(cat_id)
            if not catalog_obj:
                catalog_obj = ServiceCategoryCatalog(
                    company_id=cid,
                    category_id=cat_id,
                    title=c.get('title', ''),
                    weight=c.get('weight'),
                    api_id=c.get('api_id'),
                    updated_at=now,
                )
                db.add(catalog_obj)
                existing_catalog[cat_id] = catalog_obj
            else:
                catalog_obj.title = c.get('title', '')
                catalog_obj.weight = c.get('weight')
                catalog_obj.api_id = c.get('api_id')
                catalog_obj.updated_at = now

            obj = existing_categories.get(cat_id)
            if not obj:
                obj = ServiceCategory(
                    id=cat_id,
                    title=c.get('title', ''),
                    weight=c.get('weight'),
                    api_id=c.get('api_id'),
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.title = c.get('title', '')
                obj.weight = c.get('weight')
                obj.api_id = c.get('api_id')

        db.commit()
        print(f"  ✓ Категории услуг сохранены ({len(categories)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 3. Услуги
# ===================================================================

def sync_services(api: YClientsAPI, db, company_id: str):
    print("\n── Услуги ──")

    services = api.get_services(company_id)
    if not services:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(services)}")

    try:
        cid = int(company_id)
        now = datetime.now()
        category_by_service_id = {}
        if not any(service.get('category') for service in services):
            category_rows = db.query(ServiceCategoryCatalog).filter(
                ServiceCategoryCatalog.company_id == cid,
            ).all()
            for category in category_rows:
                category_services = api.get_services(company_id, category_id=category.category_id) or []
                for category_service in category_services:
                    category_service_id = category_service.get('id')
                    if category_service_id is None:
                        continue
                    category_by_service_id[category_service_id] = {
                        'id': category.category_id,
                        'title': category.title,
                    }

        existing_services = load_existing_map(
            db,
            Service,
            (service.get('id') for service in services),
            Service.id,
        )
        existing_catalog = load_existing_branch_map(
            db,
            ServiceCatalog,
            cid,
            (service.get('id') for service in services),
            ServiceCatalog.service_id,
        )
        for service_data in services:
            service_id = service_data.get('id')
            if service_id is None:
                continue
            category_title = None
            category_id = None
            if 'category' in service_data and service_data['category']:
                category_id = service_data['category'].get('id')
                category_title = service_data['category'].get('title')
            elif service_id in category_by_service_id:
                category_id = category_by_service_id[service_id].get('id')
                category_title = category_by_service_id[service_id].get('title')

            catalog_obj = existing_catalog.get(service_id)
            if not catalog_obj:
                catalog_obj = ServiceCatalog(
                    company_id=cid,
                    service_id=service_id,
                    title=service_data.get('title', ''),
                    price_min=service_data.get('price_min'),
                    duration=service_data.get('duration'),
                    category_id=category_id,
                    category_title=category_title,
                    updated_at=now,
                )
                db.add(catalog_obj)
                existing_catalog[service_id] = catalog_obj
            else:
                catalog_obj.title = service_data.get('title', '')
                catalog_obj.price_min = service_data.get('price_min')
                catalog_obj.duration = service_data.get('duration')
                catalog_obj.category_id = category_id
                catalog_obj.category_title = category_title
                catalog_obj.updated_at = now

            obj = existing_services.get(service_id)
            if not obj:
                obj = Service(
                    id=service_id,
                    title=service_data.get('title', ''),
                    price_min=service_data.get('price_min'),
                    duration=service_data.get('duration'),
                    category_title=category_title,
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.title = service_data.get('title', '')
                obj.price_min = service_data.get('price_min')
                obj.duration = service_data.get('duration')
                obj.category_title = category_title

        db.commit()
        print(f"  ✓ Услуги сохранены ({len(services)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 4. Должности
# ===================================================================

def sync_positions(api: YClientsAPI, db, company_id: str):
    print("\n── Должности ──")

    positions = api.get_positions(company_id)
    if not positions:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(positions)}")

    try:
        cid = int(company_id)
        now = datetime.now()
        existing_positions = load_existing_map(
            db,
            StaffPosition,
            (position.get('id') for position in positions),
            StaffPosition.id,
        )
        existing_catalog = load_existing_branch_map(
            db,
            StaffPositionCatalog,
            cid,
            (position.get('id') for position in positions),
            StaffPositionCatalog.position_id,
        )
        for p in positions:
            pid = p.get('id')
            if pid is None:
                continue
            catalog_obj = existing_catalog.get(pid)
            if not catalog_obj:
                catalog_obj = StaffPositionCatalog(
                    company_id=cid,
                    position_id=pid,
                    title=p.get('title', ''),
                    updated_at=now,
                )
                db.add(catalog_obj)
                existing_catalog[pid] = catalog_obj
            else:
                catalog_obj.title = p.get('title', '')
                catalog_obj.updated_at = now

            obj = existing_positions.get(pid)
            if not obj:
                obj = StaffPosition(id=pid, title=p.get('title', ''), company_id=cid)
                db.add(obj)
            else:
                obj.title = p.get('title', '')

        db.commit()
        print(f"  ✓ Должности сохранены ({len(positions)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 5. Сотрудники
# ===================================================================

def sync_staff(api: YClientsAPI, db, company_id: str):
    print("\n── Сотрудники ──")

    staff_list = api.get_staff(company_id)
    if not staff_list:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(staff_list)}")

    try:
        cid = int(company_id)
        staff_ids = {staff_member.get('id') for staff_member in staff_list if staff_member.get('id') is not None}
        existing_staff = load_existing_map(
            db,
            Staff,
            staff_ids,
            Staff.id,
        )
        for s in staff_list:
            staff_id = s.get('id')
            if staff_id is None:
                continue

            position_title = None
            pos = s.get('position')
            if isinstance(pos, dict):
                position_title = pos.get('title')
            elif isinstance(pos, str):
                position_title = pos

            user_id = s.get('user_id')
            fired = int(bool(s.get('is_fired'))) if s.get('fired') is None else int(s.get('fired') or 0)

            obj = existing_staff.get(staff_id)
            if not obj:
                obj = Staff(
                    id=staff_id, name=s.get('name', ''),
                    specialization=s.get('specialization'),
                    position=position_title,
                    avatar_url=s.get('avatar'),
                    rating=s.get('rating'),
                    votes_count=s.get('votes_count'),
                    bookable=s.get('bookable', True),
                    fired=fired,
                    user_id=user_id,
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.name = s.get('name', '')
                obj.specialization = s.get('specialization')
                obj.position = position_title
                obj.avatar_url = s.get('avatar')
                obj.rating = s.get('rating')
                obj.votes_count = s.get('votes_count')
                obj.bookable = s.get('bookable', True)
                obj.fired = fired
                obj.user_id = user_id

        stale_query = db.query(Staff).filter(Staff.company_id == cid)
        if staff_ids:
            stale_query = stale_query.filter(~Staff.id.in_(staff_ids))
        stale_query.update({Staff.fired: 1}, synchronize_session=False)

        db.commit()
        print(f"  ✓ Сотрудники сохранены ({len(staff_list)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 6. Клиенты
# ===================================================================

def sync_clients(api: YClientsAPI, db, company_id: str):
    print("\n── Клиенты ──")

    clients = api.get_clients(company_id)
    if not clients:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(clients)}")

    try:
        cid = int(company_id)
        existing_clients = load_existing_map(
            db,
            Client,
            (client.get('id') for client in clients),
            Client.id,
        )
        for c in clients:
            client_id = c.get('id')
            if client_id is None:
                continue

            obj = existing_clients.get(client_id)
            if not obj:
                obj = Client(
                    id=client_id, name=c.get('name', ''),
                    phone=c.get('phone'), email=c.get('email'),
                    birth_date=parse_date(c.get('birth_date')),
                    visits_count=c.get('visits_count', 0),
                    last_visit_date=parse_date(c.get('last_visit_date')),
                    discount=c.get('discount', 0),
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.name = c.get('name', '')
                obj.phone = c.get('phone')
                obj.email = c.get('email')
                obj.birth_date = parse_date(c.get('birth_date'))
                obj.visits_count = c.get('visits_count', 0)
                obj.last_visit_date = parse_date(c.get('last_visit_date'))
                obj.discount = c.get('discount', 0)

        db.commit()
        print(f"  ✓ Клиенты сохранены ({len(clients)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 7. Кассы
# ===================================================================

def sync_accounts(api: YClientsAPI, db, company_id: str):
    print("\n── Кассы ──")

    accounts = api.get_accounts(company_id)
    if not accounts:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(accounts)}")

    try:
        cid = int(company_id)
        now = datetime.now()
        existing_accounts = load_existing_map(
            db,
            Account,
            (account.get('id') for account in accounts),
            Account.id,
        )
        existing_catalog = load_existing_branch_map(
            db,
            AccountCatalog,
            cid,
            (account.get('id') for account in accounts),
            AccountCatalog.account_id,
        )
        for a in accounts:
            aid = a.get('id')
            if aid is None:
                continue
            catalog_obj = existing_catalog.get(aid)
            if not catalog_obj:
                catalog_obj = AccountCatalog(
                    company_id=cid,
                    account_id=aid,
                    title=a.get('title', ''),
                    type=a.get('type'),
                    comment=a.get('comment'),
                    updated_at=now,
                )
                db.add(catalog_obj)
                existing_catalog[aid] = catalog_obj
            else:
                catalog_obj.title = a.get('title', '')
                catalog_obj.type = a.get('type')
                catalog_obj.comment = a.get('comment')
                catalog_obj.updated_at = now

            obj = existing_accounts.get(aid)
            if not obj:
                obj = Account(
                    id=aid, title=a.get('title', ''),
                    type=a.get('type'), comment=a.get('comment'),
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.title = a.get('title', '')
                obj.type = a.get('type')
                obj.comment = a.get('comment')

        db.commit()
        print(f"  ✓ Кассы сохранены ({len(accounts)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 8. Склады
# ===================================================================

def sync_storages(api: YClientsAPI, db, company_id: str):
    print("\n── Склады ──")

    storages = api.get_storages(company_id)
    if not storages:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(storages)}")

    try:
        cid = int(company_id)
        now = datetime.now()
        existing_storages = load_existing_map(
            db,
            Storage,
            (storage.get('id') for storage in storages),
            Storage.id,
        )
        existing_catalog = load_existing_branch_map(
            db,
            StorageCatalog,
            cid,
            (storage.get('id') for storage in storages),
            StorageCatalog.storage_id,
        )
        for s in storages:
            sid = s.get('id')
            if sid is None:
                continue
            catalog_obj = existing_catalog.get(sid)
            if not catalog_obj:
                catalog_obj = StorageCatalog(
                    company_id=cid,
                    storage_id=sid,
                    title=s.get('title', ''),
                    for_services=s.get('for_services', False),
                    for_sale=s.get('for_sale', False),
                    comment=s.get('comment'),
                    updated_at=now,
                )
                db.add(catalog_obj)
                existing_catalog[sid] = catalog_obj
            else:
                catalog_obj.title = s.get('title', '')
                catalog_obj.for_services = s.get('for_services', False)
                catalog_obj.for_sale = s.get('for_sale', False)
                catalog_obj.comment = s.get('comment')
                catalog_obj.updated_at = now

            obj = existing_storages.get(sid)
            if not obj:
                obj = Storage(
                    id=sid, title=s.get('title', ''),
                    for_services=s.get('for_services', False),
                    for_sale=s.get('for_sale', False),
                    comment=s.get('comment'),
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.title = s.get('title', '')
                obj.for_services = s.get('for_services', False)
                obj.for_sale = s.get('for_sale', False)
                obj.comment = s.get('comment')

        db.commit()
        print(f"  ✓ Склады сохранены ({len(storages)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 9. Категории товаров
# ===================================================================

def sync_good_categories(api: YClientsAPI, db, company_id: str):
    print("\n── Категории товаров ──")

    categories = api.get_good_categories(company_id)
    if not categories:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(categories)}")

    try:
        cid = int(company_id)
        now = datetime.now()
        existing_categories = load_existing_map(
            db,
            GoodCategory,
            (category.get('id') for category in categories),
            GoodCategory.id,
        )
        existing_catalog = load_existing_branch_map(
            db,
            GoodCategoryCatalog,
            cid,
            (category.get('id') for category in categories),
            GoodCategoryCatalog.category_id,
        )
        for c in categories:
            cat_id = c.get('id')
            if cat_id is None:
                continue
            catalog_obj = existing_catalog.get(cat_id)
            if not catalog_obj:
                catalog_obj = GoodCategoryCatalog(
                    company_id=cid,
                    category_id=cat_id,
                    title=c.get('title', ''),
                    parent_category_id=c.get('parent_category_id'),
                    updated_at=now,
                )
                db.add(catalog_obj)
                existing_catalog[cat_id] = catalog_obj
            else:
                catalog_obj.title = c.get('title', '')
                catalog_obj.parent_category_id = c.get('parent_category_id')
                catalog_obj.updated_at = now

            obj = existing_categories.get(cat_id)
            if not obj:
                obj = GoodCategory(
                    id=cat_id, title=c.get('title', ''),
                    parent_category_id=c.get('parent_category_id'),
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.title = c.get('title', '')
                obj.parent_category_id = c.get('parent_category_id')

        db.commit()
        print(f"  ✓ Категории товаров сохранены ({len(categories)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 10. Товары
# ===================================================================

def sync_goods(api: YClientsAPI, db, company_id: str):
    print("\n── Товары ──")

    goods = api.get_goods(company_id)
    if not goods:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(goods)}")

    try:
        cid = int(company_id)
        now = datetime.now()
        good_ids = [
            g.get('good_id') or g.get('id')
            for g in goods
            if g.get('good_id') or g.get('id')
        ]
        existing_goods = load_existing_map(db, Good, good_ids, Good.good_id)
        existing_catalog = load_existing_branch_map(
            db,
            GoodCatalog,
            cid,
            good_ids,
            GoodCatalog.good_id,
        )
        for g in goods:
            gid = g.get('good_id') or g.get('id')
            if not gid:
                continue
            last_change_date = parse_datetime(g.get('last_change_date'))

            catalog_obj = existing_catalog.get(gid)
            if not catalog_obj:
                catalog_obj = GoodCatalog(
                    company_id=cid,
                    good_id=gid,
                    title=g.get('title', ''),
                    cost=g.get('cost'),
                    actual_cost=g.get('actual_cost'),
                    barcode=g.get('barcode'),
                    unit_short_title=g.get('unit_short_title'),
                    category_id=g.get('category_id'),
                    last_change_date=last_change_date,
                    updated_at=now,
                )
                db.add(catalog_obj)
                existing_catalog[gid] = catalog_obj
            else:
                catalog_obj.title = g.get('title', '')
                catalog_obj.cost = g.get('cost')
                catalog_obj.actual_cost = g.get('actual_cost')
                catalog_obj.barcode = g.get('barcode')
                catalog_obj.unit_short_title = g.get('unit_short_title')
                catalog_obj.category_id = g.get('category_id')
                catalog_obj.last_change_date = last_change_date
                catalog_obj.updated_at = now

            obj = existing_goods.get(gid)
            if not obj:
                obj = Good(
                    good_id=gid, title=g.get('title', ''),
                    cost=g.get('cost'), actual_cost=g.get('actual_cost'),
                    barcode=g.get('barcode'),
                    unit_short_title=g.get('unit_short_title'),
                    category_id=g.get('category_id'),
                    last_change_date=last_change_date,
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.title = g.get('title', '')
                obj.cost = g.get('cost')
                obj.actual_cost = g.get('actual_cost')
                obj.barcode = g.get('barcode')
                obj.unit_short_title = g.get('unit_short_title')
                obj.category_id = g.get('category_id')
                obj.last_change_date = last_change_date

        db.commit()
        print(f"  ✓ Товары сохранены ({len(goods)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 12. Записи (appointments) и транзакции (услуги внутри записи)
# ===================================================================

def sync_records(api: YClientsAPI, db, company_id: str,
                 start_date: str = None, end_date: str = None):
    print("\n── Записи (визиты) ──")

    records = api.get_records(company_id, start_date=start_date, end_date=end_date)
    if not records:
        print("  Нет записей за указанный период")
        return False

    print(f"  Найдено: {len(records)}")

    try:
        cid = int(company_id)
        tx_count = 0
        record_ids = [r.get('id') for r in records if r.get('id') is not None]
        existing_records = load_existing_map(db, Appointment, record_ids, Appointment.id)
        deleted_tx = bulk_delete_by_ids(db, Transaction, Transaction.appointment_id, record_ids)

        for r in records:
            record_id = r.get('id')
            if record_id is None:
                continue

            client_data = r.get('client') or {}
            client_id = client_data.get('id')
            staff_id = r.get('staff_id')
            created_user_id = r.get('created_user_id')

            obj = existing_records.get(record_id)
            if not obj:
                obj = Appointment(
                    id=record_id, company_id=cid,
                    staff_id=staff_id, client_id=client_id,
                    created_user_id=created_user_id,
                    date=parse_date(r.get('date')),
                    datetime=parse_datetime(r.get('datetime')),
                    create_date=parse_datetime(r.get('create_date')),
                    seance_length=r.get('seance_length'),
                    attendance=r.get('attendance', 0),
                    comment=r.get('comment'),
                )
                db.add(obj)
            else:
                obj.staff_id = staff_id
                obj.client_id = client_id
                obj.created_user_id = created_user_id
                obj.date = parse_date(r.get('date'))
                obj.datetime = parse_datetime(r.get('datetime'))
                obj.create_date = parse_datetime(r.get('create_date'))
                obj.seance_length = r.get('seance_length')
                obj.attendance = r.get('attendance', 0)
                obj.comment = r.get('comment')

            for svc in (r.get('services') or []):
                tx = Transaction(
                    appointment_id=record_id,
                    service_id=svc.get('id'),
                    service_title=svc.get('title', ''),
                    cost=svc.get('cost'),
                    first_cost=svc.get('first_cost'),
                    amount=svc.get('amount', 1),
                    company_id=cid,
                )
                db.add(tx)
                tx_count += 1

        db.commit()
        print(
            f"  ✓ Записи сохранены ({len(records)} шт.), "
            f"пересобрано транзакций: {tx_count}, удалено старых: {deleted_tx}"
        )
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 13. Финансовые транзакции
# ===================================================================

def sync_financial_transactions(api: YClientsAPI, db, company_id: str,
                                start_date: str = None, end_date: str = None):
    print("\n── Финансовые транзакции ──")

    txns = api.get_financial_transactions(company_id,
                                          start_date=start_date,
                                          end_date=end_date)
    if txns is None:
        print("  Источник недоступен")
        return False
    if not txns:
        print("  Нет данных")

    print(f"  Найдено: {len(txns)}")

    try:
        cid = int(company_id)
        existing_txns = load_existing_map(
            db,
            FinancialTransaction,
            (txn.get('id') for txn in txns),
            FinancialTransaction.id,
        )
        for t in txns:
            tid = t.get('id')
            if tid is None:
                continue
            obj = existing_txns.get(tid)

            account = t.get('account') or {}
            client = t.get('client') or {}
            master = t.get('master') or {}
            expense = t.get('expense') or {}
            expense_title = expense.get('title') if isinstance(expense, dict) else None

            if not obj:
                obj = FinancialTransaction(
                    id=tid,
                    document_id=t.get('document_id'),
                    expense_id=expense.get('id') if isinstance(expense, dict) else None,
                    expense_title=expense_title,
                    date=parse_datetime(t.get('date')),
                    amount=t.get('amount'),
                    comment=t.get('comment'),
                    account_id=account.get('id') if isinstance(account, dict) else None,
                    client_id=client.get('id') if isinstance(client, dict) else None,
                    master_id=master.get('id') if isinstance(master, dict) else None,
                    record_id=t.get('record_id'),
                    visit_id=t.get('visit_id'),
                    sold_item_id=t.get('sold_item_id'),
                    sold_item_type=t.get('sold_item_type'),
                    company_id=cid,
                )
                db.add(obj)
                existing_txns[tid] = obj
            else:
                obj.document_id = t.get('document_id')
                obj.expense_id = expense.get('id') if isinstance(expense, dict) else None
                obj.expense_title = expense_title
                obj.date = parse_datetime(t.get('date'))
                obj.amount = t.get('amount')
                obj.comment = t.get('comment')
                obj.account_id = account.get('id') if isinstance(account, dict) else None
                obj.client_id = client.get('id') if isinstance(client, dict) else None
                obj.master_id = master.get('id') if isinstance(master, dict) else None
                obj.record_id = t.get('record_id')
                obj.visit_id = t.get('visit_id')
                obj.sold_item_id = t.get('sold_item_id')
                obj.sold_item_type = t.get('sold_item_type')

        if start_date and end_date:
            state = db.get(
                SyncSourceState,
                {'company_id': cid, 'source': PERSONAL_ACCOUNT_SOURCE},
            )
            if state is None:
                state = SyncSourceState(
                    company_id=cid,
                    source=PERSONAL_ACCOUNT_SOURCE,
                    period_start=parse_date(start_date),
                    period_end=parse_date(end_date),
                    synced_at=datetime.now(),
                )
                db.add(state)
            else:
                state.period_start = min(state.period_start, parse_date(start_date))
                state.period_end = max(state.period_end, parse_date(end_date))
                state.synced_at = datetime.now()

        db.commit()
        print(f"  ✓ Финансовые транзакции сохранены ({len(txns)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 14. Товарные транзакции
# ===================================================================

def sync_goods_transactions(api: YClientsAPI, db, company_id: str,
                            start_date: str = None, end_date: str = None):
    print("\n── Товарные транзакции ──")

    txns = api.get_goods_transactions(company_id,
                                      start_date=start_date,
                                      end_date=end_date)
    if not txns:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(txns)}")

    try:
        cid = int(company_id)
        existing_txns = load_existing_map(
            db,
            GoodTransaction,
            (txn.get('id') for txn in txns),
            GoodTransaction.id,
        )
        for t in txns:
            tid = t.get('id')
            if tid is None:
                continue
            obj = existing_txns.get(tid)

            good = t.get('good') or {}
            storage = t.get('storage') or {}
            master = t.get('master') or {}
            client = t.get('client') or {}
            good_id = good.get('id') if isinstance(good, dict) else None
            good_title = good.get('title') if isinstance(good, dict) else None
            storage_id = storage.get('id') if isinstance(storage, dict) else None
            storage_title = storage.get('title') if isinstance(storage, dict) else None

            tx_date = parse_datetime(t.get('create_date') or t.get('date'))

            if not obj:
                obj = GoodTransaction(
                    id=tid,
                    document_id=t.get('document_id'),
                    type_id=t.get('type_id'),
                    good_id=good_id,
                    good_title=good_title,
                    storage_id=storage_id,
                    storage_title=storage_title,
                    amount=t.get('amount'),
                    cost_per_unit=t.get('cost_per_unit'),
                    cost=t.get('cost'),
                    discount=t.get('discount'),
                    master_id=master.get('id') if isinstance(master, dict) else None,
                    client_id=client.get('id') if isinstance(client, dict) else None,
                    company_id=cid,
                    date=tx_date,
                )
                db.add(obj)
                existing_txns[tid] = obj
            else:
                obj.document_id = t.get('document_id')
                obj.type_id = t.get('type_id')
                obj.good_id = good_id
                obj.good_title = good_title
                obj.storage_id = storage_id
                obj.storage_title = storage_title
                obj.amount = t.get('amount')
                obj.cost_per_unit = t.get('cost_per_unit')
                obj.cost = t.get('cost')
                obj.discount = t.get('discount')
                obj.master_id = master.get('id') if isinstance(master, dict) else None
                obj.client_id = client.get('id') if isinstance(client, dict) else None
                obj.date = tx_date

        db.commit()
        print(f"  ✓ Товарные транзакции сохранены ({len(txns)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 15. Комментарии / отзывы
# ===================================================================

def sync_comments(api: YClientsAPI, db, company_id: str,
                  start_date: str = None, end_date: str = None):
    print("\n── Комментарии / отзывы ──")

    comments = api.get_comments(company_id,
                                start_date=start_date,
                                end_date=end_date)
    if not comments:
        print("  Нет данных")
        return False

    print(f"  Найдено: {len(comments)}")

    try:
        cid = int(company_id)
        existing_comments = load_existing_map(
            db,
            Comment,
            (comment.get('id') for comment in comments),
            Comment.id,
        )
        for c in comments:
            cmt_id = c.get('id')
            if cmt_id is None:
                continue
            obj = existing_comments.get(cmt_id)
            if not obj:
                obj = Comment(
                    id=cmt_id,
                    type=c.get('type'),
                    master_id=c.get('master_id'),
                    text=c.get('text'),
                    date=parse_datetime(c.get('date')),
                    rating=c.get('rating'),
                    user_id=c.get('user_id'),
                    user_name=c.get('user_name'),
                    record_id=c.get('record_id'),
                    company_id=cid,
                )
                db.add(obj)
            else:
                obj.type = c.get('type')
                obj.text = c.get('text')
                obj.date = parse_datetime(c.get('date'))
                obj.rating = c.get('rating')

        db.commit()
        print(f"  ✓ Комментарии сохранены ({len(comments)} шт.)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 20. Графики работы сотрудников
# ===================================================================

def sync_staff_schedules(api: YClientsAPI, db, company_id: str,
                         start_date: str = None, end_date: str = None):
    print("\n── Графики работы сотрудников ──")

    schedules = api.get_staff_schedule(company_id,
                                       start_date=start_date,
                                       end_date=end_date)
    if not schedules:
        print("  Нет данных")
        return False

    print(f"  Найдено записей расписания: {len(schedules)}")

    try:
        cid = int(company_id)
        delete_query = db.query(StaffSchedule).filter(StaffSchedule.company_id == cid)
        if start_date:
            delete_query = delete_query.filter(StaffSchedule.date >= parse_date(start_date))
        if end_date:
            delete_query = delete_query.filter(StaffSchedule.date <= parse_date(end_date))
        deleted_slots = delete_query.delete(synchronize_session=False)

        slot_count = 0
        for entry in schedules:
            staff_id = entry.get('staff_id')
            schedule_date = entry.get('date')
            slots = entry.get('slots') or []
            for slot in slots:
                obj = StaffSchedule(
                    staff_id=staff_id,
                    date=parse_date(schedule_date),
                    slot_from=parse_time(slot.get('from')),
                    slot_to=parse_time(slot.get('to')),
                    company_id=cid,
                )
                db.add(obj)
                slot_count += 1

        db.commit()
        print(f"  ✓ Графики сохранены ({slot_count} слотов), удалено старых: {deleted_slots}")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 23. Аналитика: основные показатели
# ===================================================================

def sync_analytics_overall(api: YClientsAPI, db, company_id: str,
                           date_from: str, date_to: str):
    print("\n── Аналитика: основные показатели ──")

    data = api.get_analytics_overall(company_id, date_from, date_to)
    if not data:
        print("  Нет данных")
        return False

    try:
        cid = int(company_id)
        db.query(AnalyticsOverall).filter(AnalyticsOverall.company_id == cid).delete()

        def _parse_stat(stat_key):
            s = data.get(stat_key) or {}
            return s

        inc_total = _parse_stat('income_total_stats')
        inc_svc   = _parse_stat('income_services_stats')
        inc_goods = _parse_stat('income_goods_stats')
        inc_avg   = _parse_stat('income_average_stats')
        inc_avg_s = _parse_stat('income_average_services_stats')
        fullness  = _parse_stat('fullness_stats')
        rec       = _parse_stat('record_stats')
        cli       = _parse_stat('client_stats')

        def _f(val):
            if val is None:
                return None
            try:
                return float(val)
            except (ValueError, TypeError):
                return None

        obj = AnalyticsOverall(
            date_from=parse_date(date_from),
            date_to=parse_date(date_to),
            fetched_at=datetime.now(),
            income_total=_f(inc_total.get('current_sum')),
            income_total_prev=_f(inc_total.get('previous_sum')),
            income_total_change=_f(inc_total.get('change_percent')),
            income_services=_f(inc_svc.get('current_sum')),
            income_services_prev=_f(inc_svc.get('previous_sum')),
            income_goods=_f(inc_goods.get('current_sum')),
            income_goods_prev=_f(inc_goods.get('previous_sum')),
            income_average=_f(inc_avg.get('current_sum')),
            income_average_prev=_f(inc_avg.get('previous_sum')),
            income_average_services=_f(inc_avg_s.get('current_sum')),
            income_average_services_prev=_f(inc_avg_s.get('previous_sum')),
            fullness_current=_f(fullness.get('current_percent')),
            fullness_previous=_f(fullness.get('previous_percent')),
            fullness_change=_f(fullness.get('change_percent')),
            records_completed=rec.get('current_completed_count'),
            records_pending=rec.get('current_pending_count'),
            records_canceled=rec.get('current_canceled_count'),
            records_total=rec.get('current_total_count'),
            records_total_prev=rec.get('previous_total_count'),
            records_change=_f(rec.get('change_percent')),
            clients_total=cli.get('total_count'),
            clients_new=cli.get('new_count'),
            clients_new_percent=_f(cli.get('new_percent')),
            clients_return=cli.get('return_count'),
            clients_return_percent=_f(cli.get('return_percent')),
            clients_active=cli.get('active_count'),
            clients_lost=cli.get('lost_count'),
            clients_lost_percent=_f(cli.get('lost_percent')),
            company_id=cid,
        )
        db.add(obj)
        db.commit()
        print("  ✓ Основные показатели сохранены")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 24. Аналитика: дневные графики (выручка, записи, заполненность)
# ===================================================================

def sync_analytics_daily_charts(api: YClientsAPI, db, company_id: str,
                                date_from: str, date_to: str):
    print("\n── Аналитика: дневные графики ──")

    charts = {
        'income': api.get_analytics_income_daily,
        'records': api.get_analytics_records_daily,
        'fullness': api.get_analytics_fullness_daily,
    }

    cid = int(company_id)
    total = 0

    try:
        db.query(AnalyticsDailyMetric).filter(
            AnalyticsDailyMetric.company_id == cid
        ).delete()

        for metric_type, getter in charts.items():
            series_list = getter(company_id, date_from, date_to)
            if not series_list:
                print(f"  {metric_type}: нет данных")
                continue

            for series in series_list:
                label = series.get('label', metric_type)
                points = series.get('data', [])
                for point in points:
                    if not isinstance(point, (list, tuple)) or len(point) < 2:
                        continue
                    ts_ms, value = point[0], point[1]
                    day_str = datetime.utcfromtimestamp(ts_ms / 1000).strftime('%Y-%m-%d')
                    db.add(AnalyticsDailyMetric(
                        date=parse_date(day_str),
                        metric_type=metric_type,
                        label=label,
                        value=value,
                        company_id=cid,
                    ))
                    total += 1

            print(f"  {metric_type}: ок")

        db.commit()
        print(f"  ✓ Дневные метрики сохранены ({total} точек)")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 25. Аналитика: источники и статусы записей
# ===================================================================

def sync_analytics_sources_and_statuses(api: YClientsAPI, db, company_id: str,
                                        date_from: str, date_to: str):
    print("\n── Аналитика: источники и статусы ──")

    cid = int(company_id)

    try:
        db.query(AnalyticsSourceMetric).filter(
            AnalyticsSourceMetric.company_id == cid
        ).delete()
        db.query(AnalyticsStatusMetric).filter(
            AnalyticsStatusMetric.company_id == cid
        ).delete()

        sources = api.get_analytics_record_source(company_id, date_from, date_to)
        if sources:
            for s in sources:
                db.add(AnalyticsSourceMetric(
                    date_from=parse_date(date_from),
                    date_to=parse_date(date_to),
                    label=s.get('label', ''),
                    value=s.get('data'),
                    company_id=cid,
                ))
            print(f"  Источники: {len(sources)} шт.")
        else:
            print("  Источники: нет данных")

        statuses = api.get_analytics_record_status(company_id, date_from, date_to)
        if statuses:
            for s in statuses:
                db.add(AnalyticsStatusMetric(
                    date_from=parse_date(date_from),
                    date_to=parse_date(date_to),
                    label=s.get('label', ''),
                    value=s.get('data'),
                    company_id=cid,
                ))
            print(f"  Статусы: {len(statuses)} шт.")
        else:
            print("  Статусы: нет данных")

        db.commit()
        print("  ✓ Источники и статусы сохранены")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# 26. Z-Отчёт
# ===================================================================

def sync_z_report(api: YClientsAPI, db, company_id: str, report_date: str):
    print(f"\n── Z-Отчёт ({report_date}) ──")

    data = api.get_z_report(company_id, report_date)
    if not data:
        print("  Нет данных")
        return False

    try:
        cid = int(company_id)
        report_bound = parse_date(report_date)
        db.query(ZReport).filter(
            ZReport.company_id == cid, ZReport.report_date == report_bound
        ).delete()
        db.query(ZReportPayment).filter(
            ZReportPayment.company_id == cid, ZReportPayment.report_date == report_bound
        ).delete()

        stats = data.get('stats') or {}
        paids = data.get('paids') or {}
        total_paid = paids.get('total') or {}

        obj = ZReport(
            report_date=report_bound,
            clients=stats.get('clients'),
            clients_average=stats.get('clients_average'),
            records=stats.get('records'),
            records_average=stats.get('records_average'),
            visit_records=stats.get('visit_records'),
            visit_records_average=stats.get('visit_records_average'),
            non_visit_records=stats.get('non_visit_records'),
            non_visit_records_average=stats.get('non_visit_records_average'),
            targets=stats.get('targets'),
            targets_paid=stats.get('targets_paid'),
            goods_count=stats.get('goods'),
            goods_paid=stats.get('goods_paid'),
            certificates_count=stats.get('certificates'),
            certificates_paid=stats.get('certificates_paid'),
            abonement_count=stats.get('abonement'),
            abonement_paid=stats.get('abonement_paid'),
            total_accounts=total_paid.get('accounts'),
            total_discount=total_paid.get('discount'),
            currency=data.get('currency'),
            company_id=cid,
        )
        db.add(obj)

        for acc in (paids.get('accounts') or []):
            db.add(ZReportPayment(
                report_date=report_bound,
                payment_group='account',
                title=acc.get('title'),
                amount=acc.get('amount'),
                company_id=cid,
            ))

        for disc in (paids.get('discount') or []):
            db.add(ZReportPayment(
                report_date=report_bound,
                payment_group='discount',
                title=disc.get('title'),
                amount=disc.get('amount'),
                company_id=cid,
            ))

        db.commit()
        print("  ✓ Z-Отчёт сохранён")
        return True

    except Exception as e:
        db.rollback()
        print(f"  ✗ Ошибка: {e}")
        return False


# ===================================================================
# Основная функция
# ===================================================================

def execute_sync(
    mode: str = 'incremental',
    end_date: date | None = None,
    *,
    portal_account_id: int | None = None,
    credential_id: int | None = None,
    company_ids: Iterable[int] | None = None,
    progress_callback=None,
):
    print("=" * 60)
    print("  YClients → PostgreSQL: синхронизация")
    print("=" * 60)

    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    empty_result = {
        'completed': False,
        'success': False,
        'step_results': [],
        'mode': mode,
        'window_start': None,
        'window_end': None,
        'companies_count': 0,
    }

    if not database.test_connection():
        return empty_result

    db = database.get_db()
    step_results: list[dict] = []
    overall_success = False
    companies_count = 0

    end = end_date or date.today()
    start, sync_mode = resolve_sync_window(db, end, mode)
    sd = start.isoformat()
    ed = end.isoformat()
    schedule_end = end + timedelta(days=SCHEDULE_DAYS)
    analytics_start = end - timedelta(days=ANALYTICS_DAYS)
    analytics_sd = analytics_start.isoformat()
    checkpoint_step_names = {
        'Записи',
        'Финансовые транзакции',
        'Товарные транзакции',
        'Комментарии',
    }

    requested_company_ids = [int(item) for item in dict.fromkeys(company_ids or [])]

    def emit_progress(
        stage_key: str,
        status: str = 'info',
        progress_pct: int | None = None,
        message: str | None = None,
        *,
        credential: YClientsCredentialValue | None = None,
        company_id: int | None = None,
        payload: dict | None = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback({
            'portal_account_id': credential.portal_account_id if credential else portal_account_id,
            'credential_id': credential.id if credential else credential_id,
            'company_id': company_id,
            'stage_key': stage_key,
            'status': status,
            'progress_pct': progress_pct,
            'message': message,
            'payload': payload or {},
            'step_results': list(step_results),
        })

    try:
        credentials = load_active_credentials_sync(db, portal_account_id=portal_account_id)
        if credential_id is not None:
            credentials = [credential for credential in credentials if credential.id == int(credential_id)]
        if not credentials:
            print('! Нет активных учётных данных YClients. Добавьте их через личный кабинет.')
            emit_progress('credentials', 'warning', 100, 'No active YClients credentials')
            return {
                **empty_result,
                'completed': True,
                'mode': sync_mode,
                'window_start': sd,
                'window_end': ed,
            }

        print(
            "Параметры: "
            f"SYNC_DAYS={SYNC_DAYS}, "
            f"SCHEDULE_DAYS={SCHEDULE_DAYS}, "
            f"ANALYTICS_DAYS={ANALYTICS_DAYS}, "
            f"REQUEST_DELAY={YCLIENTS_REQUEST_DELAY}, "
            f"TIMEOUT={YCLIENTS_TIMEOUT}"
        )
        print(
            f"Режим синхронизации транзакций: {sync_mode} "
            f"({sd} .. {ed}, lookback={SYNC_LOOKBACK_DAYS} дн.)"
        )
        print(f"Активных учётных данных: {len(credentials)}")
        emit_progress(
            'credentials',
            'success',
            5,
            f'Active credentials: {len(credentials)}',
            payload={'credentials_count': len(credentials)},
        )

        company_steps = [
            ("Категории услуг", sync_service_categories, {}),
            ("Услуги", sync_services, {}),
            ("Должности", sync_positions, {}),
            ("Сотрудники", sync_staff, {}),
            ("Клиенты", sync_clients, {}),
            ("Кассы", sync_accounts, {}),
            ("Склады", sync_storages, {}),
            ("Категории товаров", sync_good_categories, {}),
            ("Товары", sync_goods, {}),
            ("Записи", sync_records, {'start_date': sd, 'end_date': schedule_end.isoformat()}),
            (
                "Финансовые транзакции",
                sync_financial_transactions,
                {'start_date': sd, 'end_date': ed},
            ),
            (
                "Товарные транзакции",
                sync_goods_transactions,
                {'start_date': sd, 'end_date': ed},
            ),
            ("Комментарии", sync_comments, {'start_date': sd, 'end_date': ed}),
            (
                "Графики сотрудников",
                sync_staff_schedules,
                {'start_date': ed, 'end_date': schedule_end.isoformat()},
            ),
        ]
        analytics_steps = [
            ("Аналитика overall", sync_analytics_overall, {'date_from': analytics_sd, 'date_to': ed}),
            ("Аналитика daily", sync_analytics_daily_charts, {'date_from': analytics_sd, 'date_to': ed}),
            (
                "Аналитика source/status",
                sync_analytics_sources_and_statuses,
                {'date_from': analytics_sd, 'date_to': ed},
            ),
            ("Z-Отчёт", sync_z_report, {'report_date': ed}),
        ]

        for credential in credentials:
            tenant_label = f"#{credential.portal_account_id} · {credential.title}"
            print(f"\n{'=' * 60}")
            print(f"  Тенант {tenant_label}")
            print(f"{'=' * 60}")

            try:
                company_api = _build_api_for_credential(credential)
            except Exception as exc:
                mark_credential_failure_sync(db, credential.id, exc.__class__.__name__)
                emit_progress(
                    'credentials_auth',
                    'failed',
                    10,
                    f'Credential auth failed: {credential.title}',
                    credential=credential,
                    payload={'error': str(exc)[:1000]},
                )
                raise
            if company_api is None:
                print(f"! Не удалось авторизовать учётные данные «{credential.title}» — пропуск тенанта")
                mark_credential_failure_sync(db, credential.id, 'YClients authentication failed')
                step_results.append({
                    'name': f"Credentials auth [{tenant_label}]",
                    'key': 'credentials_auth',
                    'success': False,
                    'elapsed': 0.0,
                })
                emit_progress(
                    'credentials_auth',
                    'failed',
                    10,
                    f'Credential auth failed: {credential.title}',
                    credential=credential,
                )
                continue
            mark_credential_success_sync(db, credential.id)
            emit_progress(
                'credentials_auth',
                'success',
                10,
                f'Credential auth succeeded: {credential.title}',
                credential=credential,
            )

            run_sync_step(
                step_results,
                f"Сети и компании [{tenant_label}]",
                sync_groups_and_companies,
                company_api,
                db,
                step_key='Сети и компании',
                progress_callback=progress_callback,
                progress_context={
                    'portal_account_id': credential.portal_account_id,
                    'credential_id': credential.id,
                },
                progress_pct=15,
            )

            credential_company_ids = list(credential.company_ids)
            if requested_company_ids:
                if credential_company_ids:
                    scoped_company_ids = sorted(set(requested_company_ids) & set(credential_company_ids))
                else:
                    scoped_company_ids = requested_company_ids
            else:
                scoped_company_ids = credential_company_ids
            target_companies = get_target_companies(db, scoped_company_ids or None)
            if not target_companies:
                print(f"! Тенант {tenant_label}: нет филиалов для синхронизации")
                emit_progress(
                    'companies',
                    'warning',
                    20,
                    f'No companies for credential: {credential.title}',
                    credential=credential,
                )
                continue

            print(f"✓ Тенант {tenant_label}: найдено филиалов {len(target_companies)}")
            companies_count += len(target_companies)
            emit_progress(
                'companies',
                'success',
                20,
                f'Companies selected: {len(target_companies)}',
                credential=credential,
                payload={'company_ids': [company.id for company in target_companies]},
            )

            for company in target_companies:
                company_id = str(company.id)
                company_label = format_company_label(company)
                print(f"\n{'─' * 60}")
                print(f"Филиал: {company_label}")
                print(f"{'─' * 60}")

                if sync_mode == 'full':
                    purge_full_refresh_window(
                        db,
                        company.id,
                        sd,
                        ed,
                        schedule_end.isoformat(),
                    )

                for name, fn, kwargs in company_steps:
                    run_sync_step(
                        step_results,
                        f"{name} [{company_label}]",
                        fn,
                        company_api,
                        db,
                        company_id,
                        step_key=name,
                        progress_callback=progress_callback,
                        progress_context={
                            'portal_account_id': credential.portal_account_id,
                            'credential_id': credential.id,
                            'company_id': company.id,
                        },
                        progress_pct=50,
                        **kwargs,
                    )

                print(f"\n── Аналитика: период {analytics_sd} .. {ed} ({ANALYTICS_DAYS} дней) [{company_label}] ──")
                for name, fn, kwargs in analytics_steps:
                    run_sync_step(
                        step_results,
                        f"{name} [{company_label}]",
                        fn,
                        company_api,
                        db,
                        company_id,
                        step_key=name,
                        progress_callback=progress_callback,
                        progress_context={
                            'portal_account_id': credential.portal_account_id,
                            'credential_id': credential.id,
                            'company_id': company.id,
                        },
                        progress_pct=85,
                        **kwargs,
                    )

        if steps_successful(step_results, checkpoint_step_names):
            set_sync_state_value(db, TRANSACTIONAL_STATE_KEY, ed)
            print(f"✓ Обновлено состояние инкрементальной синхронизации: {ed}")
        else:
            print("! Состояние инкрементальной синхронизации не обновлено")

        overall_success = True

        print("\n" + "=" * 60)
        print("  Синхронизация завершена!")
        print("=" * 60)

    finally:
        print_sync_summary(step_results)
        db.close()

    return {
        'completed': True,
        'success': overall_success,
        'step_results': step_results,
        'mode': sync_mode,
        'window_start': sd,
        'window_end': ed,
        'companies_count': companies_count,
    }


def main():
    execute_sync(mode='incremental')


if __name__ == "__main__":
    start_time = time.time()
    print(f"▶ Начало выполнения: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)

    main()

    elapsed = time.time() - start_time
    minutes, seconds = divmod(int(elapsed), 60)
    print(f"\n▶ Конец выполнения:  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"⏱ Общее время: {minutes} мин {seconds} сек")
