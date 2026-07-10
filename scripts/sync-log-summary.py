#!/usr/bin/env python3
"""Quick summary of sync_*.log files produced by sync_orchestrator + TeeWriter.

Usage:
  scripts/sync-log-summary.py                    # summarize the latest log
  scripts/sync-log-summary.py <path>             # summarize a specific log
  scripts/sync-log-summary.py --last 5           # one-line diff of last N runs
  scripts/sync-log-summary.py --errors-only      # dump errors with 5-line context
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

LOG_DIR = Path(os.environ.get('SYNC_LOG_DIR', 'logs'))

STEP_RE = re.compile(r'^\s*\[(OK|WARN|ERR|ERROR|FAIL)\]\s+(.*?)\s+(\d[\d.: ]*(?:мин|сек|s|m|ms)[\d\s мин сек]*)\s*$')
SECTION_RE = re.compile(r'^── (?P<name>.+?) ──\s*$')
BRANCH_RE = re.compile(r'^Филиал:\s+(?P<name>.+?)\s*$')
ERROR_MARKERS = ('✗', 'Ошибк', 'Traceback', 'ERROR', 'ERR ', '[ERR]', '[FAIL]')
DONE_MARKERS = ('Sync run finished', '✓ Успешно', 'done')


@dataclass
class StepTiming:
    status: str
    label: str
    seconds: float


@dataclass
class LogSummary:
    steps: list[tuple[int, StepTiming]]
    branches: set[str]
    errors: list[tuple[int, list[str]]]
    ok: int
    warn: int
    fail: int
    total_seconds: float
    finished: bool

    @property
    def verdict(self) -> str:
        return 'ok' if self.finished and self.fail == 0 else 'issues'

    @property
    def exit_code(self) -> int:
        return 0 if self.verdict == 'ok' else 1


def _parse_seconds(raw: str) -> float:
    raw = raw.strip().lower().replace(',', '.')
    total = 0.0
    minutes_match = re.search(r'(\d+(?:\.\d+)?)\s*мин', raw)
    seconds_match = re.search(r'(\d+(?:\.\d+)?)\s*сек', raw)
    if minutes_match:
        total += float(minutes_match.group(1)) * 60
    if seconds_match:
        total += float(seconds_match.group(1))
    if total == 0.0:
        try:
            total = float(re.sub(r'[^\d.]', '', raw) or 0)
        except ValueError:
            total = 0.0
    return total


def _fmt_seconds(seconds: float) -> str:
    if seconds < 60:
        return f'{seconds:.1f}s'
    minutes, rest = divmod(seconds, 60)
    return f'{int(minutes)}m{rest:04.1f}s'


def _latest_log(log_dir: Path) -> Path | None:
    candidates = sorted(log_dir.glob('sync_*.log'))
    return candidates[-1] if candidates else None


def _iter_steps(lines: list[str]):
    seen: set[tuple[str, str, float]] = set()
    for idx, line in enumerate(lines):
        match = STEP_RE.match(line)
        if match:
            status, label, raw_time = match.groups()
            label = label.rstrip(':').strip()
            seconds = _parse_seconds(raw_time)
            key = (status.upper(), label, seconds)
            if key in seen:
                continue
            seen.add(key)
            yield idx, StepTiming(status=status.upper(), label=label, seconds=seconds)


def _collect_errors(lines: list[str]):
    errors = []
    for idx, line in enumerate(lines):
        if any(marker in line for marker in ERROR_MARKERS):
            start = max(0, idx - 2)
            end = min(len(lines), idx + 4)
            errors.append((idx, lines[start:end]))
    return errors


def _summarize_lines(lines: list[str]) -> LogSummary:
    steps = list(_iter_steps(lines))
    status_counts = Counter(step.status for _, step in steps)
    fail = sum(status_counts.get(k, 0) for k in ('ERR', 'ERROR', 'FAIL'))
    return LogSummary(
        steps=steps,
        branches={line.strip() for line in lines if BRANCH_RE.match(line.strip())},
        errors=_collect_errors(lines),
        ok=status_counts.get('OK', 0),
        warn=status_counts.get('WARN', 0),
        fail=fail,
        total_seconds=sum(step.seconds for _, step in steps),
        finished=any(marker in line for line in lines for marker in DONE_MARKERS),
    )


def summarize(path: Path, *, errors_only: bool = False) -> int:
    lines = path.read_text(errors='replace').splitlines()
    if not lines:
        print(f'[empty] {path}')
        return 1

    summary = _summarize_lines(lines)

    if errors_only:
        if not summary.errors:
            print(f'{path.name}: no error markers found')
            return 0
        print(f'{path.name}: {len(summary.errors)} error line(s)')
        for _, chunk in summary.errors:
            print('  ' + '\n  '.join(chunk))
            print('  ---')
        return 0

    top = sorted(summary.steps, key=lambda item: item[1].seconds, reverse=True)[:10]

    header = lines[0] if lines else ''
    tail = next((line for line in reversed(lines) if line.strip()), '')

    print(f'== {path.name} ==')
    print(f'header: {header.strip()}')
    print(f'tail:   {tail.strip()}')
    print(f'branches seen: {len(summary.branches)}')
    print(f'steps: OK={summary.ok}  WARN={summary.warn}  FAIL={summary.fail}  total={len(summary.steps)}')
    print(f'total step-time: {_fmt_seconds(summary.total_seconds)}')
    print()
    print('top 10 slowest steps:')
    for _, step in top:
        print(f'  {_fmt_seconds(step.seconds):>10}  [{step.status}] {step.label}')

    if summary.errors:
        print()
        print(f'errors ({len(summary.errors)}):')
        for _, chunk in summary.errors[:5]:
            for line in chunk:
                print(f'  {line}')
            print('  ---')
        if len(summary.errors) > 5:
            print(f'  ... {len(summary.errors) - 5} more (use --errors-only)')

    print()
    print(f'exit: {summary.verdict}')
    return summary.exit_code


def brief(path: Path) -> str:
    try:
        lines = path.read_text(errors='replace').splitlines()
    except OSError:
        return f'{path.name}: unreadable'
    summary = _summarize_lines(lines)
    total = _fmt_seconds(summary.total_seconds)
    return (
        f'{path.name}  ok={summary.ok:>3} warn={summary.warn:>2} fail={summary.fail:>2}  '
        f'total={total:>8}  {summary.verdict}'
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', nargs='?', help='Path to a sync log; defaults to the latest in $SYNC_LOG_DIR')
    parser.add_argument('--last', type=int, default=0, help='One-line summary of the last N logs')
    parser.add_argument('--errors-only', action='store_true', help='Only dump error lines with context')
    args = parser.parse_args(argv)

    if args.last:
        candidates = sorted(LOG_DIR.glob('sync_*.log'))[-args.last:]
        if not candidates:
            print(f'no logs found in {LOG_DIR}', file=sys.stderr)
            return 1
        for path in candidates:
            print(brief(path))
        return 0

    if args.path:
        path = Path(args.path)
    else:
        path = _latest_log(LOG_DIR) or Path()
    if not path.is_file():
        print(f'log not found: {path}', file=sys.stderr)
        return 1

    return summarize(path, errors_only=args.errors_only)


if __name__ == '__main__':
    raise SystemExit(main())
