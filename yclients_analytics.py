"""Async access to YClients analytics used by the product dashboard."""

from __future__ import annotations

import asyncio
import time
from datetime import date
from typing import Any, Iterable

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import YCLIENTS_RETRY_BACKOFF, YCLIENTS_RETRY_TOTAL, YCLIENTS_TIMEOUT
from models import Company, Staff
from yclients_api import YClientsAPI
from yclients_credentials import YClientsCredentialValue, load_credentials_for_companies_async

MAX_CONCURRENT_ANALYTICS_REQUESTS = 4
# A user token outlives this by far; the TTL only bounds how long a revoked one keeps failing.
USER_TOKEN_TTL_SECONDS = 600
# The ETL client waits out a 429 for up to a minute. A dashboard request cannot: past this the
# call fails and the page falls back to local counts, as it does for any other YClients error.
MAX_RETRY_WAIT_SECONDS = 5.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
# credential id + a digest of its secrets -> (user token, expiry). Editing a credential changes
# the digest, so the old token is not reused for the new login.
_user_tokens: dict[tuple[int, int], tuple[str, float]] = {}


class YClientsAnalyticsError(RuntimeError):
    """Raised when exact appointment analytics cannot be loaded or validated."""


def _coerce_count(record_stats: dict[str, Any], field: str) -> int:
    value = record_stats.get(field)
    if isinstance(value, bool):
        raise YClientsAnalyticsError(f'Invalid YClients analytics field: {field}')
    try:
        count = int(value)
    except (TypeError, ValueError) as exc:
        raise YClientsAnalyticsError(f'Invalid YClients analytics field: {field}') from exc
    if count < 0:
        raise YClientsAnalyticsError(f'Negative YClients analytics field: {field}')
    return count


def _parse_record_stats(payload: Any) -> dict[str, int]:
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise YClientsAnalyticsError('YClients analytics request was not successful')
    data = payload.get('data')
    record_stats = data.get('record_stats') if isinstance(data, dict) else None
    if not isinstance(record_stats, dict):
        raise YClientsAnalyticsError('YClients record_stats are missing')
    return {
        'completed': _coerce_count(record_stats, 'current_completed_count'),
        'incomplete': _coerce_count(record_stats, 'current_pending_count'),
        'cancelled': _coerce_count(record_stats, 'current_canceled_count'),
        'total': _coerce_count(record_stats, 'current_total_count'),
    }


def _retry_delay(response: Any, attempt: int) -> float | None:
    """Seconds to wait before retrying, or None when the wait is longer than a page load can afford."""
    retry_after = getattr(response, 'headers', {}).get('Retry-After')
    try:
        delay = float(retry_after) if retry_after is not None else YCLIENTS_RETRY_BACKOFF * 2 ** attempt
    except ValueError:
        delay = YCLIENTS_RETRY_BACKOFF * 2 ** attempt
    return delay if delay <= MAX_RETRY_WAIT_SECONDS else None


async def _send(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    """One request, retried on 429/5xx the way the ETL client's urllib3 retry does."""
    retries = max(0, YCLIENTS_RETRY_TOTAL)
    for attempt in range(retries + 1):
        response = await getattr(client, method)(url, **kwargs)
        if response.status_code not in RETRYABLE_STATUSES or attempt == retries:
            return response
        delay = _retry_delay(response, attempt)
        if delay is None:
            return response
        await asyncio.sleep(delay)


def _token_key(credentials: YClientsCredentialValue) -> tuple[int, int] | None:
    if credentials.id is None:
        return None
    return int(credentials.id), hash((credentials.partner_token, credentials.login, credentials.password))


async def _user_token(client: httpx.AsyncClient, credentials: YClientsCredentialValue, *, refresh: bool = False) -> str:
    key = _token_key(credentials)
    cached = _user_tokens.get(key) if key is not None else None
    if cached is not None and not refresh and cached[1] > time.monotonic():
        return cached[0]
    token = await _authenticate(client, credentials)
    if key is not None:
        _user_tokens[key] = (token, time.monotonic() + USER_TOKEN_TTL_SECONDS)
    return token


async def _authenticate(client: httpx.AsyncClient, credentials: YClientsCredentialValue) -> str:
    response = await _send(
        client,
        'post',
        f'{YClientsAPI.BASE_URL}/auth',
        headers={
            'Authorization': f'Bearer {credentials.partner_token}',
            'Accept': 'application/vnd.yclients.v2+json',
            'Content-Type': 'application/json',
        },
        json={'login': credentials.login, 'password': credentials.password},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise YClientsAnalyticsError('YClients authentication failed')
    data = payload.get('data')
    user_token = data.get('user_token') if isinstance(data, dict) else None
    if payload.get('success') is not True or not user_token:
        raise YClientsAnalyticsError('YClients authentication failed')
    return str(user_token)


def _group_company_credentials(
    credential_by_company: dict[int, YClientsCredentialValue],
    company_ids: list[int],
) -> dict[int | None, tuple[YClientsCredentialValue, list[int]]]:
    groups: dict[int | None, tuple[YClientsCredentialValue, list[int]]] = {}
    for company_id in company_ids:
        credentials = credential_by_company.get(company_id)
        if credentials is None:
            raise YClientsAnalyticsError(
                f'No YClients credentials configured for company {company_id}'
            )
        key = credentials.id
        if key not in groups:
            groups[key] = (credentials, [])
        groups[key][1].append(company_id)
    return groups


async def _gather_or_cancel(coroutines: list[Any]) -> list[Any]:
    """`asyncio.gather` that leaves nothing running when it raises: the siblings of a failed call are cancelled.

    Plain `gather` re-raises the first error while the other requests go on, and by then the caller is
    closing the HTTP client under them.
    """
    tasks = [asyncio.ensure_future(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def fetch_record_stats(
    company_ids: Iterable[int],
    start: date,
    end: date,
    staff_id: int | None = None,
    *,
    db: AsyncSession,
) -> dict[str, int]:
    """Load exact record_stats and sum them across the requested companies."""
    normalized_company_ids = list(dict.fromkeys(int(company_id) for company_id in company_ids))
    if not normalized_company_ids:
        return {'completed': 0, 'incomplete': 0, 'cancelled': 0, 'total': 0}

    credential_by_company = await load_credentials_for_companies_async(db, normalized_company_ids)
    credential_groups = _group_company_credentials(credential_by_company, normalized_company_ids)
    company_rows = (
        await db.execute(
            select(Company.id, Company.external_id).where(Company.id.in_(normalized_company_ids))
        )
    ).all()
    external_company_by_internal = {
        int(row.id): int(row.external_id or row.id)
        for row in company_rows
    }
    api_staff_id = staff_id
    if staff_id is not None:
        staff_external_id = await db.scalar(select(Staff.external_id).where(Staff.id == int(staff_id)))
        api_staff_id = int(staff_external_id or staff_id)

    timeout = httpx.Timeout(max(1.0, float(YCLIENTS_TIMEOUT)))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            semaphore = asyncio.Semaphore(MAX_CONCURRENT_ANALYTICS_REQUESTS)

            async def load_company(
                company_id: int,
                credentials: YClientsCredentialValue,
                user_token: str,
            ) -> dict[str, int]:
                params: dict[str, Any] = {
                    'date_from': start.isoformat(),
                    'date_to': end.isoformat(),
                }
                if api_staff_id is not None:
                    params['staff_id'] = int(api_staff_id)
                headers = {
                    'Authorization': f'Bearer {credentials.partner_token}, User {user_token}',
                    'Accept': 'application/vnd.yclients.v2+json',
                    'Content-Type': 'application/json',
                }
                external_id = external_company_by_internal.get(company_id, company_id)
                url = f'{YClientsAPI.BASE_URL}/company/{external_id}/analytics/overall/'
                async with semaphore:
                    response = await _send(client, 'get', url, headers=headers, params=params)
                    if response.status_code in (401, 403):
                        # The cached token may have been revoked; one fresh login settles it.
                        user_token = await _user_token(client, credentials, refresh=True)
                        headers['Authorization'] = f'Bearer {credentials.partner_token}, User {user_token}'
                        response = await _send(client, 'get', url, headers=headers, params=params)
                response.raise_for_status()
                return _parse_record_stats(response.json())

            rows = []
            for credentials, grouped_company_ids in credential_groups.values():
                user_token = await _user_token(client, credentials)
                rows.extend(
                    await _gather_or_cancel(
                        [load_company(company_id, credentials, user_token) for company_id in grouped_company_ids]
                    )
                )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise YClientsAnalyticsError('Unable to load YClients appointment analytics') from exc

    totals = {'completed': 0, 'incomplete': 0, 'cancelled': 0, 'total': 0}
    for row in rows:
        for field in totals:
            totals[field] += row[field]
    return totals
