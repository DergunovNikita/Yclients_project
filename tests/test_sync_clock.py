"""Stored timestamps are naive UTC from clock.utcnow(); a bare datetime.now() would store the host's local time."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
# Names a log file after the local wall clock and stores nothing.
ALLOWED = {'sync_logging.py'}


def test_sync_modules_never_read_the_host_local_clock():
    offenders = [
        path.name
        for path in sorted(ROOT.glob('sync_*.py'))
        if path.name not in ALLOWED and re.search(r'\bdatetime\.now\(\)', path.read_text(encoding='utf-8'))
    ]

    assert offenders == [], f'use clock.utcnow() instead of datetime.now() in {offenders}'
