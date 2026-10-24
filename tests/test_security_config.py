import os
import re
import subprocess
import sys

import pytest

from config import collect_production_config_errors


def test_production_config_policy_rejects_unsafe_defaults():
    errors = collect_production_config_errors(
        api_key='',
        auth_require_login=False,
        auth_public_registration_enabled=True,
        auth_jwt_secret='change_me_local_jwt_secret',
        sync_api_token=' ',
        db_password='',
        portal_credentials_encryption_key='',
        auth_cookie_secure=False,
        auth_cookie_samesite='invalid',
        auth_console_email=True,
        smtp_host='smtp.yandex.ru',
        smtp_user='your_login@yandex.ru',
        smtp_password='your_app_password',
        smtp_from='your_login@yandex.ru',
        dashboard_cors_origins='*, https://dashboard.example.com/',
        dashboard_cors_origin_regex='https://.*',
    )

    assert 'AUTH_JWT_SECRET must be set to a strong non-default value' in errors
    assert 'SYNC_API_TOKEN must be set to a non-default value' in errors
    assert 'DB_PASSWORD must be set to a non-default value' in errors
    assert 'PORTAL_CREDENTIALS_ENCRYPTION_KEY must be set to a strong non-default value' in errors
    assert 'API_KEY must be unset or set to a strong non-default value' not in errors
    assert 'AUTH_REQUIRE_LOGIN must be true unless API_KEY is set' in errors
    assert 'AUTH_PUBLIC_REGISTRATION_ENABLED must be false' in errors
    assert 'AUTH_COOKIE_SECURE must be true' in errors
    assert 'AUTH_COOKIE_SAMESITE must be one of: lax, strict, none' in errors
    assert 'AUTH_CONSOLE_EMAIL must be false' in errors
    assert 'DASHBOARD_CORS_ORIGIN_REGEX must be empty in production' in errors
    assert (
        'DASHBOARD_CORS_ORIGINS must contain exact http(s) origins without wildcards, '
        'paths, query strings or fragments'
    ) in errors


def test_production_config_policy_accepts_safe_values():
    assert collect_production_config_errors(
        api_key='',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='smtp.example.com',
        smtp_user='mailer@example.com',
        smtp_password='smtp-password-32-plus-non-default',
        smtp_from='noreply@example.com',
        dashboard_cors_origins='https://dashboard.example.com,https://admin.example.com',
        dashboard_cors_origin_regex='',
        app_public_url='https://portal.example.com',
    ) == []


def test_production_config_policy_requires_smtp_without_console_fallback():
    errors = collect_production_config_errors(
        api_key='',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='',
        smtp_user='',
        smtp_password='',
        smtp_from='',
        dashboard_cors_origins='https://dashboard.example.com',
        dashboard_cors_origin_regex='',
        app_public_url='https://portal.example.com',
    )

    assert errors == ['SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM must be configured']


def test_production_config_policy_accepts_explicit_console_email_fallback():
    assert collect_production_config_errors(
        api_key='',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=True,
        auth_allow_console_email_in_production=True,
        smtp_host='',
        smtp_user='',
        smtp_password='',
        smtp_from='',
        dashboard_cors_origins='https://dashboard.example.com',
        dashboard_cors_origin_regex='',
    ) == []


_CORS_ORIGIN_ERROR = (
    'DASHBOARD_CORS_ORIGINS must contain exact http(s) origins without wildcards, '
    'paths, query strings or fragments'
)


@pytest.mark.parametrize(
    ('origins', 'rejected'),
    [
        ('https://dashboard.example.com,https://admin.example.com', False),
        ('null', True),
        ('https://*.example.com', True),
        ('https://dashboard.example.com/path', True),
        ('https://dashboard.example.com/', True),
        ('https://dashboard.example.com?x=1', True),
        ('https://dashboard.example.com#frag', True),
        ('https://user:pass@dashboard.example.com', True),
        ('https://dashboard.example.com:bad', True),
        ('https://ok.example.com,https://dashboard.example.com/path', True),
    ],
)
def test_production_config_policy_requires_exact_cors_origins(origins, rejected):
    errors = collect_production_config_errors(
        api_key='',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='smtp.example.com',
        smtp_user='mailer@example.com',
        smtp_password='smtp-password-32-plus-non-default',
        smtp_from='noreply@example.com',
        dashboard_cors_origins=origins,
        dashboard_cors_origin_regex='',
        app_public_url='https://portal.example.com',
    )

    assert errors == ([_CORS_ORIGIN_ERROR] if rejected else [])


def test_production_config_policy_rejects_placeholder_api_key_when_login_required():
    errors = collect_production_config_errors(
        api_key='change_me_strong_api_key',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='smtp.example.com',
        smtp_user='mailer@example.com',
        smtp_password='smtp-password-32-plus-non-default',
        smtp_from='noreply@example.com',
    )

    assert 'API_KEY must be unset or set to a strong non-default value' in errors


def test_production_config_policy_rejects_placeholder_api_key_when_login_disabled():
    errors = collect_production_config_errors(
        api_key='change_me_strong_api_key',
        auth_require_login=False,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='smtp.example.com',
        smtp_user='mailer@example.com',
        smtp_password='smtp-password-32-plus-non-default',
        smtp_from='noreply@example.com',
    )

    assert 'API_KEY must be unset or set to a strong non-default value' in errors
    assert 'AUTH_REQUIRE_LOGIN must be true unless API_KEY is set' not in errors


@pytest.mark.parametrize('url', ['', 'http://127.0.0.1:5173', 'https://localhost', 'http://portal.example.com'])
def test_production_config_policy_requires_public_https_app_url_for_smtp(url):
    errors = collect_production_config_errors(
        api_key='',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='smtp.example.com',
        smtp_user='mailer@example.com',
        smtp_password='smtp-password-32-plus-non-default',
        smtp_from='noreply@example.com',
        dashboard_cors_origins='https://dashboard.example.com',
        dashboard_cors_origin_regex='',
        app_public_url=url,
    )

    assert errors == ['APP_PUBLIC_URL must be a public https URL when e-mail is sent through SMTP']


@pytest.mark.parametrize(
    ('secret', 'rejected'),
    [('', False), ('p' * 32, False), ('p' * 31, True), ('change_me_proxy_secret', True)],
)
def test_production_config_policy_requires_a_strong_proxy_shared_secret_when_set(secret, rejected):
    errors = collect_production_config_errors(
        api_key='',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token-32-plus-non-default-value',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='smtp.example.com',
        smtp_user='mailer@example.com',
        smtp_password='smtp-password-32-plus-non-default',
        smtp_from='noreply@example.com',
        dashboard_cors_origins='https://dashboard.example.com',
        dashboard_cors_origin_regex='',
        app_public_url='https://portal.example.com',
        proxy_shared_secret=secret,
    )

    expected = ['PROXY_SHARED_SECRET must be unset or set to a strong non-default value'] if rejected else []
    assert errors == expected


def _run_import_api(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        'PYTHONPATH': os.getcwd(),
        'APP_ENV': 'production',
        'API_KEY': '',
        'AUTH_REQUIRE_LOGIN': 'true',
        'AUTH_PUBLIC_REGISTRATION_ENABLED': 'false',
        'AUTH_JWT_SECRET': 'production-jwt-secret-32-plus-value',
        'SYNC_API_TOKEN': 'production-sync-token-32-plus-value',
        'DB_PASSWORD': 'production-db-password-32-plus-value',
        'PORTAL_CREDENTIALS_ENCRYPTION_KEY': 'production-credential-key-32-plus-value',
        'AUTH_COOKIE_SECURE': 'true',
        'AUTH_COOKIE_SAMESITE': 'lax',
        'AUTH_CONSOLE_EMAIL': 'false',
        'SMTP_HOST': 'smtp.example.com',
        'SMTP_USER': 'mailer@example.com',
        'SMTP_PASSWORD': 'production-smtp-password-32-plus-value',
        'SMTP_FROM': 'noreply@example.com',
        'DASHBOARD_CORS_ORIGINS': 'https://dashboard.example.com',
        'DASHBOARD_CORS_ORIGIN_REGEX': '',
        'APP_PUBLIC_URL': 'https://portal.example.com',
    })
    env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            '-c',
            'import api',
        ],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_import_config(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        'PYTHONPATH': os.getcwd(),
    })
    env.pop('AUTH_PUBLIC_REGISTRATION_ENABLED', None)
    env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            '-c',
            'import config; print(config.AUTH_PUBLIC_REGISTRATION_ENABLED)',
        ],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path) as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            name, value = line.split('=', 1)
            values[name] = value
    return values


def _safe_production_config_env() -> dict[str, str]:
    return {
        'APP_ENV': 'production',
        'API_KEY': '',
        'AUTH_REQUIRE_LOGIN': 'true',
        'AUTH_JWT_SECRET': 'production-jwt-secret-32-plus-value',
        'SYNC_API_TOKEN': 'production-sync-token-32-plus-value',
        'DB_PASSWORD': 'production-db-password-32-plus-value',
        'PORTAL_CREDENTIALS_ENCRYPTION_KEY': 'production-credential-key-32-plus-value',
        'AUTH_COOKIE_SECURE': 'true',
        'AUTH_COOKIE_SAMESITE': 'lax',
        'AUTH_CONSOLE_EMAIL': 'false',
        'SMTP_HOST': 'smtp.example.com',
        'SMTP_USER': 'mailer@example.com',
        'SMTP_PASSWORD': 'production-smtp-password-32-plus-value',
        'SMTP_FROM': 'noreply@example.com',
        'DASHBOARD_CORS_ORIGINS': 'https://dashboard.example.com',
        'DASHBOARD_CORS_ORIGIN_REGEX': '',
        'APP_PUBLIC_URL': 'https://portal.example.com',
    }


def _run_production_code(code: str, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({'PYTHONPATH': os.getcwd()})
    env.update(_safe_production_config_env())
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [sys.executable, '-c', code],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_invalid_log_level_is_refused_at_import():
    result = _run_import_config({'APP_ENV': 'local', 'LOG_LEVEL': 'LOUD'})

    assert result.returncode != 0
    assert 'LOG_LEVEL must be one of' in result.stderr


def test_garbage_db_port_is_refused_not_replaced_by_the_default():
    # 5432 is a different PostgreSQL on a developer machine; a silent fallback would connect there.
    result = _run_import_config({'APP_ENV': 'local', 'DB_PORT': 'five'})

    assert result.returncode != 0
    assert "DB_PORT must be an integer, got 'five'" in result.stderr

    result = _run_import_config({'APP_ENV': 'local', 'DB_PORT': '5433'})
    assert result.returncode == 0, result.stderr


def test_env_example_is_importable_as_local_config():
    result = _run_import_config(_read_env_file('.env.example'))

    assert result.returncode == 0, result.stderr


def test_production_import_fails_with_unsafe_defaults():
    result = _run_import_api({
        'AUTH_JWT_SECRET': 'change_me_local_jwt_secret',
        'SYNC_API_TOKEN': '',
        'DB_PASSWORD': '',
        'PORTAL_CREDENTIALS_ENCRYPTION_KEY': '',
        'AUTH_COOKIE_SECURE': 'false',
    })

    assert result.returncode != 0
    assert 'Unsafe production configuration' in result.stderr
    assert 'AUTH_JWT_SECRET' in result.stderr
    assert 'SYNC_API_TOKEN' in result.stderr


def test_production_import_fails_when_public_registration_enabled():
    result = _run_import_api({'AUTH_PUBLIC_REGISTRATION_ENABLED': 'true'})

    assert result.returncode != 0
    assert 'AUTH_PUBLIC_REGISTRATION_ENABLED' in result.stderr


def test_production_import_fails_with_unsafe_cors_regex():
    result = _run_import_api({'DASHBOARD_CORS_ORIGIN_REGEX': 'https://.*'})

    assert result.returncode != 0
    assert 'DASHBOARD_CORS_ORIGIN_REGEX must be empty in production' in result.stderr


def test_production_http_docs_routes_are_closed_but_health_is_open():
    # Readiness and liveness stay open on purpose: the deploy loop reads them before any credential exists.
    result = _run_production_code(
        """
from fastapi.testclient import TestClient
from api import app

client = TestClient(app)
for path in ('/health', '/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect'):
    print(path, client.get(path).status_code)
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-5:] == [
        '/health 200', '/docs 404', '/redoc 404', '/openapi.json 404', '/docs/oauth2-redirect 404',
    ]


def test_production_cors_allows_exact_origin_and_denies_unknown_origin():
    result = _run_production_code(
        """
from fastapi.testclient import TestClient
from api import app

client = TestClient(app, base_url='https://api.example.com')
allowed = client.options(
    '/health',
    headers={
        'Origin': 'https://dashboard.example.com',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'content-type,x-csrf-token,x-portal-account-id,x-api-key',
    },
)
assert allowed.status_code == 200, allowed.text
assert allowed.headers.get('access-control-allow-origin') == 'https://dashboard.example.com'
assert allowed.headers.get('access-control-allow-credentials') == 'true'
allowed_methods = allowed.headers.get('access-control-allow-methods', '')
for method in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE'):
    assert method in allowed_methods, allowed_methods
allowed_headers = allowed.headers.get('access-control-allow-headers', '').lower()
for header in ('content-type', 'x-csrf-token', 'x-portal-account-id', 'x-api-key'):
    assert header in allowed_headers, allowed_headers

unknown_preflight = client.options(
    '/health',
    headers={
        'Origin': 'https://evil.example.com',
        'Access-Control-Request-Method': 'POST',
    },
)
assert unknown_preflight.headers.get('access-control-allow-origin') is None
unknown_simple = client.get('/health', headers={'Origin': 'https://evil.example.com'})
assert unknown_simple.headers.get('access-control-allow-origin') is None

blocked_method = client.options(
    '/health',
    headers={
        'Origin': 'https://dashboard.example.com',
        'Access-Control-Request-Method': 'TRACE',
    },
)
assert blocked_method.status_code == 400
assert blocked_method.headers.get('access-control-allow-origin') == 'https://dashboard.example.com'

blocked_header = client.options(
    '/health',
    headers={
        'Origin': 'https://dashboard.example.com',
        'Access-Control-Request-Method': 'POST',
        'Access-Control-Request-Headers': 'authorization',
    },
)
assert blocked_header.status_code == 400
assert blocked_header.headers.get('access-control-allow-origin') == 'https://dashboard.example.com'
print('ok')
"""
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == 'ok'


def test_production_security_headers_are_present_on_https_responses():
    result = _run_production_code(
        """
from fastapi.testclient import TestClient
from api import OPEN_PATHS, app

@app.get('/boom')
async def boom():
    raise RuntimeError('boom')
OPEN_PATHS.add('/boom')

https_client = TestClient(app, base_url='https://api.example.com')
response = https_client.get('/health')
assert response.headers.get('x-content-type-options') == 'nosniff'
assert response.headers.get('x-frame-options') == 'DENY'
assert response.headers.get('referrer-policy') == 'strict-origin-when-cross-origin'
csp = response.headers.get('content-security-policy', '')
assert "frame-ancestors 'none'" in csp
assert "base-uri 'self'" in csp
assert "object-src 'none'" in csp
assert response.headers.get('strict-transport-security') == 'max-age=31536000; includeSubDomains'

http_client = TestClient(app, base_url='http://api.example.com')
http_response = http_client.get('/health')
assert http_response.headers.get('strict-transport-security') is None
forwarded_https_response = http_client.get('/health', headers={'X-Forwarded-Proto': 'https'})
assert forwarded_https_response.headers.get('strict-transport-security') == 'max-age=31536000; includeSubDomains'

error_client = TestClient(app, base_url='https://api.example.com', raise_server_exceptions=False)
error_response = error_client.get('/boom')
assert error_response.status_code == 500
assert error_response.headers.get('x-content-type-options') == 'nosniff'
assert error_response.headers.get('x-frame-options') == 'DENY'
assert error_response.headers.get('referrer-policy') == 'strict-origin-when-cross-origin'
assert "frame-ancestors 'none'" in error_response.headers.get('content-security-policy', '')
assert error_response.headers.get('strict-transport-security') == 'max-age=31536000; includeSubDomains'
print('ok')
""",
        {'APP_PUBLIC_URL': 'https://api.example.com'},
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == 'ok'


def test_static_headers_snippet_defines_security_headers():
    expected = {
        'Content-Security-Policy': "frame-ancestors 'none'; base-uri 'self'; object-src 'none'; script-src 'self'",
        'Referrer-Policy': 'strict-origin-when-cross-origin',
        'X-Content-Type-Options': 'nosniff',
        'X-Frame-Options': 'DENY',
    }

    with open('deploy/nginx/static-headers.conf', encoding='utf-8') as handle:
        merged = dict(re.findall(r'^add_header (\S+) "([^"]*)" always;$', handle.read(), re.M))

    assert merged == expected
    # HSTS belongs to the site config, next to the certificate it depends on.
    assert 'Strict-Transport-Security' not in merged


def test_local_and_test_import_allow_local_defaults():
    for app_env in ('local', 'test'):
        result = _run_import_api({
            'APP_ENV': app_env,
            'AUTH_JWT_SECRET': 'change_me_local_jwt_secret',
            'SYNC_API_TOKEN': '',
            'DB_PASSWORD': '',
            'PORTAL_CREDENTIALS_ENCRYPTION_KEY': '',
            'AUTH_COOKIE_SECURE': 'false',
            'AUTH_CONSOLE_EMAIL': 'true',
        })
        assert result.returncode == 0, result.stderr


def test_unrecognised_app_env_fails_closed_like_production():
    """A forgotten, blank, or misspelled APP_ENV must be treated as production, not skip it.

    The check used to only match the exact strings 'prod'/'production'; anything else —
    including an empty APP_ENV or a plausible typo such as 'staging'/'preprod' — silently
    skipped validate_production_config() and would boot with the placeholder secrets below.
    """
    for app_env in ('', 'staging', 'preprod', 'Production_typo'):
        result = _run_import_api({
            'APP_ENV': app_env,
            'AUTH_JWT_SECRET': 'change_me_local_jwt_secret',
            'SYNC_API_TOKEN': '',
            'DB_PASSWORD': '',
            'PORTAL_CREDENTIALS_ENCRYPTION_KEY': '',
            'AUTH_COOKIE_SECURE': 'false',
        })

        assert result.returncode != 0, f'APP_ENV={app_env!r} must be treated as production'
        assert 'Unsafe production configuration' in result.stderr
        assert 'AUTH_JWT_SECRET' in result.stderr


def test_public_registration_defaults_enabled_only_for_local_and_test():
    for app_env in ('local', 'test'):
        result = _run_import_config({'APP_ENV': app_env})
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'True'

    production_env = _safe_production_config_env()
    production_env['AUTH_PUBLIC_REGISTRATION_ENABLED'] = 'false'
    production_result = _run_import_config(production_env)
    assert production_result.returncode == 0, production_result.stderr
    assert production_result.stdout.strip() == 'False'
