"""
API server: portal and dashboard routers, health probes and queued sync controls.
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time as perf_time
from typing import Callable, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    API_HOST,
    API_PORT,
    LOG_LEVEL,
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
from auth_deps import forbid_demo, require_auth
from open_paths import OPEN_PATHS
from auth_routes import router as auth_router
from auth_scope import AccessContext, require_sync_company_ids, require_tenant_context
from onboarding_routes import router as onboarding_router
from database import get_async_db, init_async_database
from portal_audit import log_portal_audit
from models import YClientsCredential
from dashboard_service import PeriodOutOfRangeError
from sync_jobs import SyncJobService
from sync_orchestrator import get_sync_status
from sync_security import validate_sync_token

init_async_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)

LOGGER = logging.getLogger('yclients.api')


def _configure_logging() -> None:
    """Send application logs to stdout.

    uvicorn configures only its own loggers, so without a handler here the request
    timings below are built, formatted and then dropped: the application logger sits
    at WARNING with no handler and `LOGGER.info` never reaches the container log.
    """
    logger = logging.getLogger('yclients')
    if logger.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(LOG_LEVEL)
    logger.propagate = False


_configure_logging()

# Every dashboard route is timed. A hand-kept list of paths silently omits whatever
# nobody remembered to add, and the log exists to answer "did anything get slower".
TIMED_DASHBOARD_PREFIX = '/dashboard/'
_CONTROL_CHARACTERS = re.compile(r'[\x00-\x1f\x7f-\x9f\u2028\u2029]')
_REPORT_ID_TOKEN = re.compile(r'[A-Za-z0-9_]{1,64}')


def _timed_request_fields(request: Request) -> tuple[str, str] | None:
    """Return the path and report id as they may be written to the log.

    Both come from an unauthenticated request and Starlette decodes `%0A`, so a raw value could
    forge whole log lines: the path has control characters escaped and the report id is only
    logged when it looks like a report id.
    """
    path = request.url.path
    if not path.startswith(TIMED_DASHBOARD_PREFIX):
        return None
    report_id = request.query_params.get('report_id') or ''
    if not _REPORT_ID_TOKEN.fullmatch(report_id):
        report_id = '-'
    path = _CONTROL_CHARACTERS.sub(lambda match: match.group().encode('unicode_escape').decode(), path)
    return path, report_id


async def require_api_key(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
    x_portal_account_id: int | None = Header(default=None),
    db: AsyncSession = Depends(get_async_db),
):
    """Global auth: open paths, JWT user, or API key when configured."""
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
    # Everything the API answers is per-user data; a route that wants caching sets its own header.
    'Cache-Control': 'private, no-store',
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
        timed = _timed_request_fields(request)
        if timed is not None:
            path, report_id = timed
            duration_ms = round((perf_time.perf_counter() - started) * 1000, 2)
            access = request_access(request)
            LOGGER.exception(
                'dashboard_api_request_failed path=%s report_id=%s duration_ms=%s '
                'user_id=%s portal_account_id=%s role=%s exc=%s',
                path,
                report_id,
                duration_ms,
                getattr(access, 'user_id', None),
                getattr(access, 'portal_account_id', None),
                getattr(access, 'role', None),
                exc.__class__.__name__,
            )
        raise
    finally:
        timed = _timed_request_fields(request)
        if timed is not None and response is not None:
            path, report_id = timed
            duration_ms = round((perf_time.perf_counter() - started) * 1000, 2)
            access = request_access(request)
            LOGGER.info(
                'dashboard_api_request path=%s report_id=%s status=%s duration_ms=%s '
                'user_id=%s portal_account_id=%s role=%s',
                path,
                report_id,
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


@app.exception_handler(PeriodOutOfRangeError)
async def period_out_of_range_handler(request: Request, exc: PeriodOutOfRangeError):
    response = JSONResponse(status_code=400, content={'detail': str(exc)})
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
    validate_sync_token(x_sync_token, SYNC_API_TOKEN)


def request_access(request: Request) -> AccessContext | None:
    return getattr(request.state, 'access', None)


@app.get("/")
async def root():
    return {"name": "YClients BI System API", "status": "ok"}


# Bounded so a stuck database cannot hold the probe open past the caller's own timeout.
HEALTH_DB_TIMEOUT_SECONDS = 3


@app.get("/health")
async def health():
    """Liveness only: the process is up and serving.

    Deliberately does not touch the database. The container healthcheck reads this, and
    a database blip should not mark a working API unhealthy — readiness answers that.
    """
    return {"status": "ok"}


@app.get("/health/ready")
async def health_ready(db: AsyncSession = Depends(get_async_db)):
    """Readiness: the API can actually serve requests, database included.

    The async engine is lazy — it opens no connection until the first query — so a broken
    connection setting leaves every route returning 500 while liveness still answers 200.
    The deploy loop reads this endpoint so that such a build fails instead of shipping.
    """
    try:
        await asyncio.wait_for(db.execute(text('SELECT 1')), timeout=HEALTH_DB_TIMEOUT_SECONDS)
    except Exception:
        LOGGER.exception('health_ready_database_unavailable')
        return JSONResponse(
            status_code=503,
            content={'status': 'degraded', 'database': 'error'},
        )
    return {'status': 'ok', 'database': 'ok'}


class SyncTriggerRequest(BaseModel):
    mode: Literal['incremental', 'refresh', 'full'] = 'incremental'
    initiator: str = Field('dashboard', max_length=64)
    portal_account_id: int | None = None
    credential_id: int | None = None
    company_ids: list[int] = Field(default_factory=list, max_length=200)
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

    if credential_id is not None:
        credential_stmt = select(YClientsCredential.id).where(YClientsCredential.id == credential_id)
        if portal_account_id is not None:
            credential_stmt = credential_stmt.where(YClientsCredential.portal_account_id == portal_account_id)
        if (await db.execute(credential_stmt)).scalar_one_or_none() is None:
            raise HTTPException(status_code=404, detail='Credential not found')

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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=API_HOST, port=API_PORT)
