"""
API server for exposing YClients BI data and queued sync controls.
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    API_HOST,
    API_KEY,
    API_PORT,
    DASHBOARD_CORS_ORIGIN_REGEX,
    DASHBOARD_CORS_ORIGINS,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    SYNC_API_TOKEN,
)
from dashboard_routes import router as dashboard_router
from database import get_async_db, init_async_database
from models import (
    Account,
    Appointment,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    Good,
    GoodCatalog,
    GoodCategory,
    GoodCategoryCatalog,
    GoodTransaction,
    Group,
    AccountCatalog,
    Service,
    ServiceCatalog,
    ServiceCategory,
    ServiceCategoryCatalog,
    Staff,
    StaffPosition,
    StaffPositionCatalog,
    StaffSchedule,
    Storage,
    StorageCatalog,
    Transaction,
)
from sync_jobs import SyncJobService
from sync_orchestrator import get_sync_status
from sync_parsing import parse_date, parse_datetime_end, parse_datetime_start

init_async_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)

MAX_PAGE_SIZE = 5000
DEFAULT_PAGE_SIZE = 1000

OPEN_PATHS = {"/health", "/openapi.json", "/docs", "/redoc"}


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)):
    """Global auth: skip for health/docs, enforce when API_KEY is set."""
    if request.url.path in OPEN_PATHS:
        return
    if not API_KEY:
        return
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


app = FastAPI(
    title="YClients BI System API",
    description="API для получения данных YClients в табличном формате",
    version="5.0.0",
    dependencies=[Depends(require_api_key)],
)

_cors_origins = [o.strip() for o in DASHBOARD_CORS_ORIGINS.split(',') if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_origin_regex=DASHBOARD_CORS_ORIGIN_REGEX or None,
        allow_credentials=True,
        allow_methods=['*'],
        allow_headers=['*'],
        allow_private_network=True,
    )

app.include_router(dashboard_router, prefix='/dashboard', tags=['dashboard'])


def require_sync_token(x_sync_token: str | None = Header(default=None)):
    if not SYNC_API_TOKEN:
        return
    if x_sync_token != SYNC_API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid sync token")


def serialize_value(value: Any) -> Any:
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def page_params(
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    offset: int = Query(0, ge=0),
) -> tuple[int, int]:
    return limit, offset


def build_page_response(total: int, limit: int, offset: int, data: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        'total': total,
        'limit': limit,
        'offset': offset,
        'data': data,
    }


def serialize_rows(rows: list[Any], serializer: Callable[[Any], dict[str, Any]]) -> list[dict[str, Any]]:
    return [serializer(row) for row in rows]


async def fetch_page(db: AsyncSession, stmt, limit: int, offset: int) -> tuple[int, list[Any]]:
    count_result = await db.execute(select(func.count()).select_from(stmt.order_by(None).subquery()))
    total = count_result.scalar_one()
    result = await db.execute(stmt.offset(offset).limit(limit))
    items = list(result.scalars().all())
    return total, items


@app.get("/")
async def root():
    return {
        "message": "YClients BI System API",
        "endpoints": {
            "/groups": "Сети",
            "/companies": "Компании",
            "/service_categories": "Категории услуг",
            "/services": "Услуги",
            "/staff_positions": "Должности",
            "/staff": "Сотрудники",
            "/clients": "Клиенты",
            "/accounts": "Кассы",
            "/storages": "Склады",
            "/good_categories": "Категории товаров",
            "/goods": "Товары",
            "/appointments": "Записи (визиты)",
            "/transactions": "Услуги внутри записей",
            "/financial_transactions": "Финансовые транзакции",
            "/goods_transactions": "Товарные транзакции",
            "/comments": "Комментарии / отзывы",
            "/staff_schedules": "Графики работы",
            "/stats": "Общая статистика",
            "/sync/trigger": "Поставить sync в очередь",
            "/sync/status": "Статус sync и очереди",
            "/export/csv/{table}": "Экспорт таблицы в CSV",
            "/dashboard/branches": "Филиалы (компании) для портала",
            "/dashboard/staff_directory.csv": "CSV справочник сотрудников для Google Sheets",
            "/dashboard/bundle": "Сводка дашборда за период (JSON)",
            "/dashboard/widget/plan_fact": "План/факт по филиалам за период",
            "/dashboard/plan/reviews_fact": "Ручной факт по отзывам администраторов",
            "/dashboard/plan/sync": "Импорт плана из Google Sheets CSV",
            "/dashboard/widget/sync_status": "Статус синка для UI",
        },
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/groups")
async def api_groups(
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Group).order_by(Group.id.asc())
    total, groups = await fetch_page(db, stmt, limit, offset)
    data = []
    for group in groups:
        count_result = await db.execute(
            select(func.count()).where(Company.group_id == group.id)
        )
        data.append({
            "id": group.id,
            "title": group.title,
            "companies_count": count_result.scalar_one(),
        })
    return build_page_response(total, limit, offset, data)


@app.get("/companies")
async def api_companies(
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Company)
    if group_id is not None:
        stmt = stmt.where(Company.group_id == group_id)
    stmt = stmt.order_by(Company.id.asc())
    total, companies = await fetch_page(db, stmt, limit, offset)
    data = [{"id": c.id, "title": c.title, "group_id": c.group_id} for c in companies]
    return build_page_response(total, limit, offset, data)


@app.get("/service_categories")
async def api_service_categories(
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(ServiceCategoryCatalog)
    if company_id is not None:
        stmt = stmt.where(ServiceCategoryCatalog.company_id == company_id)
    stmt = stmt.order_by(ServiceCategoryCatalog.category_id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.category_id,
        "title": item.title,
        "weight": item.weight,
        "api_id": item.api_id,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/services")
async def api_services(
    company_id: Optional[int] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(ServiceCatalog)
    if company_id is not None:
        stmt = stmt.where(ServiceCatalog.company_id == company_id)
    if category:
        stmt = stmt.where(ServiceCatalog.category_title == category)
    if min_price is not None:
        stmt = stmt.where(ServiceCatalog.price_min >= min_price)
    if max_price is not None:
        stmt = stmt.where(ServiceCatalog.price_min <= max_price)
    stmt = stmt.order_by(ServiceCatalog.service_id.asc())
    total, services = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(services, lambda item: {
        "id": item.service_id,
        "title": item.title,
        "price_min": item.price_min,
        "duration_sec": item.duration,
        "duration_min": round(item.duration / 60, 1) if item.duration else None,
        "category": item.category_title,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/staff_positions")
async def api_staff_positions(
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(StaffPositionCatalog)
    if company_id is not None:
        stmt = stmt.where(StaffPositionCatalog.company_id == company_id)
    stmt = stmt.order_by(StaffPositionCatalog.position_id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.position_id,
        "title": item.title,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/staff")
async def api_staff(
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Staff)
    if company_id is not None:
        stmt = stmt.where(Staff.company_id == company_id)
    stmt = stmt.order_by(Staff.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "name": item.name,
        "specialization": item.specialization,
        "position": item.position,
        "rating": item.rating,
        "votes_count": item.votes_count,
        "bookable": item.bookable,
        "fired": item.fired,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/clients")
async def api_clients(
    company_id: Optional[int] = None,
    min_visits: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Client)
    if company_id is not None:
        stmt = stmt.where(Client.company_id == company_id)
    if min_visits is not None:
        stmt = stmt.where(Client.visits_count >= min_visits)
    stmt = stmt.order_by(Client.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "name": item.name,
        "phone": item.phone,
        "email": item.email,
        "birth_date": serialize_value(item.birth_date),
        "visits_count": item.visits_count,
        "last_visit_date": serialize_value(item.last_visit_date),
        "discount": item.discount,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/accounts")
async def api_accounts(
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(AccountCatalog)
    if company_id is not None:
        stmt = stmt.where(AccountCatalog.company_id == company_id)
    stmt = stmt.order_by(AccountCatalog.account_id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.account_id,
        "title": item.title,
        "type": item.type,
        "comment": item.comment,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/storages")
async def api_storages(
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(StorageCatalog)
    if company_id is not None:
        stmt = stmt.where(StorageCatalog.company_id == company_id)
    stmt = stmt.order_by(StorageCatalog.storage_id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.storage_id,
        "title": item.title,
        "for_services": item.for_services,
        "for_sale": item.for_sale,
        "comment": item.comment,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/good_categories")
async def api_good_categories(
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(GoodCategoryCatalog)
    if company_id is not None:
        stmt = stmt.where(GoodCategoryCatalog.company_id == company_id)
    stmt = stmt.order_by(GoodCategoryCatalog.category_id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.category_id,
        "title": item.title,
        "parent_category_id": item.parent_category_id,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/goods")
async def api_goods(
    company_id: Optional[int] = None,
    category_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(GoodCatalog)
    if company_id is not None:
        stmt = stmt.where(GoodCatalog.company_id == company_id)
    if category_id is not None:
        stmt = stmt.where(GoodCatalog.category_id == category_id)
    stmt = stmt.order_by(GoodCatalog.good_id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "good_id": item.good_id,
        "title": item.title,
        "cost": item.cost,
        "actual_cost": item.actual_cost,
        "barcode": item.barcode,
        "unit": item.unit_short_title,
        "category_id": item.category_id,
        "last_change_date": serialize_value(item.last_change_date),
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/appointments")
async def api_appointments(
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    client_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Appointment)
    if company_id is not None:
        stmt = stmt.where(Appointment.company_id == company_id)
    if staff_id is not None:
        stmt = stmt.where(Appointment.staff_id == staff_id)
    if client_id is not None:
        stmt = stmt.where(Appointment.client_id == client_id)
    if date_from:
        stmt = stmt.where(Appointment.date >= parse_date(date_from))
    if date_to:
        stmt = stmt.where(Appointment.date <= parse_date(date_to))
    stmt = stmt.order_by(Appointment.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "company_id": item.company_id,
        "staff_id": item.staff_id,
        "client_id": item.client_id,
        "date": serialize_value(item.date),
        "datetime": serialize_value(item.datetime),
        "create_date": serialize_value(item.create_date),
        "seance_length": item.seance_length,
        "attendance": item.attendance,
        "comment": item.comment,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/transactions")
async def api_transactions(
    company_id: Optional[int] = None,
    appointment_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Transaction)
    if company_id is not None:
        stmt = stmt.where(Transaction.company_id == company_id)
    if appointment_id is not None:
        stmt = stmt.where(Transaction.appointment_id == appointment_id)
    stmt = stmt.order_by(Transaction.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "appointment_id": item.appointment_id,
        "service_id": item.service_id,
        "service_title": item.service_title,
        "cost": item.cost,
        "first_cost": item.first_cost,
        "amount": item.amount,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/financial_transactions")
async def api_financial_transactions(
    company_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(FinancialTransaction)
    if company_id is not None:
        stmt = stmt.where(FinancialTransaction.company_id == company_id)
    if date_from:
        stmt = stmt.where(FinancialTransaction.date >= parse_datetime_start(date_from))
    if date_to:
        stmt = stmt.where(FinancialTransaction.date <= parse_datetime_end(date_to))
    stmt = stmt.order_by(FinancialTransaction.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "document_id": item.document_id,
        "expense_id": item.expense_id,
        "date": serialize_value(item.date),
        "amount": item.amount,
        "comment": item.comment,
        "account_id": item.account_id,
        "client_id": item.client_id,
        "master_id": item.master_id,
        "record_id": item.record_id,
        "visit_id": item.visit_id,
        "sold_item_id": item.sold_item_id,
        "sold_item_type": item.sold_item_type,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/goods_transactions")
async def api_goods_transactions(
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(GoodTransaction)
    if company_id is not None:
        stmt = stmt.where(GoodTransaction.company_id == company_id)
    stmt = stmt.order_by(GoodTransaction.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "document_id": item.document_id,
        "type_id": item.type_id,
        "good_id": item.good_id,
        "good_title": item.good_title,
        "storage_id": item.storage_id,
        "storage_title": item.storage_title,
        "amount": item.amount,
        "cost_per_unit": item.cost_per_unit,
        "cost": item.cost,
        "discount": item.discount,
        "master_id": item.master_id,
        "client_id": item.client_id,
        "company_id": item.company_id,
        "date": item.date.isoformat() if item.date else None,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/comments")
async def api_comments(
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    min_rating: Optional[float] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Comment)
    if company_id is not None:
        stmt = stmt.where(Comment.company_id == company_id)
    if staff_id is not None:
        stmt = stmt.where(Comment.master_id == staff_id)
    if min_rating is not None:
        stmt = stmt.where(Comment.rating >= min_rating)
    if date_from:
        stmt = stmt.where(Comment.date >= parse_datetime_start(date_from))
    if date_to:
        stmt = stmt.where(Comment.date <= parse_datetime_end(date_to))
    stmt = stmt.order_by(Comment.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "type": item.type,
        "master_id": item.master_id,
        "text": item.text,
        "date": serialize_value(item.date),
        "rating": item.rating,
        "user_id": item.user_id,
        "user_name": item.user_name,
        "record_id": item.record_id,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/staff_schedules")
async def api_staff_schedules(
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(StaffSchedule)
    if company_id is not None:
        stmt = stmt.where(StaffSchedule.company_id == company_id)
    if staff_id is not None:
        stmt = stmt.where(StaffSchedule.staff_id == staff_id)
    if date_from:
        stmt = stmt.where(StaffSchedule.date >= parse_date(date_from))
    if date_to:
        stmt = stmt.where(StaffSchedule.date <= parse_date(date_to))
    stmt = stmt.order_by(StaffSchedule.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "id": item.id,
        "staff_id": item.staff_id,
        "date": serialize_value(item.date),
        "slot_from": serialize_value(item.slot_from),
        "slot_to": serialize_value(item.slot_to),
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/stats")
async def api_stats(db: AsyncSession = Depends(get_async_db)):
    revenue_result = await db.execute(
        select(func.sum(Transaction.cost * Transaction.amount))
    )
    revenue = revenue_result.scalar_one_or_none() or 0

    fin_result = await db.execute(
        select(func.sum(FinancialTransaction.amount)).where(FinancialTransaction.amount > 0)
    )
    fin_income = fin_result.scalar_one_or_none() or 0

    async def count_of(model):
        r = await db.execute(select(func.count()).select_from(model))
        return r.scalar_one()

    attended_result = await db.execute(
        select(func.count()).where(Appointment.attendance > 0)
    )
    appointments_total = await count_of(Appointment)

    return {
        "groups": await count_of(Group),
        "companies": await count_of(Company),
        "service_categories": await count_of(ServiceCategory),
        "service_category_catalog": await count_of(ServiceCategoryCatalog),
        "services": await count_of(Service),
        "service_catalog": await count_of(ServiceCatalog),
        "staff_positions": await count_of(StaffPosition),
        "staff_position_catalog": await count_of(StaffPositionCatalog),
        "staff": await count_of(Staff),
        "clients": await count_of(Client),
        "accounts": await count_of(Account),
        "account_catalog": await count_of(AccountCatalog),
        "storages": await count_of(Storage),
        "storage_catalog": await count_of(StorageCatalog),
        "good_categories": await count_of(GoodCategory),
        "good_category_catalog": await count_of(GoodCategoryCatalog),
        "goods": await count_of(Good),
        "good_catalog": await count_of(GoodCatalog),
        "appointments": appointments_total,
        "appointments_attended": attended_result.scalar_one(),
        "transactions": await count_of(Transaction),
        "financial_transactions": await count_of(FinancialTransaction),
        "goods_transactions": await count_of(GoodTransaction),
        "comments": await count_of(Comment),
        "staff_schedule_slots": await count_of(StaffSchedule),
        "total_revenue": round(revenue, 2),
        "financial_income": round(fin_income, 2),
    }


class SyncTriggerRequest(BaseModel):
    mode: Literal['incremental', 'full'] = 'incremental'
    initiator: str = 'dashboard'


@app.post("/sync/trigger")
async def trigger_sync(
    payload: SyncTriggerRequest,
    _: None = Depends(require_sync_token),
    db: AsyncSession = Depends(get_async_db),
):
    job = await SyncJobService().async_enqueue_job(db, payload.mode, payload.initiator)
    return {
        "status": "queued",
        "job_id": job.id,
        "mode": job.mode,
        "initiator": job.initiator,
    }


@app.get("/sync/status")
async def sync_status(
    _: None = Depends(require_sync_token),
    db: AsyncSession = Depends(get_async_db),
):
    return {
        "sync": get_sync_status(),
        "queue": await SyncJobService().async_get_status_payload(db),
    }


TABLE_MAP = {
    "groups": Group,
    "companies": Company,
    "service_categories": ServiceCategory,
    "service_category_catalog": ServiceCategoryCatalog,
    "services": Service,
    "service_catalog": ServiceCatalog,
    "staff_positions": StaffPosition,
    "staff_position_catalog": StaffPositionCatalog,
    "staff": Staff,
    "clients": Client,
    "accounts": Account,
    "account_catalog": AccountCatalog,
    "storages": Storage,
    "storage_catalog": StorageCatalog,
    "good_categories": GoodCategory,
    "good_category_catalog": GoodCategoryCatalog,
    "goods": Good,
    "good_catalog": GoodCatalog,
    "appointments": Appointment,
    "transactions": Transaction,
    "financial_transactions": FinancialTransaction,
    "goods_transactions": GoodTransaction,
    "comments": Comment,
    "staff_schedules": StaffSchedule,
}


async def async_stream_csv_rows(db: AsyncSession, model):
    columns = [column.key for column in model.__table__.columns]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    stmt = select(model).order_by(*model.__table__.primary_key.columns)
    result = await db.stream(stmt)
    async for row in result.scalars():
        writer.writerow([serialize_value(getattr(row, column)) for column in columns])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


@app.get("/export/csv/{table_name}")
async def export_csv(table_name: str, db: AsyncSession = Depends(get_async_db)):
    model = TABLE_MAP.get(table_name)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"Table '{table_name}' not found. Available: {list(TABLE_MAP.keys())}",
        )

    return StreamingResponse(
        async_stream_csv_rows(db, model),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={table_name}.csv"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=API_HOST, port=API_PORT)
