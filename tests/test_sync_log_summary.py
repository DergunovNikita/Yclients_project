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


def test_summarize_prints_normal_summary_and_ok_exit(tmp_path):
    module = load_module()
    path = write_log(
        tmp_path / 'sync_20250101.log',
        """
        started
        Филиал: Main
        [OK] Fetch clients 1.5 сек
        [WARN] Refresh views 1 мин 2 сек
        Sync run finished
        """,
    )

    result, output = capture_stdout(module.summarize, path)

    assert result == 0
    assert output == (
        '== sync_20250101.log ==\n'
        'header: started\n'
        'tail:   Sync run finished\n'
        'branches seen: 1\n'
        'steps: OK=1  WARN=1  FAIL=0  total=2\n'
        'total step-time: 1m03.5s\n'
        '\n'
        'top 10 slowest steps:\n'
        '     1m02.0s  [WARN] Refresh views\n'
        '        1.5s  [OK] Fetch clients\n'
        '\n'
        'exit: ok\n'
    )


def test_main_last_prints_brief_summaries(tmp_path, monkeypatch):
    module = load_module()
    write_log(
        tmp_path / 'sync_20250101.log',
        """
        run one
        [OK] First step 2 сек
        done
        """,
    )
    write_log(
        tmp_path / 'sync_20250102.log',
        """
        run two
        [FAIL] Broken step 1 сек
        done
        """,
    )
    monkeypatch.setattr(module, 'LOG_DIR', tmp_path)

    result, output = capture_stdout(module.main, ['--last', '2'])

    assert result == 0
    assert output == (
        'sync_20250101.log  ok=  1 warn= 0 fail= 0  total=    2.0s  ok\n'
        'sync_20250102.log  ok=  0 warn= 0 fail= 1  total=    1.0s  issues\n'
    )


def test_errors_only_without_errors_prints_no_markers(tmp_path):
    module = load_module()
    path = write_log(
        tmp_path / 'sync_clean.log',
        """
        clean run
        [OK] Step 100ms
        done
        """,
    )

    result, output = capture_stdout(module.summarize, path, errors_only=True)

    assert result == 0
    assert output == 'sync_clean.log: no error markers found\n'


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
        done
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
