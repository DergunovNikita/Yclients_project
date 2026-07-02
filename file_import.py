"""File-import source: turn a gestionale export into a `SyncSource`.

For vendors without a usable public API (e.g. Area Salon), a salon periodically
exports CSV files. `FileImportSyncClient` reads those files through a
`MappingProfile` and exposes the same `get_*` methods as `YClientsAPI`, so the
existing `sync_pipeline` consumes it unchanged.

Idempotency: rows are keyed by the source `id` when present. When an export has
no stable id, a deterministic synthetic key is derived from the row's business
fields so re-importing the same file does not duplicate data.
"""

from __future__ import annotations

import csv
import hashlib
from collections import defaultdict
from dataclasses import dataclass
from typing import IO, Optional

from mapping_profiles import MappingProfile, get_profile, parse_amount, parse_iso_date, parse_iso_datetime

SOURCE_FILE_IMPORT = 'file_import'
MAX_SIGNED_INT = 2_147_483_647


def _synthetic_id(*parts) -> int:
    digest = hashlib.sha1('|'.join('' if p is None else str(p) for p in parts).encode()).hexdigest()
    return int(digest[:16], 16) % MAX_SIGNED_INT + 1


def _source_id(profile_name: str, company_id: str, entity: str, source_id) -> int:
    return _synthetic_id(SOURCE_FILE_IMPORT, profile_name, company_id, entity, source_id)


def _map_row(row: dict, mapping: dict[str, str], profile: MappingProfile) -> dict:
    """Project one source row onto canonical fields, parsing amounts/dates."""
    out: dict = {}
    for canonical, column in mapping.items():
        raw = row.get(column)
        if canonical in profile.amount_fields:
            out[canonical] = parse_amount(raw, profile.locale)
        elif canonical in profile.datetime_fields:
            out[canonical] = parse_iso_datetime(raw, profile.locale)
        elif canonical in profile.date_fields:
            out[canonical] = parse_iso_date(raw, profile.locale)
        else:
            out[canonical] = raw if raw not in ('', None) else None
    return out


def read_csv(source: IO | str) -> list[dict]:
    """Read a CSV file path or open text stream into a list of row dicts."""
    if isinstance(source, str):
        with open(source, newline='', encoding='utf-8-sig') as fh:
            return list(csv.DictReader(fh))
    return list(csv.DictReader(source))


@dataclass
class ImportPayload:
    """Raw rows for each supported entity, as read from the export files."""

    clients: list[dict] = None
    records: list[dict] = None

    def __post_init__(self):
        self.clients = self.clients or []
        self.records = self.records or []


class FileImportSyncClient:
    """`SyncSource` backed by parsed export rows and a mapping profile."""

    def __init__(self, profile: MappingProfile, payload: ImportPayload):
        self.profile = profile
        self.payload = payload

    def authenticate(self) -> bool:
        return True

    def get_clients(self, company_id: str) -> list[dict]:
        result = []
        for row in self.payload.clients:
            mapped = _map_row(row, self.profile.clients, self.profile)
            source_id = mapped.get('id') or _synthetic_id(
                mapped.get('phone'),
                mapped.get('email'),
                mapped.get('name'),
            )
            mapped['id'] = _source_id(self.profile.name, company_id, 'client', source_id)
            result.append(mapped)
        return result

    def get_records(self, company_id: str, start_date: Optional[str] = None,
                    end_date: Optional[str] = None) -> list[dict]:
        # Export layout: one row per service line. Group lines into appointments.
        grouped: dict = defaultdict(lambda: {'services': []})
        line_key = self.profile.services.get('record_id')
        for row in self.payload.records:
            head = _map_row(row, self.profile.records, self.profile)
            line = _map_row(row, self.profile.services, self.profile)

            record_id = head.get('id') or (row.get(line_key) if line_key else None)
            if record_id is None:
                record_id = _synthetic_id(head.get('date'), head.get('client_id'), head.get('staff_id'))
            record_id = _source_id(self.profile.name, company_id, 'record', record_id)

            rec = grouped[record_id]
            rec['id'] = record_id
            rec['date'] = head.get('date')
            rec['datetime'] = head.get('datetime') or head.get('date')
            rec['create_date'] = head.get('create_date')
            staff_source_id = head.get('staff_id')
            client_source_id = head.get('client_id')
            rec['staff_id'] = (
                _source_id(self.profile.name, company_id, 'staff', staff_source_id)
                if staff_source_id not in (None, '')
                else None
            )
            rec['client'] = {
                'id': (
                    _source_id(self.profile.name, company_id, 'client', client_source_id)
                    if client_source_id not in (None, '')
                    else None
                )
            }
            rec.setdefault('attendance', 1)

            if any(v is not None for k, v in line.items() if k != 'record_id'):
                svc_id = line.get('id')
                cost = line.get('cost')
                rec['services'].append({
                    'id': _source_id(
                        self.profile.name,
                        company_id,
                        'service',
                        svc_id if svc_id is not None else line.get('title'),
                    ),
                    'title': line.get('title') or '',
                    'cost': cost,
                    'first_cost': cost,
                    'amount': _as_int(line.get('amount')) or 1,
                })

        records = list(grouped.values())
        if start_date or end_date:
            records = [r for r in records if _in_window(r.get('date'), start_date, end_date)]
        return records

    # --- entities a plain export does not provide: pipeline handles empties ---
    def get_groups(self) -> list:
        return []

    def get_service_categories(self, company_id: str) -> list:
        return []

    def get_services(self, company_id: str, staff_id=None, category_id=None) -> list:
        return []

    def get_positions(self, company_id: str) -> list:
        return []

    def get_staff(self, company_id: str) -> list:
        return []

    def get_accounts(self, company_id: str) -> list:
        return []

    def get_storages(self, company_id: str) -> list:
        return []

    def get_good_categories(self, company_id: str) -> list:
        return []

    def get_goods(self, company_id: str) -> list:
        return []

    def get_financial_transactions(self, company_id: str, start_date=None, end_date=None) -> list:
        return []

    def get_goods_transactions(self, company_id: str, start_date=None, end_date=None) -> list:
        return []

    def get_comments(self, company_id: str, start_date=None, end_date=None) -> list:
        return []

    def get_staff_schedule(self, company_id: str, start_date=None, end_date=None) -> list:
        return []

    def get_analytics_overall(self, company_id: str, date_from=None, date_to=None) -> None:
        return None


def _as_int(value) -> Optional[int]:
    if value in (None, ''):
        return None
    try:
        return int(str(value).strip())
    except (ValueError, TypeError):
        return None


def _in_window(iso_date: Optional[str], start: Optional[str], end: Optional[str]) -> bool:
    if not iso_date:
        return True
    if start and iso_date < start:
        return False
    if end and iso_date > end:
        return False
    return True


def build_file_import_client(profile_name: str, payload: ImportPayload) -> FileImportSyncClient:
    return FileImportSyncClient(get_profile(profile_name), payload)


def load_payload_from_csv(clients: IO | str | None = None,
                          records: IO | str | None = None) -> ImportPayload:
    return ImportPayload(
        clients=read_csv(clients) if clients is not None else [],
        records=read_csv(records) if records is not None else [],
    )


__all__ = [
    'SOURCE_FILE_IMPORT',
    'FileImportSyncClient',
    'ImportPayload',
    'build_file_import_client',
    'load_payload_from_csv',
    'read_csv',
]
