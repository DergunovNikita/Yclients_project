from __future__ import annotations

from datetime import date, datetime, time
from typing import Any


def parse_date(value: Any) -> date | None:
    if value in (None, '', '0000-00-00'):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate)
    except ValueError:
        return None


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, '', '0000-00-00 00:00:00'):
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    text = str(value).strip()
    if not text:
        return None
    normalized = text.replace('Z', '+00:00').replace(' ', 'T', 1) if 'T' not in text and ' ' in text else text.replace('Z', '+00:00')
    try:
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is not None:
            return parsed.astimezone().replace(tzinfo=None)
        return parsed
    except ValueError:
        pass
    date_part = parse_date(text)
    if date_part is not None:
        return datetime.combine(date_part, time.min)
    return None


def parse_time(value: Any) -> time | None:
    if value in (None, ''):
        return None
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if isinstance(value, datetime):
        return value.time().replace(tzinfo=None)
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:8]
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(candidate, fmt).time()
        except ValueError:
            continue
    return None


def parse_datetime_start(value: Any) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_datetime_end(value: Any) -> datetime | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.replace(hour=23, minute=59, second=59, microsecond=999999)


def parse_int(value: Any) -> int | None:
    """Coerce a YClients numeric field to int, treating blanks as absent.

    Historical records return '' for optional numeric fields such as expense.id,
    which Postgres rejects for an integer column.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        try:
            return int(float(text))
        except ValueError:
            return None
