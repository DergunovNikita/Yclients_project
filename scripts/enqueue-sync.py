#!/usr/bin/env python3
"""Enqueue a sync job via POST /sync/trigger without hand-crafting curl.

Reads API_KEY, SYNC_API_TOKEN, API_HOST and API_PORT from `.env` (or the
current environment; env wins). Use --dry-run to print an equivalent curl.

Examples:
  scripts/enqueue-sync.py --tenant 1 --mode incremental
  scripts/enqueue-sync.py --global --mode full --initiator cli
  scripts/enqueue-sync.py --status
  scripts/enqueue-sync.py --tenant 1 --companies 148406,85779 --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(errors='replace').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, _, value = line.partition('=')
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def _env(name: str, dotenv: dict[str, str], default: str = '') -> str:
    return os.environ.get(name) or dotenv.get(name, default)


def _build_url(host: str, path: str) -> str:
    if '://' not in host:
        host = f'http://{host}'
    return host.rstrip('/') + path


def _parse_companies(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(item) for item in raw.replace(' ', '').split(',') if item]


def _request(method: str, url: str, headers: dict[str, str], body: dict | None) -> tuple[int, dict | str]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode(errors='replace')
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode(errors='replace')
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload
    except urllib.error.URLError as exc:
        return 0, f'connection failed: {exc.reason}'


def _print_curl(url: str, method: str, headers: dict[str, str], body: dict | None) -> None:
    parts = [f'curl -X {method}']
    for key, value in headers.items():
        parts.append(f'-H "{key}: {value}"')
    if body is not None:
        parts.append(f"-d '{json.dumps(body, ensure_ascii=False)}'")
    parts.append(url)
    print(' \\\n  '.join(parts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['incremental', 'full'], default='incremental')
    parser.add_argument('--tenant', type=int, help='portal_account_id')
    parser.add_argument('--global', dest='global_sync', action='store_true', help='Sync across all tenants')
    parser.add_argument('--credential', type=int, help='YClients credential id')
    parser.add_argument('--companies', help='Comma-separated company ids')
    parser.add_argument('--initiator', default='cli')
    parser.add_argument('--host', help='Override API host (e.g. 127.0.0.1:8000)')
    parser.add_argument('--env-file', default='.env', help='Path to .env (default: .env)')
    parser.add_argument('--dry-run', action='store_true', help='Print equivalent curl instead of calling')
    parser.add_argument('--status', action='store_true', help='GET /sync/status instead of triggering')
    args = parser.parse_args(argv)

    dotenv = _load_env_file(Path(args.env_file))
    api_key = _env('API_KEY', dotenv)
    sync_token = _env('SYNC_API_TOKEN', dotenv)
    if not sync_token:
        print('SYNC_API_TOKEN is required (env or .env)', file=sys.stderr)
        return 2

    host = args.host or f"{_env('API_HOST', dotenv, '127.0.0.1')}:{_env('API_PORT', dotenv, '8000')}"

    headers: dict[str, str] = {
        'Content-Type': 'application/json',
        'X-Sync-Token': sync_token,
    }
    if api_key:
        headers['X-API-Key'] = api_key

    if args.status:
        url = _build_url(host, '/sync/status')
        if args.dry_run:
            _print_curl(url, 'GET', headers, None)
            return 0
        code, payload = _request('GET', url, headers, None)
        print(f'HTTP {code}')
        print(json.dumps(payload, ensure_ascii=False, indent=2) if isinstance(payload, dict) else payload)
        return 0 if code == 200 else 1

    if not args.tenant and not args.global_sync:
        print('Specify --tenant <id> or --global', file=sys.stderr)
        return 2

    body: dict = {
        'mode': args.mode,
        'initiator': args.initiator,
        'global_sync': bool(args.global_sync),
    }
    if args.tenant:
        body['portal_account_id'] = args.tenant
    if args.credential is not None:
        body['credential_id'] = args.credential
    companies = _parse_companies(args.companies)
    if companies:
        body['company_ids'] = companies

    url = _build_url(host, '/sync/trigger')

    if args.dry_run:
        _print_curl(url, 'POST', headers, body)
        return 0

    code, payload = _request('POST', url, headers, body)
    print(f'HTTP {code}')
    print(json.dumps(payload, ensure_ascii=False, indent=2) if isinstance(payload, dict) else payload)
    return 0 if code == 200 else 1


if __name__ == '__main__':
    raise SystemExit(main())
