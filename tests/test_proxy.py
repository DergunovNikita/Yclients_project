from pathlib import Path


def test_vercel_proxy_does_not_inject_backend_api_key_for_anonymous_requests():
    root = Path(__file__).resolve().parents[1]
    for relative_path in ('api/_proxy.js', 'web/api/_proxy.js'):
        source = (root / relative_path).read_text()
        assert 'VM_API_KEY' not in source
        assert 'API_KEY' not in source
        assert "headers['X-API-Key']" not in source
