from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SCAN_EXTENSIONS = {'.html', '.js', '.mjs'}
FRONTEND_SCAN_EXCLUDED_DIRS = {'dist', 'node_modules', 'tests'}


def test_frontend_auth_no_longer_persists_bearer_tokens():
    frontend_sources = {
        path.relative_to(ROOT).as_posix(): path.read_text()
        for path in (ROOT / 'web').rglob('*')
        if path.is_file()
        and path.suffix in FRONTEND_SCAN_EXTENSIONS
        and not (FRONTEND_SCAN_EXCLUDED_DIRS & set(path.relative_to(ROOT / 'web').parts))
    }
    combined_source = '\n'.join(frontend_sources.values())
    auth_source = frontend_sources['web/src/auth.js']
    login_source = frontend_sources['web/src/login.js']

    assert 'portal_access_token' not in auth_source
    assert 'localStorage.setItem(TOKEN_KEY' not in auth_source
    assert 'headers.Authorization' not in auth_source
    assert 'payload.data.access_token' not in login_source
    assert 'portal_access_token' not in combined_source
    assert 'Authorization' not in combined_source
    assert 'Bearer' not in combined_source
