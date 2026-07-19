"""
API server for exposing YClients BI data and queued sync controls.
"""
from __future__ import annotations

import asyncio
import csv
import hmac
import io
import logging
import time as perf_time
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Callable, Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    API_HOST,
    API_PORT,
    DASHBOARD_CORS_ALLOW_HEADERS,
    DASHBOARD_CORS_ALLOW_METHODS,
    DASHBOARD_CORS_ORIGIN_REGEX,
    DASHBOARD_CORS_ORIGINS,
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    IS_PRODUCTION,
    SYNC_API_TOKEN,
)
from dashboard_routes import router as dashboard_router
from auth_deps import forbid_demo
from auth_routes import router as auth_router
from auth_scope import (
    AccessContext,
    can_view_financials,
    effective_staff_id,
    require_financial_access,
    require_sync_company_ids,
    require_tenant_context,
)
from onboarding_routes import router as onboarding_router
from database import get_async_db, init_async_database
from portal_audit import log_portal_audit
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

try:
    from auth_service import _email_delivery_mode

    print(f'[auth] Email delivery: {_email_delivery_mode()}')
except Exception:
    pass

MAX_PAGE_SIZE = 5000
DEFAULT_PAGE_SIZE = 1000
PII_CLIENT_ROLES = {'platform_admin', 'owner', 'branch_admin', 'manager'}

OPEN_PATHS = {"/health"}
if not IS_PRODUCTION:
    OPEN_PATHS.update({"/openapi.json", "/docs", "/redoc"})

LOGGER = logging.getLogger('yclients.api')
TIMED_DASHBOARD_PATHS = {
    '/dashboard/bundle',
    '/dashboard/widget/plan_fact',
    '/dashboard/branches',
    '/dashboard/staff',
    '/dashboard/widget/sync_status',
}


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
):
    """Global auth: open paths, JWT user, or API key when configured."""
    from auth_deps import require_auth

    if request.url.path in OPEN_PATHS:
        return
    await require_auth(
        request,
        authorization,
        x_api_key=x_api_key,
        x_portal_account_id=x_portal_account_id,
        db=db,
    )


app = FastAPI(
    title="YClients BI System API",
    description="API для получения данных YClients в табличном формате",
    version="5.0.0",
    docs_url=None if IS_PRODUCTION else '/docs',
    redoc_url=None if IS_PRODUCTION else '/redoc',
    openapi_url=None if IS_PRODUCTION else '/openapi.json',
    dependencies=[Depends(require_api_key)],
)

_cors_origins = [o.strip() for o in DASHBOARD_CORS_ORIGINS.split(',') if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_origin_regex=DASHBOARD_CORS_ORIGIN_REGEX or None,
        allow_credentials=True,
        allow_methods=list(DASHBOARD_CORS_ALLOW_METHODS),
        allow_headers=list(DASHBOARD_CORS_ALLOW_HEADERS),
    )


SECURITY_HEADERS = {
    'X-Content-Type-Options': 'nosniff',
    'X-Frame-Options': 'DENY',
    'Referrer-Policy': 'strict-origin-when-cross-origin',
    'Content-Security-Policy': "frame-ancestors 'none'; base-uri 'self'; object-src 'none'",
}


def _request_is_https(request: Request) -> bool:
    forwarded_proto = request.headers.get('x-forwarded-proto', '').split(',')[0].strip().lower()
    return request.url.scheme == 'https' or forwarded_proto == 'https'


def _apply_security_headers(request: Request, response):
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    if IS_PRODUCTION and _request_is_https(request):
        response.headers.setdefault('Strict-Transport-Security', 'max-age=31536000; includeSubDomains')
    return response


@app.middleware('http')
async def add_security_headers(request: Request, call_next: Callable):
    started = perf_time.perf_counter()
    response = None
    try:
        response = await call_next(request)
        return _apply_security_headers(request, response)
    except Exception as exc:
        if request.url.path in TIMED_DASHBOARD_PATHS:
            duration_ms = round((perf_time.perf_counter() - started) * 1000, 2)
            access = request_access(request)
            LOGGER.exception(
                'dashboard_api_request_failed path=%s duration_ms=%s user_id=%s portal_account_id=%s role=%s exc=%s',
                request.url.path,
                duration_ms,
                getattr(access, 'user_id', None),
                getattr(access, 'portal_account_id', None),
                getattr(access, 'role', None),
                exc.__class__.__name__,
            )
        raise
    finally:
        if request.url.path in TIMED_DASHBOARD_PATHS and response is not None:
            duration_ms = round((perf_time.perf_counter() - started) * 1000, 2)
            access = request_access(request)
            LOGGER.info(
                'dashboard_api_request path=%s status=%s duration_ms=%s user_id=%s portal_account_id=%s role=%s',
                request.url.path,
                response.status_code,
                duration_ms,
                getattr(access, 'user_id', None),
                getattr(access, 'portal_account_id', None),
                getattr(access, 'role', None),
            )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    response = JSONResponse(
        status_code=500,
        content={'detail': 'Internal server error'},
    )
    return _apply_security_headers(request, response)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    if 'yclients-credentials' in request.url.path:
        response = JSONResponse(status_code=422, content={'detail': 'Invalid credential request'})
        return _apply_security_headers(request, response)
    sanitized = []
    for error in exc.errors():
        item = dict(error)
        item.pop('input', None)
        if item.get('ctx'):
            item['ctx'] = {
                key: str(value) if isinstance(value, BaseException) else value
                for key, value in item['ctx'].items()
            }
        sanitized.append(item)
    response = JSONResponse(status_code=422, content={'detail': sanitized})
    return _apply_security_headers(request, response)

app.include_router(auth_router, prefix='/auth', tags=['auth'])
app.include_router(auth_router, prefix='/dashboard/auth', tags=['auth'])
app.include_router(onboarding_router, prefix='/onboarding', tags=['onboarding'])
app.include_router(onboarding_router, prefix='/dashboard/onboarding', tags=['onboarding'])
app.include_router(dashboard_router, prefix='/dashboard', tags=['dashboard'])


def require_sync_token(x_sync_token: str | None = Header(default=None)):
    configured_token = (SYNC_API_TOKEN or '').strip()
    if not configured_token or not hmac.compare_digest(x_sync_token or '', configured_token):
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


def request_access(request: Request) -> AccessContext | None:
    return getattr(request.state, 'access', None)


def apply_company_scope(stmt, column, company_id: Optional[int], ctx: AccessContext | None):
    if ctx is None or ctx.full_access:
        return stmt.where(column == company_id) if company_id is not None else stmt
    allowed = ctx.company_ids or []
    if company_id is not None:
        if company_id not in allowed:
            raise HTTPException(status_code=403, detail='Branch not allowed')
        return stmt.where(column == company_id)
    return stmt.where(column.in_(allowed))


def apply_staff_scope(stmt, column, staff_id: Optional[int], ctx: AccessContext | None):
    scoped_staff_id = effective_staff_id(ctx, staff_id) if ctx is not None else staff_id
    return stmt.where(column == scoped_staff_id) if scoped_staff_id is not None else stmt


def require_client_pii_access(request: Request) -> tuple[AccessContext, int]:
    ctx = request_access(request)
    if ctx is None or ctx.user_id is None:
        raise HTTPException(status_code=401, detail='Authentication required')
    if ctx.role not in PII_CLIENT_ROLES:
        raise HTTPException(status_code=403, detail='Insufficient permissions')
    portal_account_id = require_tenant_context(ctx)
    if not ctx.company_ids:
        raise HTTPException(status_code=403, detail='No branch access assigned')
    return ctx, int(portal_account_id)


def require_request_financial_access(request: Request) -> None:
    ctx = request_access(request)
    if ctx is not None:
        require_financial_access(ctx)


def can_request_view_financials(request: Request) -> bool:
    ctx = request_access(request)
    return ctx is None or can_view_financials(ctx)


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
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    ctx = request_access(request)
    stmt = select(Group)
    count_company_filter = []
    if ctx is not None and not ctx.full_access:
        allowed = ctx.company_ids or []
        stmt = stmt.join(Company, Company.group_id == Group.id).where(Company.id.in_(allowed)).distinct()
        count_company_filter.append(Company.id.in_(allowed))
    stmt = stmt.order_by(Group.id.asc())
    total, groups = await fetch_page(db, stmt, limit, offset)
    data = []
    for group in groups:
        count_result = await db.execute(
            select(func.count()).where(Company.group_id == group.id, *count_company_filter)
        )
        data.append({
            "id": group.id,
            "title": group.title,
            "companies_count": count_result.scalar_one(),
        })
    return build_page_response(total, limit, offset, data)


@app.get("/companies")
async def api_companies(
    request: Request,
    group_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Company)
    ctx = request_access(request)
    stmt = apply_company_scope(stmt, Company.id, None, ctx)
    if group_id is not None:
        stmt = stmt.where(Company.group_id == group_id)
    stmt = stmt.order_by(Company.id.asc())
    total, companies = await fetch_page(db, stmt, limit, offset)
    data = [{"id": c.id, "title": c.title, "group_id": c.group_id} for c in companies]
    return build_page_response(total, limit, offset, data)


@app.get("/service_categories")
async def api_service_categories(
    request: Request,
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(ServiceCategoryCatalog)
    stmt = apply_company_scope(stmt, ServiceCategoryCatalog.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    category: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    show_financials = can_request_view_financials(request)
    if (min_price is not None or max_price is not None) and not show_financials:
        raise HTTPException(status_code=403, detail='Financial metrics are not allowed for this role')
    stmt = select(ServiceCatalog)
    stmt = apply_company_scope(stmt, ServiceCatalog.company_id, company_id, request_access(request))
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
        "price_min": item.price_min if show_financials else None,
        "duration_sec": item.duration,
        "duration_min": round(item.duration / 60, 1) if item.duration else None,
        "category": item.category_title,
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/staff_positions")
async def api_staff_positions(
    request: Request,
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(StaffPositionCatalog)
    stmt = apply_company_scope(stmt, StaffPositionCatalog.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(Staff)
    stmt = apply_company_scope(stmt, Staff.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    min_visits: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    ctx, portal_account_id = require_client_pii_access(request)
    stmt = select(Client)
    stmt = apply_company_scope(stmt, Client.company_id, company_id, ctx)
    if min_visits is not None:
        stmt = stmt.where(Client.visits_count >= min_visits)
    stmt = stmt.order_by(Client.id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    await log_portal_audit(
        db,
        actor_user_id=ctx.user_id,
        portal_account_id=portal_account_id,
        action='client_pii.read',
        target_type='clients',
        metadata={
            'company_id': company_id,
            'min_visits': min_visits,
            'limit': limit,
            'offset': offset,
            'row_count': len(items),
            'total': int(total),
        },
    )
    await db.commit()
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
    request: Request,
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(AccountCatalog)
    stmt = apply_company_scope(stmt, AccountCatalog.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(StorageCatalog)
    stmt = apply_company_scope(stmt, StorageCatalog.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    stmt = select(GoodCategoryCatalog)
    stmt = apply_company_scope(stmt, GoodCategoryCatalog.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    category_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    show_financials = can_request_view_financials(request)
    stmt = select(GoodCatalog)
    stmt = apply_company_scope(stmt, GoodCatalog.company_id, company_id, request_access(request))
    if category_id is not None:
        stmt = stmt.where(GoodCatalog.category_id == category_id)
    stmt = stmt.order_by(GoodCatalog.good_id.asc())
    total, items = await fetch_page(db, stmt, limit, offset)
    data = serialize_rows(items, lambda item: {
        "good_id": item.good_id,
        "title": item.title,
        "cost": item.cost if show_financials else None,
        "actual_cost": item.actual_cost if show_financials else None,
        "barcode": item.barcode,
        "unit": item.unit_short_title,
        "category_id": item.category_id,
        "last_change_date": serialize_value(item.last_change_date),
        "company_id": item.company_id,
    })
    return build_page_response(total, limit, offset, data)


@app.get("/appointments")
async def api_appointments(
    request: Request,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    client_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    ctx = request_access(request)
    stmt = select(Appointment)
    stmt = apply_company_scope(stmt, Appointment.company_id, company_id, ctx)
    stmt = apply_staff_scope(stmt, Appointment.staff_id, staff_id, ctx)
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
    request: Request,
    company_id: Optional[int] = None,
    appointment_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    require_request_financial_access(request)
    limit, offset = pagination
    stmt = select(Transaction)
    stmt = apply_company_scope(stmt, Transaction.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    require_request_financial_access(request)
    limit, offset = pagination
    stmt = select(FinancialTransaction)
    stmt = apply_company_scope(stmt, FinancialTransaction.company_id, company_id, request_access(request))
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
        "expense_title": item.expense_title,
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
    request: Request,
    company_id: Optional[int] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    require_request_financial_access(request)
    limit, offset = pagination
    stmt = select(GoodTransaction)
    stmt = apply_company_scope(stmt, GoodTransaction.company_id, company_id, request_access(request))
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
    request: Request,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    min_rating: Optional[float] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    ctx = request_access(request)
    stmt = select(Comment)
    stmt = apply_company_scope(stmt, Comment.company_id, company_id, ctx)
    stmt = apply_staff_scope(stmt, Comment.master_id, staff_id, ctx)
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
    request: Request,
    company_id: Optional[int] = None,
    staff_id: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: AsyncSession = Depends(get_async_db),
    pagination: tuple[int, int] = Depends(page_params),
):
    limit, offset = pagination
    ctx = request_access(request)
    stmt = select(StaffSchedule)
    stmt = apply_company_scope(stmt, StaffSchedule.company_id, company_id, ctx)
    stmt = apply_staff_scope(stmt, StaffSchedule.staff_id, staff_id, ctx)
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
async def api_stats(request: Request, db: AsyncSession = Depends(get_async_db)):
    require_request_financial_access(request)
    ctx = request_access(request)
    allowed = None if ctx is None or ctx.full_access else (ctx.company_ids or [])
    revenue_result = await db.execute(
        apply_company_scope(
            select(func.sum(Transaction.cost * Transaction.amount)),
            Transaction.company_id,
            None,
            ctx,
        )
    )
    revenue = revenue_result.scalar_one_or_none() or 0

    fin_result = await db.execute(
        apply_company_scope(
            select(func.sum(FinancialTransaction.amount)).where(FinancialTransaction.amount > 0),
            FinancialTransaction.company_id,
            None,
            ctx,
        )
    )
    fin_income = fin_result.scalar_one_or_none() or 0

    async def count_of(model):
        stmt = select(func.count()).select_from(model)
        company_column = getattr(model, 'company_id', None)
        if allowed is not None and company_column is not None:
            stmt = stmt.where(company_column.in_(allowed))
        elif allowed is not None and model is Company:
            stmt = stmt.where(Company.id.in_(allowed))
        r = await db.execute(stmt)
        return r.scalar_one()

    attended_result = await db.execute(
        apply_company_scope(
            select(func.count()).where(Appointment.attendance == 1),
            Appointment.company_id,
            None,
            ctx,
        )
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
    portal_account_id: int | None = None
    credential_id: int | None = None
    company_ids: list[int] = []
    global_sync: bool = False


@app.post("/sync/trigger", dependencies=[Depends(forbid_demo)])
async def trigger_sync(
    payload: SyncTriggerRequest,
    request: Request,
    _: None = Depends(require_sync_token),
    db: AsyncSession = Depends(get_async_db),
):
    ctx = request_access(request)
    portal_account_id: int | None = None
    company_ids: list[int] | None = None
    credential_id = payload.credential_id

    if ctx is not None and not ctx.full_access:
        portal_account_id = require_tenant_context(ctx)
        company_ids = require_sync_company_ids(ctx, payload.company_ids)
    elif ctx is not None and ctx.full_access:
        if payload.portal_account_id is not None:
            portal_account_id = payload.portal_account_id
        elif not payload.global_sync:
            raise HTTPException(status_code=400, detail='portal_account_id or global_sync=true is required')
        company_ids = [int(item) for item in dict.fromkeys(payload.company_ids)] or None

    job = await SyncJobService().async_enqueue_job(
        db,
        payload.mode,
        payload.initiator,
        portal_account_id=portal_account_id,
        credential_id=credential_id,
        company_ids=company_ids,
    )
    await log_portal_audit(
        db,
        actor_user_id=ctx.user_id if ctx is not None else None,
        portal_account_id=portal_account_id,
        action='sync.started',
        target_type='sync_job',
        target_id=job.id,
        metadata={
            'mode': job.mode,
            'initiator': job.initiator,
            'credential_id': job.credential_id,
            'company_ids': job.company_ids or [],
            'global_sync': bool(payload.global_sync and portal_account_id is None),
        },
    )
    await db.commit()
    return {
        "status": "queued",
        "job_id": job.id,
        "mode": job.mode,
        "initiator": job.initiator,
        "portal_account_id": job.portal_account_id,
        "credential_id": job.credential_id,
        "company_ids": job.company_ids or [],
    }


@app.get("/sync/status")
async def sync_status(
    request: Request,
    _: None = Depends(require_sync_token),
    db: AsyncSession = Depends(get_async_db),
):
    ctx = request_access(request)
    portal_account_id = None
    if ctx is not None and not ctx.full_access:
        portal_account_id = require_tenant_context(ctx)
    return {
        "sync": await asyncio.to_thread(get_sync_status),
        "queue": await SyncJobService().async_get_status_payload(db, portal_account_id=portal_account_id),
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

FINANCIAL_EXPORT_MODELS = {Transaction, FinancialTransaction, GoodTransaction}
STAFF_SCOPED_EXPORT_COLUMNS = {
    Appointment: Appointment.staff_id,
    Comment: Comment.master_id,
    StaffSchedule: StaffSchedule.staff_id,
}


async def async_stream_csv_rows(db: AsyncSession, model, ctx: AccessContext | None = None):
    columns = [column.key for column in model.__table__.columns]
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(columns)
    yield buffer.getvalue()
    buffer.seek(0)
    buffer.truncate(0)

    stmt = select(model)
    company_column = getattr(model, 'company_id', None)
    if company_column is not None:
        stmt = apply_company_scope(stmt, company_column, None, ctx)
    elif model is Company:
        stmt = apply_company_scope(stmt, Company.id, None, ctx)
    elif ctx is not None and not ctx.full_access:
        stmt = stmt.where(False)
    staff_column = STAFF_SCOPED_EXPORT_COLUMNS.get(model)
    if staff_column is not None:
        stmt = apply_staff_scope(stmt, staff_column, None, ctx)
    stmt = stmt.order_by(*model.__table__.primary_key.columns)
    result = await db.stream(stmt)
    async for row in result.scalars():
        writer.writerow([serialize_value(getattr(row, column)) for column in columns])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


@app.get("/export/csv/{table_name}")
async def export_csv(table_name: str, request: Request, db: AsyncSession = Depends(get_async_db)):
    model = TABLE_MAP.get(table_name)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"Table '{table_name}' not found. Available: {list(TABLE_MAP.keys())}",
        )
    ctx = request_access(request)
    if model in FINANCIAL_EXPORT_MODELS:
        require_request_financial_access(request)
    if model is Client:
        ctx, portal_account_id = require_client_pii_access(request)
        await log_portal_audit(
            db,
            actor_user_id=ctx.user_id,
            portal_account_id=portal_account_id,
            action='client_pii.export',
            target_type='clients',
            metadata={'table': table_name, 'format': 'csv'},
        )
        await db.commit()

    return StreamingResponse(
        async_stream_csv_rows(db, model, ctx),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename={table_name}.csv"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=API_HOST, port=API_PORT)
