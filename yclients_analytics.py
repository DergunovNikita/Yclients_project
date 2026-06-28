"""Async access to YClients analytics used by the product dashboard."""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any, Iterable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from config import YCLIENTS_TIMEOUT
from yclients_credentials import YClientsCredentialValue, load_credentials_for_companies_async

YCLIENTS_BASE_URL = 'https://api.yclients.com/api/v1'
MAX_CONCURRENT_ANALYTICS_REQUESTS = 4

# Backward-compatible test hooks. Runtime analytics credentials are loaded from
# system.yclients_credentials.
PARTNER_TOKEN = ''
LOGIN = ''
PASSWORD = ''
USER_LOGIN = ''
USER_PASSWORD = ''


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


async def _authenticate(client: httpx.AsyncClient, credentials: YClientsCredentialValue) -> str:
    response = await client.post(
        f'{YCLIENTS_BASE_URL}/auth',
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
        key = credentials.id if credentials.id is not None else None
        if key not in groups:
            groups[key] = (credentials, [])
        groups[key][1].append(company_id)
    return groups


async def fetch_record_stats(
    company_ids: Iterable[int],
    start: date,
    end: date,
    staff_id: int | None = None,
    db: AsyncSession | None = None,
) -> dict[str, int]:
    """Load exact record_stats and sum them across the requested companies."""
    normalized_company_ids = list(dict.fromkeys(int(company_id) for company_id in company_ids))
    if not normalized_company_ids:
        return {'completed': 0, 'incomplete': 0, 'cancelled': 0, 'total': 0}

    if db is None:
        raise YClientsAnalyticsError('Database session is required to resolve YClients credentials')
    credential_by_company = await load_credentials_for_companies_async(db, normalized_company_ids)
    credential_groups = _group_company_credentials(credential_by_company, normalized_company_ids)

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
                if staff_id is not None:
                    params['staff_id'] = int(staff_id)
                headers = {
                    'Authorization': f'Bearer {credentials.partner_token}, User {user_token}',
                    'Accept': 'application/vnd.yclients.v2+json',
                    'Content-Type': 'application/json',
                }
                async with semaphore:
                    response = await client.get(
                        f'{YCLIENTS_BASE_URL}/company/{company_id}/analytics/overall/',
                        headers=headers,
                        params=params,
                    )
                response.raise_for_status()
                return _parse_record_stats(response.json())

            rows = []
            for credentials, grouped_company_ids in credential_groups.values():
                user_token = await _authenticate(client, credentials)
                rows.extend(
                    await asyncio.gather(
                        *(load_company(company_id, credentials, user_token) for company_id in grouped_company_ids)
                    )
                )
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise YClientsAnalyticsError('Unable to load YClients appointment analytics') from exc

    totals = {'completed': 0, 'incomplete': 0, 'cancelled': 0, 'total': 0}
    for row in rows:
        for field in totals:
            totals[field] += row[field]
    return totals
