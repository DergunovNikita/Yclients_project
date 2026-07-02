"""Declarative mapping profiles for file-import sources.

A gestionale export (e.g. Area Salon, BeautyCheck) uses its own column names,
locale and layout. Instead of hardcoding one vendor, a `MappingProfile` describes
`canonical_field -> source_column` per entity plus how to parse the file's locale.
Onboarding a new vendor becomes a config change, not a code change.

The exact Area Salon export columns are NOT yet confirmed (that is a Phase 0
discovery task — obtain a real export file). `AREA_SALON_PROFILE` below is a
best-guess template with Italian headers; adjust the column names once the real
export is available.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional

_THOUSANDS_DOT = re.compile(r'\.(?=\d{3}(\D|$))')


@dataclass(frozen=True)
class LocaleFormat:
    """How numbers and dates are written in the source file."""

    decimal_comma: bool = True   # Italian: "1.234,56"
    day_first: bool = True       # Italian: "gg/mm/aaaa"


def parse_amount(value, locale: LocaleFormat) -> Optional[float]:
    """Parse a monetary/number cell into float, honouring locale decimal style."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    text = text.replace('€', '').replace('\xa0', '').replace(' ', '').strip()
    if locale.decimal_comma:
        text = _THOUSANDS_DOT.sub('', text).replace(',', '.')
    else:
        text = text.replace(',', '')
    try:
        return float(text)
    except ValueError:
        return None


def parse_iso_date(value, locale: LocaleFormat) -> Optional[str]:
    """Parse a date cell into an ISO 'YYYY-MM-DD' string the pipeline accepts."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return None
    m = re.match(r'^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})', text)
    if not m:
        try:
            return date.fromisoformat(text[:10]).isoformat()
        except ValueError:
            return None
    a, b, c = (int(g) for g in m.groups())
    if a > 31:  # already year-first (YYYY-MM-DD)
        year, month, day = a, b, c
    elif locale.day_first:
        day, month, year = a, b, c
    else:
        month, day, year = a, b, c
    if year < 100:
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_iso_datetime(value, locale: LocaleFormat) -> Optional[str]:
    """Parse a date/time cell into an ISO datetime string when time is present."""
    if value in (None, ''):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None).isoformat()
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time()).isoformat()
    text = str(value).strip()
    if not text:
        return None

    normalized = text.replace('Z', '+00:00').replace(' ', 'T', 1) if 'T' not in text and ' ' in text else text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
        return parsed.replace(tzinfo=None).isoformat()
    except ValueError:
        pass

    m = re.match(
        r'^(\d{1,4})[/.\-](\d{1,2})[/.\-](\d{1,4})(?:\s+(\d{1,2}):(\d{2})(?::(\d{2}))?)?',
        text,
    )
    if not m:
        parsed_date = parse_iso_date(text, locale)
        return f'{parsed_date}T00:00:00' if parsed_date else None
    a, b, c = (int(g) for g in m.groups()[:3])
    if a > 31:
        year, month, day = a, b, c
    elif locale.day_first:
        day, month, year = a, b, c
    else:
        month, day, year = a, b, c
    if year < 100:
        year += 2000
    hour = int(m.group(4) or 0)
    minute = int(m.group(5) or 0)
    second = int(m.group(6) or 0)
    try:
        return datetime(year, month, day, hour, minute, second).isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class MappingProfile:
    """Maps a vendor export to canonical (YClients-shaped) entities.

    Each mapping is `{canonical_field: source_column_header}`. Only entities the
    export actually provides need to be filled; the rest stay empty and the
    corresponding sync steps simply find no data.
    """

    name: str
    locale: LocaleFormat = field(default_factory=LocaleFormat)
    clients: dict[str, str] = field(default_factory=dict)
    records: dict[str, str] = field(default_factory=dict)
    services: dict[str, str] = field(default_factory=dict)

    # canonical fields parsed as amounts / dates (subset of the maps above)
    amount_fields: frozenset[str] = frozenset({'cost', 'first_cost', 'discount'})
    date_fields: frozenset[str] = frozenset(
        {'birth_date', 'last_visit_date', 'date', 'create_date'}
    )
    datetime_fields: frozenset[str] = frozenset({'datetime'})


# Best-guess template — column names are UNCONFIRMED, adjust after Phase 0 discovery.
AREA_SALON_PROFILE = MappingProfile(
    name='area_salon',
    locale=LocaleFormat(decimal_comma=True, day_first=True),
    clients={
        'id': 'ID',
        'name': 'Nome',
        'phone': 'Telefono',
        'email': 'Email',
        'birth_date': 'Data di nascita',
    },
    records={
        'id': 'ID Appuntamento',
        'client_id': 'ID Cliente',
        'staff_id': 'ID Operatore',
        'date': 'Data',
        'datetime': 'Data e ora',
    },
    services={
        'record_id': 'ID Appuntamento',
        'id': 'ID Servizio',
        'title': 'Servizio',
        'cost': 'Prezzo',
        'amount': 'Quantità',
    },
)

PROFILES: dict[str, MappingProfile] = {AREA_SALON_PROFILE.name: AREA_SALON_PROFILE}


def get_profile(name: str) -> MappingProfile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(f'Unknown mapping profile: {name}. Known: {", ".join(PROFILES)}')


__all__ = [
    'LocaleFormat',
    'MappingProfile',
    'parse_amount',
    'parse_iso_date',
    'parse_iso_datetime',
    'AREA_SALON_PROFILE',
    'PROFILES',
    'get_profile',
]
