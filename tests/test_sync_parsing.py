from datetime import date, datetime, time, timedelta, timezone

from sync_parsing import parse_date, parse_datetime, parse_datetime_end, parse_datetime_start, parse_time


def test_parse_date_accepts_iso_values():
    assert parse_date('2026-03-28') == date(2026, 3, 28)
    assert parse_date('2026-03-28T11:22:33') == date(2026, 3, 28)


def test_parse_datetime_accepts_timestamp_values():
    assert parse_datetime('2026-03-28 11:22:33') == datetime(2026, 3, 28, 11, 22, 33)
    assert parse_datetime('2026-03-28') == datetime(2026, 3, 28, 0, 0, 0)


def test_parse_datetime_normalizes_aware_values_to_utc_without_host_timezone():
    source_timezone = timezone(timedelta(hours=3))

    assert parse_datetime(datetime(2026, 3, 28, 11, 22, tzinfo=source_timezone)) == datetime(
        2026, 3, 28, 8, 22
    )
    assert parse_datetime('2026-03-28T11:22:00+03:00') == datetime(2026, 3, 28, 8, 22)
    assert parse_datetime('2026-03-28T08:22:00Z') == datetime(2026, 3, 28, 8, 22)


def test_parse_time_accepts_hh_mm_and_hh_mm_ss():
    assert parse_time('09:30') == time(9, 30)
    assert parse_time('09:30:15') == time(9, 30, 15)
    assert parse_time('24:00') == time.min
    assert parse_time('24:00:00') == time.min


def test_parse_datetime_range_bounds_expand_date_only_values():
    assert parse_datetime_start('2026-03-28') == datetime(2026, 3, 28, 0, 0, 0)
    assert parse_datetime_end('2026-03-28') == datetime(2026, 3, 28, 23, 59, 59, 999999)


def test_parsers_return_none_for_invalid_values():
    assert parse_date('not-a-date') is None
    assert parse_datetime('still-not-a-date') is None
    assert parse_time('99:99') is None
