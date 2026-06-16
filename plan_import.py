"""Import manually maintained branch and staff plan values from a Google Sheets CSV export."""

from __future__ import annotations

import csv
import io
import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from config import (
    GOOGLE_SERVICE_ACCOUNT_FILE,
    GOOGLE_SERVICE_ACCOUNT_JSON_B64,
    PLAN_SHEET_CSV_URL,
    PLAN_SHEET_ID,
    PLAN_SHEET_NAME,
    SERVICES_SHEET_CSV_URL,
    SERVICES_SHEET_ID,
    SERVICES_SHEET_NAME,
)
from models import Company, PlanMetric, Service, ServiceCatalog, ServiceLabel, Staff
from plan_config import (
    PLAN_FACT_METRICS,
    RAW_PLAN_FACT_CODES,
    STAFF_CATEGORY_METRIC_CODES,
    normalize_staff_category,
)


METRIC_COLUMN_ALIASES = {
    'revenue': ('выручка', 'revenue'),
    'avg_check_total': ('сч общий', 'сч', 'avg_check_total', 'average_check'),
    'clients': ('кол-во клиентов', 'количество клиентов', 'клиенты', 'clients'),
    'wax_qty': ('воск, шт', 'воск', 'wax_qty'),
    'camouflage_qty': ('камуфляж, шт', 'камуфляж', 'camouflage_qty'),
    'face_care_qty': ('уход лицо, шт', 'уход лицо', 'face_care_qty'),
    'head_care_qty': ('уход голова, шт', 'уход голова', 'head_care_qty'),
    'cosmo_qty': ('космо, шт', 'космо шт', 'cosmo_qty'),
    'cosmo_sum': ('космо сумм.', 'космо сумма', 'космо сумм', 'cosmo_sum'),
    'opz_qty': ('опз, шт', 'опз шт', 'opz_qty'),
    'opz_pct': ('опз,%', 'опз %', 'опз процент', 'opz_pct'),
    'extra_services_pct': ('% доп.услуг', 'доп.услуг,%', 'доп услуги %', 'extra_services_pct'),
    'reviews_qty': ('отзывы', 'отзывы план', 'кол-во отзывов', 'количество отзывов', 'reviews_qty'),
}

RETIRED_PLAN_METRIC_CODES: set[str] = set()
PLAN_FACT_METRIC_CODES = {metric['code'] for metric in PLAN_FACT_METRICS}

PERIOD_START_ALIASES = ('period_start', 'date_from', 'start_date', 'начало периода', 'с')
PERIOD_END_ALIASES = ('period_end', 'date_to', 'end_date', 'конец периода', 'по')
MONTH_ALIASES = ('month', 'месяц', 'period', 'период')
COMPANY_ID_ALIASES = ('company_id', 'yclients_company_id', 'id филиала')
BRANCH_ALIASES = ('branch', 'branch_name', 'филиал', 'бш', 'салон')
STAFF_ID_ALIASES = ('staff_id', 'stuff_id', 'yclients_staff_id', 'employee_id', 'id сотрудника', 'id работника')
STAFF_NAME_ALIASES = ('staff', 'staff_name', 'stuff_name', 'employee', 'employee_name', 'сотрудник', 'работник')
STAFF_CATEGORY_ALIASES = ('staff_category', 'category', 'position', 'role', 'должность', 'роль', 'категория', 'тип сотрудника')
NETWORK_NAMES = {'сеть', 'итого', 'total', 'network'}
VALIDATION_TOLERANCE = 0.01

SERVICE_ID_ALIASES = ('service_id', 'yclients_service_id', 'id услуги', 'id_услуги', 'id', 'service id')
SERVICE_TITLE_ALIASES = ('service', 'service_title', 'title', 'услуга', 'название услуги')
SERVICE_CATEGORY_ALIASES = ('category', 'service_category', 'категория', 'категория услуги')
SERVICE_EXTRA_ALIASES = (
    'is_extra',
    'extra_service',
    'additional_service',
    'доп услуга',
    'доп. услуга',
    'доп услуги',
    'доп. услуги',
    'метка доп услуг',
)
SERVICE_TAG_ALIASES = ('tag', 'label', 'метка', 'тег')
TRUE_MARKERS = {'1', 'true', 'yes', 'y', 'да', 'истина', 'доп', 'доп услуга', 'доп услуги', 'extra', 'additional', 'x', '+'}
FALSE_MARKERS = {
    '0',
    'false',
    'no',
    'n',
    'нет',
    'ложь',
    'обычная',
    'основная',
    'не доп',
    'не доп услуга',
    'не доп услуги',
    'standard',
    'base',
    '-',
}
GOOGLE_SHEETS_READONLY_SCOPE = 'https://www.googleapis.com/auth/spreadsheets.readonly'
GOOGLE_OAUTH_TOKEN_URI = 'https://oauth2.googleapis.com/token'


@dataclass(frozen=True)
class ParsedPlanRow:
    row_index: int
    period_start: date
    period_end: date
    company_id: int | None
    staff_id: int | None
    staff_category: str | None
    values: dict[str, float]
    metric_codes: frozenset[str]
    scope: str


def _normalize(value: Any) -> str:
    text = str(value or '').strip().lower().replace('ё', 'е')
    text = text.replace('\xa0', ' ')
    text = re.sub(r'\s+', ' ', text)
    return text


def _find_value(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    normalized = {_normalize(key): value for key, value in row.items()}
    for alias in aliases:
        value = normalized.get(_normalize(alias))
        if value not in (None, ''):
            return value
    return ''


def _parse_date(value: str) -> date:
    raw = str(value or '').strip()
    for fmt in ('%Y-%m-%d', '%d.%m.%Y', '%d/%m/%Y'):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    raise ValueError(f'Unsupported date format: {value}')


def _parse_period(row: dict[str, str]) -> tuple[date, date]:
    start_value = _find_value(row, PERIOD_START_ALIASES)
    end_value = _find_value(row, PERIOD_END_ALIASES)
    if start_value and end_value:
        return _parse_date(start_value), _parse_date(end_value)

    month_value = _find_value(row, MONTH_ALIASES).strip()
    if re.fullmatch(r'\d{4}-\d{2}', month_value):
        year, month = [int(part) for part in month_value.split('-')]
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])

    raise ValueError('Plan row must contain period_start/period_end or month=YYYY-MM')


def _parse_number(value: str) -> float | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    normalized = (
        raw.replace('\xa0', '')
        .replace(' ', '')
        .replace('%', '')
        .replace('₽', '')
    )
    if ',' in normalized and '.' in normalized:
        normalized = normalized.replace(',', '')
    elif normalized.count(',') > 1:
        normalized = normalized.replace(',', '')
    elif re.fullmatch(r'-?\d{1,3},\d{3}', normalized):
        normalized = normalized.replace(',', '')
    else:
        normalized = normalized.replace(',', '.')
    try:
        return float(normalized)
    except ValueError:
        return None


def _parse_marker(value: str) -> bool | None:
    text = _normalize(value)
    if not text:
        return None
    if text in TRUE_MARKERS:
        return True
    if text in FALSE_MARKERS:
        return False
    if 'доп' in text or 'extra' in text or 'additional' in text:
        return True
    return None


def _branch_or_staff_filter(period_start: date, period_end: date, company_id: int, staff_id: int | None):
    parts = [
        PlanMetric.period_start == period_start,
        PlanMetric.period_end == period_end,
        PlanMetric.company_id == company_id,
    ]
    if staff_id is None:
        parts.append(PlanMetric.staff_id.is_(None))
    else:
        parts.append(PlanMetric.staff_id == staff_id)
    return parts


def _validation_warning(
    scope: str,
    key: tuple[Any, ...],
    metric_code: str,
    expected: float | None,
    actual: float,
) -> str:
    expected_text = 'нет плана' if expected is None else f'{expected:g}'
    return (
        f'{scope} {key}: metric {metric_code} mismatch '
        f'(expected {expected_text}, calculated {actual:g})'
    )


def _sum_values(rows: list[ParsedPlanRow]) -> dict[str, float]:
    values: dict[str, float] = {}
    for row in rows:
        for code in RAW_PLAN_FACT_CODES:
            if code in row.values:
                values[code] = values.get(code, 0.0) + float(row.values[code] or 0)
    return values


def _barber_avg_check_value(row: ParsedPlanRow) -> float | None:
    if row.staff_category != 'barber':
        return None
    if 'avg_check_total' in row.values:
        return float(row.values['avg_check_total'] or 0.0)
    clients = float(row.values.get('clients') or 0.0)
    if not clients or 'revenue' not in row.values:
        return None
    return float(row.values.get('revenue') or 0.0) / clients


def _branch_values_from_staff(rows: list[ParsedPlanRow]) -> tuple[dict[str, float], set[str]]:
    values = _sum_values(rows)
    metric_codes = _metric_codes(rows) & RAW_PLAN_FACT_CODES
    barber_avg_checks = [
        value
        for row in rows
        if (value := _barber_avg_check_value(row)) is not None
    ]
    if barber_avg_checks:
        values['avg_check_total'] = sum(barber_avg_checks) / len(barber_avg_checks)
        metric_codes.add('avg_check_total')
    return values, metric_codes


def _metric_codes(rows: list[ParsedPlanRow]) -> set[str]:
    codes: set[str] = set()
    for row in rows:
        codes.update(row.metric_codes)
    return codes


def _has_zero_clients_plan(row: ParsedPlanRow) -> bool:
    return (
        row.scope == 'staff'
        and 'clients' in row.values
        and float(row.values.get('clients') or 0.0) == 0.0
    )


def _has_positive_plan_values(row: ParsedPlanRow) -> bool:
    return any(float(value or 0.0) > 0.0 for value in row.values.values())


def _is_non_working_staff_plan(row: ParsedPlanRow) -> bool:
    return row.scope == 'staff' and (
        _has_zero_clients_plan(row)
        or not _has_positive_plan_values(row)
    )


def _scope_count(rows: list[ParsedPlanRow], scope: str) -> int:
    return sum(1 for row in rows if row.scope == scope)


def _scope_metric_count(rows: list[ParsedPlanRow], scope: str) -> int:
    return sum(len(row.values) for row in rows if row.scope == scope)


def _plan_import_diagnostics(
    parsed_rows: list[ParsedPlanRow],
    effective_rows: list[ParsedPlanRow],
    imported: int,
) -> dict[str, Any]:
    return {
        'parsed_rows': {
            'total': len(parsed_rows),
            'network': _scope_count(parsed_rows, 'network'),
            'branch': _scope_count(parsed_rows, 'branch'),
            'staff': _scope_count(parsed_rows, 'staff'),
        },
        'effective_rows': {
            'total': len(effective_rows),
            'branch': _scope_count(effective_rows, 'branch'),
            'staff': _scope_count(effective_rows, 'staff'),
        },
        'parsed_metrics': {
            'network': _scope_metric_count(parsed_rows, 'network'),
            'branch': _scope_metric_count(parsed_rows, 'branch'),
            'staff': _scope_metric_count(parsed_rows, 'staff'),
        },
        'imported_metrics': {
            'total': imported,
            'branch': _scope_metric_count(effective_rows, 'branch'),
            'staff': _scope_metric_count(effective_rows, 'staff'),
        },
    }


async def _save_plan_values(
    db: AsyncSession,
    *,
    period_start: date,
    period_end: date,
    company_id: int,
    staff_id: int | None,
    staff_category: str | None,
    metric_codes: set[str] | frozenset[str],
    values: dict[str, float],
    source: str,
    updated_at: datetime,
) -> int:
    if not metric_codes:
        return 0

    await db.execute(
        delete(PlanMetric).where(
            *_branch_or_staff_filter(period_start, period_end, company_id, staff_id),
            PlanMetric.metric_code.in_(list(PLAN_FACT_METRIC_CODES | RETIRED_PLAN_METRIC_CODES)),
        )
    )

    imported = 0
    for metric_code, value in values.items():
        db.add(
            PlanMetric(
                period_start=period_start,
                period_end=period_end,
                company_id=company_id,
                staff_id=staff_id,
                staff_category=staff_category,
                metric_code=metric_code,
                value=value,
                source=source,
                updated_at=updated_at,
            )
        )
        imported += 1
    return imported


def _effective_import_rows(parsed_rows: list[ParsedPlanRow]) -> list[ParsedPlanRow]:
    """Build the persisted sheet snapshot: last row wins, branch plans mirror staff sums."""
    rows_by_key: dict[tuple[date, date, int, int | None], ParsedPlanRow] = {}
    for row in parsed_rows:
        if row.scope == 'network' or row.company_id is None:
            continue
        key = (row.period_start, row.period_end, row.company_id, row.staff_id)
        if _is_non_working_staff_plan(row):
            rows_by_key.pop(key, None)
            continue
        rows_by_key[key] = row

    staff_by_branch: dict[tuple[date, date, int], list[ParsedPlanRow]] = {}
    for row in rows_by_key.values():
        if row.scope == 'staff' and row.company_id is not None:
            staff_by_branch.setdefault((row.period_start, row.period_end, row.company_id), []).append(row)

    for key, rows in staff_by_branch.items():
        period_start, period_end, company_id = key
        values, metric_codes = _branch_values_from_staff(rows)
        if not values:
            continue
        rows_by_key[(period_start, period_end, company_id, None)] = ParsedPlanRow(
            row_index=0,
            period_start=period_start,
            period_end=period_end,
            company_id=company_id,
            staff_id=None,
            staff_category=None,
            values=values,
            metric_codes=frozenset(metric_codes),
            scope='branch',
        )

    return list(rows_by_key.values())


async def _replace_plan_values(
    db: AsyncSession,
    effective_rows: list[ParsedPlanRow],
    *,
    source: str,
    updated_at: datetime,
) -> int:
    if not effective_rows:
        return 0

    period_company_keys = {
        (row.period_start, row.period_end, row.company_id)
        for row in effective_rows
        if row.company_id is not None
    }
    for period_start, period_end, company_id in period_company_keys:
        await db.execute(
            delete(PlanMetric).where(
                PlanMetric.period_start == period_start,
                PlanMetric.period_end == period_end,
                PlanMetric.company_id == company_id,
                PlanMetric.metric_code.in_(list(PLAN_FACT_METRIC_CODES | RETIRED_PLAN_METRIC_CODES)),
            )
        )

    imported = 0
    for row in effective_rows:
        if row.company_id is None:
            continue
        row_source = f'{source}:staff_sum' if row.scope == 'branch' and row.row_index == 0 else source
        imported += await _save_plan_values(
            db,
            period_start=row.period_start,
            period_end=row.period_end,
            company_id=row.company_id,
            staff_id=row.staff_id,
            staff_category=row.staff_category,
            metric_codes=row.metric_codes,
            values=row.values,
            source=row_source,
            updated_at=updated_at,
        )
    return imported


def _validate_plan_totals(parsed_rows: list[ParsedPlanRow]) -> list[str]:
    warnings: list[str] = []

    effective_rows = _effective_import_rows(parsed_rows)
    branch_rows = [row for row in effective_rows if row.scope == 'branch']
    staff_rows = [row for row in effective_rows if row.scope == 'staff']
    network_rows = [row for row in parsed_rows if row.scope == 'network']

    branch_by_key = {
        (row.period_start, row.period_end, row.company_id): row
        for row in parsed_rows
        if row.scope == 'branch' and row.row_index > 0 and row.company_id is not None
    }
    staff_by_branch: dict[tuple[date, date, int], list[ParsedPlanRow]] = {}
    for row in staff_rows:
        if row.company_id is None:
            continue
        staff_by_branch.setdefault((row.period_start, row.period_end, row.company_id), []).append(row)

    for key, rows in staff_by_branch.items():
        branch = branch_by_key.get(key)
        if branch is None:
            continue
        branch_values = branch.values if branch else {}
        staff_values = _sum_values(rows)
        for metric_code, staff_value in staff_values.items():
            branch_value = branch_values.get(metric_code)
            if branch_value is None or abs(branch_value - staff_value) > VALIDATION_TOLERANCE:
                warnings.append(_validation_warning('branch staff total', key, metric_code, branch_value, staff_value))

    branches_by_period: dict[tuple[date, date], list[ParsedPlanRow]] = {}
    for row in branch_rows:
        branches_by_period.setdefault((row.period_start, row.period_end), []).append(row)

    for row in network_rows:
        key = (row.period_start, row.period_end)
        branch_values = _sum_values(branches_by_period.get(key, []))
        for metric_code, network_value in row.values.items():
            if metric_code not in RAW_PLAN_FACT_CODES:
                continue
            branch_value = branch_values.get(metric_code, 0.0)
            if abs(network_value - branch_value) > VALIDATION_TOLERANCE:
                warnings.append(_validation_warning('network branch total', key, metric_code, network_value, branch_value))

    return warnings


def _csv_text_from_url(url: str) -> str:
    url = _normalize_google_sheet_csv_url(url)
    with urllib.request.urlopen(url, timeout=30) as response:
        return response.read().decode('utf-8-sig')


def _normalize_google_sheet_csv_url(url: str) -> str:
    raw = str(url or '').strip()
    match = re.search(r'/spreadsheets/d/([^/]+)', raw)
    if not match or '/export?' in raw or '/gviz/' in raw:
        return raw

    parsed = urllib.parse.urlparse(raw)
    query = urllib.parse.parse_qs(parsed.query)
    fragment = urllib.parse.parse_qs(parsed.fragment)
    gid = (fragment.get('gid') or query.get('gid') or ['0'])[0]
    spreadsheet_id = match.group(1)
    export_query = urllib.parse.urlencode({'format': 'csv', 'gid': gid})
    return f'https://docs.google.com/spreadsheets/d/{spreadsheet_id}/export?{export_query}'


def _spreadsheet_id_from_url(url: str) -> str:
    match = re.search(r'/spreadsheets/d/([^/]+)', str(url or ''))
    return match.group(1) if match else ''


def _service_account_info() -> dict[str, Any] | None:
    if GOOGLE_SERVICE_ACCOUNT_JSON_B64:
        payload = base64.b64decode(GOOGLE_SERVICE_ACCOUNT_JSON_B64).decode('utf-8')
        return json.loads(payload)
    if GOOGLE_SERVICE_ACCOUNT_FILE:
        with open(GOOGLE_SERVICE_ACCOUNT_FILE, encoding='utf-8') as file_obj:
            return json.load(file_obj)
    return None


def _base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _google_service_account_access_token(service_account: dict[str, Any]) -> str:
    now = int(time.time())
    token_uri = service_account.get('token_uri') or GOOGLE_OAUTH_TOKEN_URI
    header = {'alg': 'RS256', 'typ': 'JWT'}
    claims = {
        'iss': service_account['client_email'],
        'scope': GOOGLE_SHEETS_READONLY_SCOPE,
        'aud': token_uri,
        'iat': now,
        'exp': now + 3600,
    }
    signing_input = (
        _base64url(json.dumps(header, separators=(',', ':')).encode('utf-8'))
        + '.'
        + _base64url(json.dumps(claims, separators=(',', ':')).encode('utf-8'))
    ).encode('ascii')

    key_file_path = ''
    try:
        with tempfile.NamedTemporaryFile('w', delete=False) as key_file:
            key_file.write(service_account['private_key'])
            key_file_path = key_file.name
        os.chmod(key_file_path, 0o600)
        signature = subprocess.check_output(
            ['openssl', 'dgst', '-sha256', '-sign', key_file_path],
            input=signing_input,
        )
    finally:
        if key_file_path:
            try:
                os.unlink(key_file_path)
            except FileNotFoundError:
                pass

    assertion = signing_input.decode('ascii') + '.' + _base64url(signature)
    body = urllib.parse.urlencode({
        'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer',
        'assertion': assertion,
    }).encode('utf-8')
    request = urllib.request.Request(token_uri, data=body, method='POST')
    with urllib.request.urlopen(request, timeout=30) as response:
        token_data = json.loads(response.read().decode('utf-8'))
    return token_data['access_token']


def _google_sheet_values_to_csv_text(values: list[list[Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    for row in values:
        writer.writerow([str(value) if value is not None else '' for value in row])
    return out.getvalue()


def _sheet_csv_text_from_service_account(sheet_id: str, sheet_name: str) -> str:
    service_account = _service_account_info()
    if not service_account:
        raise ValueError('Google service account is not configured')
    if not sheet_id:
        raise ValueError('Google sheet id is not configured')
    sheet_name = sheet_name or 'services'
    access_token = _google_service_account_access_token(service_account)
    range_name = urllib.parse.quote(sheet_name, safe='')
    url = (
        f'https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/'
        f'{range_name}?majorDimension=ROWS'
    )
    request = urllib.request.Request(url, headers={'Authorization': f'Bearer {access_token}'})
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode('utf-8'))
    return _google_sheet_values_to_csv_text(payload.get('values') or [])


def _sheet_csv_url_from_spreadsheet_url(url: str, sheet_name: str) -> str:
    match = re.search(r'/spreadsheets/d/([^/]+)', url or '')
    if not match:
        return ''
    spreadsheet_id = match.group(1)
    query = urllib.parse.urlencode({'tqx': 'out:csv', 'sheet': sheet_name})
    return f'https://docs.google.com/spreadsheets/d/{spreadsheet_id}/gviz/tq?{query}'


async def import_plan_sheet_csv(db: AsyncSession, csv_text: str, source: str = 'google_sheet') -> dict[str, Any]:
    company_rows = (await db.execute(select(Company.id, Company.title))).all()
    companies_by_title = {_normalize(row.title): int(row.id) for row in company_rows}
    company_ids = {int(row.id) for row in company_rows}
    staff_rows = (await db.execute(select(Staff.id, Staff.name, Staff.company_id, Staff.position))).all()
    staff_by_id = {int(row.id): row for row in staff_rows}
    staff_by_company_name = {
        (int(row.company_id), _normalize(row.name)): row
        for row in staff_rows
    }

    metric_headers = {
        _normalize(alias): code
        for code, aliases in METRIC_COLUMN_ALIASES.items()
        for alias in aliases
    }

    reader = csv.DictReader(io.StringIO(csv_text))
    now = datetime.utcnow()
    skipped: list[str] = []
    warnings: list[str] = []
    parsed_rows: list[ParsedPlanRow] = []

    for index, row in enumerate(reader, start=2):
        try:
            period_start, period_end = _parse_period(row)
        except ValueError as exc:
            skipped.append(f'row {index}: {exc}')
            continue

        company_id_value = _find_value(row, COMPANY_ID_ALIASES)
        branch_value = _find_value(row, BRANCH_ALIASES)
        is_network = _normalize(branch_value) in NETWORK_NAMES

        company_id_text = company_id_value.strip()
        company_id = int(company_id_text) if company_id_text.isdigit() else None
        if company_id is None and branch_value:
            company_id = companies_by_title.get(_normalize(branch_value))

        staff_id_value = _find_value(row, STAFF_ID_ALIASES)
        staff_name_value = _find_value(row, STAFF_NAME_ALIASES)
        staff_id_text = staff_id_value.strip()
        staff_id = int(staff_id_text) if staff_id_text.isdigit() else None
        staff_row = None
        if staff_id is not None:
            staff_row = staff_by_id.get(staff_id)
            if staff_row is None:
                skipped.append(f'row {index}: unknown staff {staff_id}')
                continue
            if company_id is not None and int(staff_row.company_id) != company_id:
                skipped.append(f'row {index}: staff {staff_id} does not belong to company {company_id}')
                continue
            company_id = int(staff_row.company_id)
        elif staff_name_value and company_id is not None:
            staff_row = staff_by_company_name.get((company_id, _normalize(staff_name_value)))
            if staff_row is None:
                skipped.append(f'row {index}: unknown staff {staff_name_value}')
                continue
            staff_id = int(staff_row.id)

        if not is_network and company_id not in company_ids:
            skipped.append(f'row {index}: unknown company {branch_value or company_id_value}')
            continue

        staff_category = None
        if staff_id is not None:
            staff_category = normalize_staff_category(_find_value(row, STAFF_CATEGORY_ALIASES))
            if staff_category is None and staff_row is not None:
                staff_category = normalize_staff_category(staff_row.position)
            if staff_category not in STAFF_CATEGORY_METRIC_CODES:
                skipped.append(f'row {index}: unknown staff category for staff {staff_id}')
                continue

        values: dict[str, float] = {}
        row_metric_codes: set[str] = set()
        for header, raw_value in row.items():
            metric_code = metric_headers.get(_normalize(header))
            if not metric_code:
                continue
            row_metric_codes.add(metric_code)
            number = _parse_number(raw_value)
            if number is not None:
                values[metric_code] = number

        if staff_category:
            allowed_codes = set(STAFF_CATEGORY_METRIC_CODES[staff_category])
            invalid_codes = sorted(set(values) - allowed_codes)
            if invalid_codes:
                warnings.append(f'row {index}: skipped metrics not applicable to {staff_category}: {", ".join(invalid_codes)}')
            values = {
                metric_code: value
                for metric_code, value in values.items()
                if metric_code in allowed_codes
            }

        if not values:
            skipped.append(f'row {index}: no metric values')
            continue

        scope = 'network' if is_network else ('staff' if staff_id is not None else 'branch')
        parsed_row = ParsedPlanRow(
            row_index=index,
            period_start=period_start,
            period_end=period_end,
            company_id=company_id,
            staff_id=staff_id,
            staff_category=staff_category,
            values=values,
            metric_codes=frozenset(row_metric_codes),
            scope=scope,
        )
        parsed_rows.append(parsed_row)

    effective_rows = _effective_import_rows(parsed_rows)
    imported = await _replace_plan_values(db, effective_rows, source=source, updated_at=now)
    await db.commit()
    warnings.extend(_validate_plan_totals(parsed_rows))
    diagnostics = _plan_import_diagnostics(parsed_rows, effective_rows, imported)
    if diagnostics['parsed_rows']['branch'] and not diagnostics['parsed_rows']['staff']:
        warnings.append('plan sheet has no staff rows; staff plans will be empty')
    return {
        'imported': imported,
        'skipped': skipped,
        'warnings': warnings,
        'diagnostics': diagnostics,
    }


async def import_services_sheet_csv(
    db: AsyncSession,
    csv_text: str,
    source: str = 'google_sheet:services',
) -> dict[str, Any]:
    service_rows = (
        await db.execute(
            select(
                ServiceCatalog.service_id.label('service_id'),
                ServiceCatalog.title.label('title'),
                ServiceCatalog.company_id.label('company_id'),
                ServiceCatalog.category_title.label('category_title'),
            )
        )
    ).all()
    if not service_rows:
        service_rows = (
            await db.execute(
                select(
                    Service.id.label('service_id'),
                    Service.title.label('title'),
                    Service.company_id.label('company_id'),
                    Service.category_title.label('category_title'),
                )
            )
        ).all()
    legacy_service_ids = {
        int(row.id)
        for row in (await db.execute(select(Service.id))).all()
    }
    services_by_id: dict[int, list[Any]] = {}
    services_by_company_title: dict[tuple[int, str], list[Any]] = {}
    services_by_title: dict[str, list[Any]] = {}
    for row in service_rows:
        services_by_id.setdefault(int(row.service_id), []).append(row)
        services_by_company_title.setdefault((int(row.company_id), _normalize(row.title)), []).append(row)
        services_by_title.setdefault(_normalize(row.title), []).append(row)

    company_rows = (await db.execute(select(Company.id, Company.title))).all()
    companies_by_title = {_normalize(row.title): int(row.id) for row in company_rows}
    company_ids = {int(row.id) for row in company_rows}
    service_company_ids_by_id: dict[int, set[int]] = {}
    for row in service_rows:
        service_company_ids_by_id.setdefault(int(row.service_id), set()).add(int(row.company_id))

    reader = csv.DictReader(io.StringIO(csv_text))
    now = datetime.utcnow()
    skipped: list[str] = []
    warnings: list[str] = []
    labels_by_service_key: dict[tuple[int, int], ServiceLabel] = {}
    processed_markers = 0

    for index, row in enumerate(reader, start=2):
        service_id_value = _find_value(row, SERVICE_ID_ALIASES).strip()
        service_id = int(service_id_value) if service_id_value.isdigit() else None
        matched_services = list(services_by_id.get(service_id, [])) if service_id is not None else []
        matched_by_id = bool(matched_services)
        title_value = _find_value(row, SERVICE_TITLE_ALIASES)
        category_value = _find_value(row, SERVICE_CATEGORY_ALIASES)
        category_key = _normalize(category_value)

        company_id_value = _find_value(row, COMPANY_ID_ALIASES).strip()
        branch_value = _find_value(row, BRANCH_ALIASES)
        has_company_scope = bool(company_id_value or branch_value)
        company_id = int(company_id_value) if company_id_value.isdigit() else None
        if company_id is None:
            company_id = companies_by_title.get(_normalize(branch_value))
        if has_company_scope and company_id is None:
            skipped.append(f'row {index}: unknown company {branch_value or company_id_value}')
            continue
        if company_id is not None and company_id not in company_ids:
            skipped.append(f'row {index}: unknown company {branch_value or company_id_value}')
            continue

        if not matched_services:
            if company_id is not None and title_value:
                title_key = _normalize(title_value)
                matched_services = list(services_by_company_title.get((company_id, title_key), []))
            elif title_value:
                matched_services = list(services_by_title.get(_normalize(title_value), []))

        if company_id is not None:
            matched_services = [
                matched_service
                for matched_service in matched_services
                if (
                    matched_service is not None
                    and (
                        int(matched_service.company_id) == company_id
                        or company_id in service_company_ids_by_id.get(int(matched_service.service_id), set())
                    )
                )
            ]
        if category_key and not matched_by_id:
            category_matches = [
                matched_service
                for matched_service in matched_services
                if matched_service is not None and _normalize(matched_service.category_title) == category_key
            ]
            if category_matches:
                matched_services = category_matches

        if not matched_services:
            skipped.append(f'row {index}: unknown service {service_id_value or _find_value(row, SERVICE_TITLE_ALIASES)}')
            continue

        marker_value = _find_value(row, SERVICE_EXTRA_ALIASES) or _find_value(row, SERVICE_TAG_ALIASES)
        is_extra = _parse_marker(marker_value)
        if is_extra is None:
            skipped.append(f'row {index}: no extra-service marker')
            continue

        processed_markers += 1
        if is_extra:
            for matched_service in matched_services:
                service_id = int(matched_service.service_id)
                if service_id not in legacy_service_ids:
                    skipped.append(f'row {index}: service {service_id} is missing in legacy services table')
                    continue
                label_company_ids = (
                    {company_id}
                    if company_id is not None
                    else service_company_ids_by_id.get(service_id, {int(matched_service.company_id)})
                )
                for label_company_id in label_company_ids:
                    if label_company_id not in company_ids:
                        continue
                    labels_by_service_key[(service_id, label_company_id)] = ServiceLabel(
                        service_id=service_id,
                        company_id=label_company_id,
                        is_extra=True,
                        source=source,
                        updated_at=now,
                    )

    if processed_markers == 0:
        warnings.append('services sheet has no rows with extra-service marker; labels unchanged')
        return {'imported': 0, 'processed': 0, 'skipped': skipped, 'warnings': warnings}

    await db.execute(delete(ServiceLabel))
    imported = 0
    for label in labels_by_service_key.values():
        db.add(label)
        imported += 1
    await db.commit()
    return {
        'imported': imported,
        'processed': processed_markers,
        'skipped': skipped,
        'warnings': warnings,
    }


async def import_services_sheet_from_config(db: AsyncSession) -> dict[str, Any]:
    services_url = SERVICES_SHEET_CSV_URL or _sheet_csv_url_from_spreadsheet_url(
        PLAN_SHEET_CSV_URL,
        'services',
    )
    sheet_id = (
        SERVICES_SHEET_ID
        or _spreadsheet_id_from_url(services_url)
        or PLAN_SHEET_ID
        or _spreadsheet_id_from_url(PLAN_SHEET_CSV_URL)
    )
    csv_error = None

    if services_url:
        try:
            csv_text = await asyncio.to_thread(_csv_text_from_url, services_url)
        except Exception as exc:
            csv_error = exc
        else:
            return await import_services_sheet_csv(db, csv_text, source='google_sheet:services')

    try:
        csv_text = await asyncio.to_thread(
            _sheet_csv_text_from_service_account,
            sheet_id,
            SERVICES_SHEET_NAME or 'services',
        )
    except Exception as exc:
        skipped = []
        if not services_url and not sheet_id:
            skipped.append('services sheet CSV URL, SERVICES_SHEET_ID or PLAN_SHEET_ID is not configured')
        if csv_error is not None:
            skipped.append(f'services sheet CSV URL is unavailable: {csv_error}')
        skipped.append(f'services sheet service account read failed: {exc}')
        return {'imported': 0, 'processed': 0, 'skipped': skipped, 'warnings': []}

    return await import_services_sheet_csv(db, csv_text, source='google_sheet:services')


async def import_plan_sheet_from_config(db: AsyncSession) -> dict[str, Any]:
    sheet_id = PLAN_SHEET_ID or _spreadsheet_id_from_url(PLAN_SHEET_CSV_URL)
    service_account_error = None
    csv_error = None

    csv_text = None
    if sheet_id:
        try:
            csv_text = await asyncio.to_thread(
                _sheet_csv_text_from_service_account,
                sheet_id,
                PLAN_SHEET_NAME or 'plan',
            )
        except Exception as exc:
            service_account_error = exc

    if csv_text is None:
        if PLAN_SHEET_CSV_URL:
            try:
                csv_text = await asyncio.to_thread(_csv_text_from_url, PLAN_SHEET_CSV_URL)
            except Exception as exc:
                csv_error = exc

    if csv_text is None:
        skipped = []
        if not PLAN_SHEET_CSV_URL and not sheet_id:
            skipped.append('PLAN_SHEET_CSV_URL or PLAN_SHEET_ID is not configured')
        if service_account_error is not None:
            skipped.append(f'plan sheet service account read failed: {service_account_error}')
        if csv_error is not None:
            skipped.append(f'plan sheet CSV URL is unavailable: {csv_error}')
        return {'imported': 0, 'skipped': skipped}

    result = await import_plan_sheet_csv(db, csv_text, source='google_sheet')
    result['services'] = await import_services_sheet_from_config(db)
    return result
