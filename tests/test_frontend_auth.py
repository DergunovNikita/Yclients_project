from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_auth_no_longer_persists_bearer_tokens():
    auth_source = (ROOT / 'web/src/auth.js').read_text()
    login_source = (ROOT / 'web/src/login.js').read_text()

    assert 'portal_access_token' not in auth_source
    assert 'localStorage.setItem(TOKEN_KEY' not in auth_source
    assert 'headers.Authorization' not in auth_source
    assert 'payload.data.access_token' not in login_source
