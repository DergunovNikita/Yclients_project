"""Capture dashboard payloads and compare them across a change.

An optimisation that changes a number is not an optimisation, and the dashboard has no
golden-payload coverage: the suite checks named fields, not whole responses. This walks
the report catalogue over a matrix of periods and scopes and hashes every answer.

Payloads are stored and compared numerically rather than hashed. Hashing looked simpler
and does not work here: `sum()` over `double precision` is not associative, so a plan that
accumulates rows in a different order lands on a different last bit. Measured on this data,
the same query on an unchanged schema returns 94121226.00000003 and 94121226.00000004 on
alternating runs — a relative 1e-16, invisible once money is formatted, but enough to
change any hash. Rounding before hashing only moves the problem to the rounding boundary.
So every number is compared against RELATIVE_TOLERANCE instead, which ignores machine
noise and still catches anything a person could notice.

    python -m scripts.report_payload_snapshot --out before.json
    # ... apply the change ...
    python -m scripts.report_payload_snapshot --compare before.json
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any, Iterator

import requests

DEFAULT_BASE_URL = 'http://127.0.0.1:8020'
TIMEOUT_SECONDS = 300

# Anchored on a fixed day so a snapshot stays comparable across calendar days.
TODAY = date(2026, 9, 7)
PERIODS: tuple[tuple[str, str, str], ...] = (
    ('day', '2026-09-07', '2026-09-07'),
    ('week', '2026-09-01', '2026-09-07'),
    ('month_to_date', '2026-09-01', '2026-09-07'),
    ('whole_month', '2026-08-01', '2026-08-31'),
    ('quarter', '2026-07-01', '2026-09-07'),
    ('year', '2025-01-01', '2025-12-31'),
)

WIDGETS = (
    'widget/summary',
    'widget/plan_fact',
    'widget/revenue_daily',
    'widget/top_services',
    'widget/extra_services',
)


# Machine noise sits at ~1e-16; the smallest change worth a person's attention is many
# orders of magnitude above it. Anything in between would be invisible in the UI anyway.
RELATIVE_TOLERANCE = 1e-9


def compare(before: Any, after: Any, path: str = '') -> list[str]:
    """Structural comparison that forgives float noise and nothing else."""
    if isinstance(before, bool) or isinstance(after, bool):
        return [] if before == after else [f'{path}: {before} -> {after}']
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        scale = max(abs(before), abs(after))
        if abs(before - after) <= RELATIVE_TOLERANCE * scale:
            return []
        return [f'{path}: {before} -> {after}']
    if type(before) is not type(after):
        return [f'{path}: type {type(before).__name__} -> {type(after).__name__}']
    if isinstance(before, dict):
        issues = []
        for key in sorted(set(before) | set(after)):
            if key not in before:
                issues.append(f'{path}.{key}: added')
            elif key not in after:
                issues.append(f'{path}.{key}: removed')
            else:
                issues += compare(before[key], after[key], f'{path}.{key}')
        return issues
    if isinstance(before, list):
        if len(before) != len(after):
            return [f'{path}: length {len(before)} -> {len(after)}']
        issues = []
        for index, (left, right) in enumerate(zip(before, after)):
            issues += compare(left, right, f'{path}[{index}]')
        return issues
    return [] if before == after else [f'{path}: {before!r} -> {after!r}']


def api_key() -> str:
    with open('.env', encoding='utf-8') as handle:
        for line in handle:
            if line.startswith('API_KEY='):
                return line.split('=', 1)[1].strip()
    raise SystemExit('API_KEY not found in .env')


def get(session: requests.Session, base_url: str, path: str, params: dict[str, Any]) -> Any:
    response = session.get(
        f'{base_url}/dashboard/{path}',
        params={key: value for key, value in params.items() if value is not None},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code != 200:
        return {'__status__': response.status_code}
    return response.json()


def scopes(session: requests.Session, base_url: str) -> list[tuple[str, dict[str, Any]]]:
    """Network, one branch with a reporting start, one without, and a staff filter."""
    branches = get(session, base_url, 'branches', {}).get('data') or []
    with_start = next((b for b in branches if b.get('reporting_start_date')), None)
    without_start = next((b for b in branches if not b.get('reporting_start_date')), None)
    result: list[tuple[str, dict[str, Any]]] = [('network', {})]
    if with_start:
        result.append((f"branch_{with_start['id']}", {'company_id': with_start['id']}))
        staff = get(session, base_url, 'staff', {'company_id': with_start['id']}).get('data') or []
        if staff:
            result.append((
                f"branch_{with_start['id']}_staff_{staff[0]['id']}",
                {'company_id': with_start['id'], 'staff_id': staff[0]['id']},
            ))
    if without_start:
        result.append((f"branch_{without_start['id']}", {'company_id': without_start['id']}))
    return result


def cases(session: requests.Session, base_url: str) -> Iterator[tuple[str, str, dict[str, Any]]]:
    catalogue = get(session, base_url, 'reports', {}).get('data') or []
    report_ids = sorted({item['id'] for item in catalogue if item.get('id')})
    for scope_name, scope in scopes(session, base_url):
        for period_name, start, end in PERIODS:
            window = {'start_date': start, 'end_date': end, **scope}
            for widget in WIDGETS:
                yield f'{widget}|{period_name}|{scope_name}', widget, window
            for report_id in report_ids:
                yield (
                    f'{report_id}|{period_name}|{scope_name}',
                    'reports/data',
                    {**window, 'report_id': report_id, 'granularity': 'day'},
                )


def snapshot(base_url: str) -> dict[str, Any]:
    session = requests.Session()
    session.headers['X-API-Key'] = api_key()
    result: dict[str, Any] = {}
    for index, (key, path, params) in enumerate(cases(session, base_url), start=1):
        result[key] = get(session, base_url, path, params)
        if index % 50 == 0:
            print(f'  ... {index} captured', file=sys.stderr)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL)
    parser.add_argument('--out', help='write the manifest here')
    parser.add_argument('--compare', help='compare against an existing manifest')
    args = parser.parse_args()

    current = snapshot(args.base_url)
    print(f'captured {len(current)} payloads')

    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            json.dump(current, handle, sort_keys=True)
        print(f'written to {args.out}')

    if args.compare:
        with open(args.compare, encoding='utf-8') as handle:
            baseline = json.load(handle)
        missing = sorted(set(baseline) - set(current))
        added = sorted(set(current) - set(baseline))
        differences: dict[str, list[str]] = {}
        for key in sorted(set(baseline) & set(current)):
            issues = compare(baseline[key], current[key])
            if issues:
                differences[key] = issues
        for label, keys in (('missing', missing), ('added', added)):
            if keys:
                print(f'{label}: {len(keys)}')
                for key in keys[:20]:
                    print(f'  {key}')
        if differences:
            print(f'CHANGED: {len(differences)}')
            for key, issues in list(differences.items())[:20]:
                print(f'  {key}: {len(issues)} field(s)')
                for issue in issues[:3]:
                    print(f'      {issue}')
        if differences or missing or added:
            return 1
        print(f'identical: all {len(current)} payloads match within {RELATIVE_TOLERANCE:g}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
