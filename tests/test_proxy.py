from pathlib import Path
import shutil
import subprocess

import pytest


def test_vercel_proxy_does_not_inject_backend_api_key_for_anonymous_requests():
    root = Path(__file__).resolve().parents[1]
    for relative_path in ('api/_proxy.js', 'web/api/_proxy.js'):
        source = (root / relative_path).read_text()
        assert 'VM_API_KEY' not in source
        assert 'API_KEY' not in source
        assert "headers['X-API-Key']" not in source


def test_root_proxy_forwards_real_client_ip_headers():
    root = Path(__file__).resolve().parents[1]
    node = root / '.venv/node-v24.15.0-darwin-arm64/bin/node'
    node_cmd = str(node) if node.exists() else shutil.which('node')
    if not node_cmd:
        pytest.skip('node is required to execute api/_proxy.js')
    script = """
        import assert from 'node:assert/strict';
        import { forwardedHeaders } from './api/_proxy.js';

        const forwarded = forwardedHeaders({
          headers: {
            cookie: 'session=ok',
            'x-forwarded-for': '203.0.113.10',
            'x-real-ip': '203.0.113.11',
          },
          socket: { remoteAddress: '198.51.100.42' },
        });

        assert.equal(forwarded.cookie, 'session=ok');
        assert.equal(forwarded['x-forwarded-for'], '198.51.100.42');
        assert.equal(forwarded['x-real-ip'], '198.51.100.42');
        assert.notEqual(forwarded['x-forwarded-for'], '203.0.113.10');
        assert.notEqual(forwarded['x-real-ip'], '203.0.113.11');
    """
    result = subprocess.run(
        [node_cmd, '--input-type=module', '-e', script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_production_workflows_fail_closed():
    root = Path(__file__).resolve().parents[1]
    deploy = (root / '.github/workflows/deploy-vm.yml').read_text()
    security = (root / '.github/workflows/security.yml').read_text()

    assert 'refusing to report a successful production deploy' in deploy
    assert 'continue-on-error: true' not in security
    assert '--soft-fail' not in security


def test_root_and_web_proxy_trees_are_byte_identical():
    # AGENTS.md: a route present in only one of these trees 404s in deploy. A directory
    # walk (rather than a hardcoded filename list) also catches a file added to one side only.
    root = Path(__file__).resolve().parents[1]
    root_api = root / 'api'
    web_api = root / 'web' / 'api'

    def tracked_files(directory: Path) -> set[Path]:
        return {
            path.relative_to(directory)
            for path in directory.rglob('*')
            if path.is_file() and path.name != '.DS_Store'
        }

    root_files = tracked_files(root_api)
    web_files = tracked_files(web_api)
    assert root_files == web_files, (
        f'api/ and web/api/ file sets differ: {root_files.symmetric_difference(web_files)}'
    )

    for relative_path in sorted(root_files):
        assert (root_api / relative_path).read_bytes() == (web_api / relative_path).read_bytes(), (
            f'{relative_path} differs between api/ and web/api/'
        )
