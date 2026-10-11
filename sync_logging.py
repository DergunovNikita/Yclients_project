from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
import sys


class TeeWriter:
    def __init__(self, *streams):
        self._streams = streams

    def write(self, data):
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self):
        for stream in self._streams:
            stream.flush()


def build_log_path(log_dir: str, mode: str, trigger_type: str) -> str:
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    path = Path(log_dir) / f'sync_{timestamp}_{mode}_{trigger_type}.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def remove_run_logs(log_dir: str, log_paths) -> int:
    """Best-effort delete of run log files; returns how many were removed.

    The paths come from sync_runs rows, so only `sync_*.log` files that resolve inside `log_dir` are touched.
    Nothing here may fail the caller: housekeeping of a log must not undo or block the sweep that pruned its run.
    """
    removed = 0
    try:
        root = Path(log_dir).resolve()
    except OSError:
        return 0
    for raw in log_paths:
        if not raw:
            continue
        try:
            path = Path(raw).resolve()
            if path.parent != root or not (path.name.startswith('sync_') and path.suffix == '.log'):
                continue
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


@contextmanager
def stream_run_output(log_path: str):
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as log_file:
        stdout_tee = TeeWriter(sys.stdout, log_file)
        stderr_tee = TeeWriter(sys.stderr, log_file)
        with redirect_stdout(stdout_tee), redirect_stderr(stderr_tee):
            yield
