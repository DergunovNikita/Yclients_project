import importlib.util
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / 'scripts/sync-log-summary.py'


def load_module():
    spec = importlib.util.spec_from_file_location('sync_log_summary', SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def write_log(path: Path, content: str) -> Path:
    path.write_text(content.strip() + '\n')
    return path


def capture_stdout(func, *args, **kwargs):
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        result = func(*args, **kwargs)
    return result, stdout.getvalue()


def test_a_finished_run_without_failures_has_an_ok_verdict(tmp_path):
    module = load_module()
    path = write_log(
        tmp_path / 'sync_20250101.log',
        """
        started
        Филиал: Main
        [OK] Fetch clients 1.5 сек
        [WARN] Refresh views 1 мин 2 сек
        Синхронизация завершена!
        """,
    )

    result, _ = capture_stdout(module.summarize, path)
    summary = module._summarize_lines(path.read_text().splitlines())

    assert result == 0
    assert (summary.ok, summary.warn, summary.fail) == (1, 1, 0)
    assert summary.total_seconds == 63.5
    assert len(summary.branches) == 1


def test_main_last_reports_one_line_per_log_with_its_verdict(tmp_path, monkeypatch):
    module = load_module()
    write_log(
        tmp_path / 'sync_20250101.log',
        """
        run one
        [OK] First step 2 сек
        Синхронизация завершена!
        """,
    )
    write_log(
        tmp_path / 'sync_20250102.log',
        """
        run two
        [FAIL] Broken step 1 сек
        Синхронизация завершена!
        """,
    )
    monkeypatch.setattr(module, 'LOG_DIR', tmp_path)

    result, output = capture_stdout(module.main, ['--last', '2'])

    first, second = output.splitlines()
    assert result == 0
    assert first.startswith('sync_20250101.log') and first.endswith('ok')
    assert second.startswith('sync_20250102.log') and second.endswith('issues')


def test_failure_step_prints_errors_and_issues_exit(tmp_path):
    module = load_module()
    path = write_log(
        tmp_path / 'sync_failed.log',
        """
        started
        [OK] Good step 1 сек
        about to fail
        [ERR] Bad step 2 сек
        Traceback: boom
        Синхронизация завершена!
        """,
    )

    result, output = capture_stdout(module.summarize, path)

    assert result == 1
    assert 'steps: OK=1  WARN=0  FAIL=1  total=2\n' in output
    assert 'errors (2):\n' in output
    assert '  [ERR] Bad step 2 сек\n' in output
    assert '  Traceback: boom\n' in output
    assert output.endswith('\nexit: issues\n')


def test_unfinished_run_has_issues_verdict(tmp_path):
    module = load_module()
    path = write_log(
        tmp_path / 'sync_unfinished.log',
        """
        started
        [OK] Good step 1 сек
        still running
        """,
    )

    result, output = capture_stdout(module.summarize, path)

    assert result == 1
    assert output.endswith('\nexit: issues\n')


def test_summary_reads_what_the_pipeline_really_prints(tmp_path):
    module = load_module()
    pipeline = importlib.import_module('sync_pipeline')
    results = [
        {'name': 'Записи [Salon]', 'success': True, 'elapsed': 3723},
        {'name': 'Клиенты [Salon]', 'success': False, 'elapsed': 1.5},
    ]
    _, printed = capture_stdout(pipeline.print_sync_summary, results)
    path = write_log(tmp_path / 'sync_real.log', f'Филиал: Salon\n{printed}\n  Синхронизация завершена!')

    summary = module._summarize_lines(path.read_text().splitlines())

    assert summary.finished
    assert (summary.ok, summary.warn, summary.fail) == (1, 1, 0)
    assert summary.total_seconds == 3723 + 1.5
    assert summary.verdict == 'ok'


def test_a_step_that_raised_counts_as_a_failure(tmp_path):
    module = load_module()
    path = write_log(
        tmp_path / 'sync_aborted.log',
        """
        started
          ✗ Шаг прерван: Комментарии [Salon]
          [WARN] Комментарии [Salon]: 0.10 сек
        Синхронизация завершена!
        """,
    )

    result, output = capture_stdout(module.summarize, path)

    assert result == 1
    assert 'steps: OK=0  WARN=1  FAIL=1  total=1\n' in output
