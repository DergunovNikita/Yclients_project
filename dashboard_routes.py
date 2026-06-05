"""HTTP routes for product dashboard JSON (Chart.js / SPA)."""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from config import SYNC_API_TOKEN
from dashboard_service import (
    fetch_branches,
    fetch_extra_services,
    fetch_manual_review_facts,
    fetch_plan_fact,
    fetch_plan_settings,
    fetch_revenue_daily,
    save_plan_settings,
    save_manual_review_facts,
    fetch_staff,
    fetch_staff_directory,
    fetch_summary,
    fetch_top_services,
)
from database import get_async_db
from plan_import import import_plan_sheet_from_config
from sync_jobs import SyncJobService
from sync_orchestrator import get_sync_status

router = APIRouter()


class ManualReviewFactItem(BaseModel):
    company_id: int
    staff_id: int
    date: date
    value: float | None = None


class ManualReviewFactsPayload(BaseModel):
    start_date: date
    end_date: date
    company_id: int | None = None
    staff_id: int | None = None
    items: list[ManualReviewFactItem]


class PlanSettingsBranchPayload(BaseModel):
    company_id: int
    wax_pct: Any = None
    head_care_pct: Any = None
    face_care_pct: Any = None
    camouflage_pct: Any = None
    cosmo_pct: Any = None
    opz_pct: Any = None
    cosmo_price: Any = None


class PlanSettingsStaffPayload(BaseModel):
    company_id: int
    staff_id: int
    staff_category: str
    clients: Any = None
    avg_check_total: Any = None
    reviews_qty: Any = None
    cosmo_qty: Any = None


class PlanSettingsPayload(BaseModel):
    month: str
    branches: list[PlanSettingsBranchPayload]
    staff: list[PlanSettingsStaffPayload]


def _parse_range(start: date, end: date) -> tuple[date, date]:
    if start > end:
        raise HTTPException(status_code=400, detail='start_date must be <= end_date')
    return start, end


def _require_sync_token(x_sync_token: str | None) -> None:
    if SYNC_API_TOKEN and x_sync_token != SYNC_API_TOKEN:
        raise HTTPException(status_code=401, detail='Invalid sync token')


@router.get('/branches')
async def dashboard_branches(db: AsyncSession = Depends(get_async_db)):
    """Companies available as salon branches (filtered when system.portal_branches is populated)."""
    return {'success': True, 'data': await fetch_branches(db)}


@router.get('/staff')
async def dashboard_staff(
    company_id: int | None = Query(None, description='Optional YClients company (salon) id'),
    db: AsyncSession = Depends(get_async_db),
):
    """Active staff available for dashboard filters."""
    return {'success': True, 'data': await fetch_staff(db, company_id)}


@router.get('/staff_directory.csv')
async def dashboard_staff_directory_csv(
    include_fired: bool = Query(False, description='Include fired/stale staff when true'),
    db: AsyncSession = Depends(get_async_db),
):
    """CSV staff directory for Google Sheets IMPORTDATA."""
    rows = await fetch_staff_directory(db, include_fired)
    columns = [
        'company_id',
        'company_title',
        'staff_id',
        'staff_name',
        'position',
        'user_id',
        'fired',
        'working',
        'bookable',
    ]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        buffer.getvalue(),
        media_type='text/csv; charset=utf-8',
        headers={'Content-Disposition': 'inline; filename=staff_directory.csv'},
    )


@router.get('/widget/sync_status')
async def dashboard_widget_sync_status(db: AsyncSession = Depends(get_async_db)):
    """Read-only sync + queue snapshot for dashboard UX (API key only, no sync token)."""
    sync_payload = await asyncio.to_thread(get_sync_status)
    queue = await SyncJobService().async_get_status_payload(db)
    return {'success': True, 'data': {'sync': sync_payload, 'queue': queue}}


@router.get('/widget/summary')
async def dashboard_widget_summary(
    start_date: date = Query(..., description='Inclusive period start'),
    end_date: date = Query(..., description='Inclusive period end'),
    company_id: int | None = Query(None, description='Optional YClients company (salon) id'),
    staff_id: int | None = Query(None, description='Optional active staff id'),
    db: AsyncSession = Depends(get_async_db),
):
    start, end = _parse_range(start_date, end_date)
    return {'success': True, 'data': await fetch_summary(db, start, end, company_id, staff_id)}


@router.get('/widget/revenue_daily')
async def dashboard_widget_revenue_daily(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
):
    start, end = _parse_range(start_date, end_date)
    return {'success': True, 'data': await fetch_revenue_daily(db, start, end, company_id, staff_id)}


@router.get('/widget/top_services')
async def dashboard_widget_top_services(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_async_db),
):
    start, end = _parse_range(start_date, end_date)
    return {'success': True, 'data': await fetch_top_services(db, start, end, company_id, limit, staff_id)}


@router.get('/widget/extra_services')
async def dashboard_widget_extra_services(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    limit: int | None = Query(None, ge=1, le=1000),
    db: AsyncSession = Depends(get_async_db),
):
    start, end = _parse_range(start_date, end_date)
    return {'success': True, 'data': await fetch_extra_services(db, start, end, company_id, limit, staff_id)}


@router.get('/widget/plan_fact')
async def dashboard_widget_plan_fact(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
):
    start, end = _parse_range(start_date, end_date)
    return {'success': True, 'data': await fetch_plan_fact(db, start, end, company_id, staff_id)}


@router.get('/plan/settings')
async def dashboard_plan_settings(
    month: str = Query(..., description='Plan settings month in YYYY-MM format'),
    copy_from: str | None = Query(None, description='Optional source month in YYYY-MM format'),
    db: AsyncSession = Depends(get_async_db),
):
    try:
        data = await fetch_plan_settings(db, month, copy_from)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.post('/plan/settings')
async def dashboard_plan_settings_save(
    payload: PlanSettingsPayload,
    db: AsyncSession = Depends(get_async_db),
):
    try:
        data = await save_plan_settings(
            db,
            payload.month,
            [item.model_dump() for item in payload.branches],
            [item.model_dump() for item in payload.staff],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.get('/plan/reviews_fact')
async def dashboard_plan_reviews_fact(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
):
    start, end = _parse_range(start_date, end_date)
    try:
        data = await fetch_manual_review_facts(db, start, end, company_id, staff_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.post('/plan/reviews_fact')
async def dashboard_plan_reviews_fact_save(
    payload: ManualReviewFactsPayload,
    db: AsyncSession = Depends(get_async_db),
):
    start, end = _parse_range(payload.start_date, payload.end_date)
    try:
        data = await save_manual_review_facts(
            db,
            start,
            end,
            payload.company_id,
            payload.staff_id,
            [item.model_dump() for item in payload.items],
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {'success': True, 'data': data}


@router.post('/plan/sync')
async def dashboard_plan_sync(
    x_sync_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
):
    _require_sync_token(x_sync_token)
    return {'success': True, 'data': await import_plan_sheet_from_config(db)}


@router.get('/bundle')
async def dashboard_bundle(
    start_date: date = Query(...),
    end_date: date = Query(...),
    company_id: int | None = Query(None),
    staff_id: int | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
):
    """Single round-trip: summary + daily revenue + service breakdowns (sequential server-side)."""
    start, end = _parse_range(start_date, end_date)
    summary = await fetch_summary(db, start, end, company_id, staff_id)
    daily = await fetch_revenue_daily(db, start, end, company_id, staff_id)
    services = await fetch_top_services(db, start, end, company_id, 10, staff_id)
    extra_services = await fetch_extra_services(db, start, end, company_id, None, staff_id)
    return {
        'success': True,
        'data': {
            'summary': summary,
            'revenue_daily': daily,
            'top_services': services,
            'extra_services': extra_services,
        },
    }
