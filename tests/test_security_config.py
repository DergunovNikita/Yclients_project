import os
import subprocess
import sys

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
    assert 'SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM must be configured' in errors


def test_production_config_policy_accepts_safe_values():
    assert collect_production_config_errors(
        api_key='',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token',
        db_password='db-password-32-plus-non-default',
        portal_credentials_encryption_key='credential-key-32-plus-non-default',
        auth_cookie_secure=True,
        auth_cookie_samesite='lax',
        auth_console_email=False,
        smtp_host='smtp.example.com',
        smtp_user='mailer@example.com',
        smtp_password='smtp-password-32-plus-non-default',
        smtp_from='noreply@example.com',
    ) == []


def test_production_config_policy_rejects_placeholder_api_key_when_login_required():
    errors = collect_production_config_errors(
        api_key='change_me_strong_api_key',
        auth_require_login=True,
        auth_public_registration_enabled=False,
        auth_jwt_secret='a-long-production-jwt-secret-32-plus',
        sync_api_token='sync-token',
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
        sync_api_token='sync-token',
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


def _run_import_api(env_overrides: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update({
        'PYTHONPATH': os.getcwd(),
        'APP_ENV': 'production',
        'API_KEY': '',
        'AUTH_REQUIRE_LOGIN': 'true',
        'AUTH_PUBLIC_REGISTRATION_ENABLED': 'false',
        'AUTH_JWT_SECRET': 'production-jwt-secret-32-plus-value',
        'SYNC_API_TOKEN': 'production-sync-token',
        'DB_PASSWORD': 'production-db-password-32-plus-value',
        'PORTAL_CREDENTIALS_ENCRYPTION_KEY': 'production-credential-key-32-plus-value',
        'AUTH_COOKIE_SECURE': 'true',
        'AUTH_COOKIE_SAMESITE': 'lax',
        'AUTH_CONSOLE_EMAIL': 'false',
        'SMTP_HOST': 'smtp.example.com',
        'SMTP_USER': 'mailer@example.com',
        'SMTP_PASSWORD': 'production-smtp-password-32-plus-value',
        'SMTP_FROM': 'noreply@example.com',
    })
    env.update(env_overrides)
    return subprocess.run(
        [
            sys.executable,
            '-c',
            (
                'import api; '
                'print(api.app.docs_url, api.app.redoc_url, api.app.openapi_url, '
                'sorted(api.OPEN_PATHS))'
            ),
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


def _safe_production_config_env() -> dict[str, str]:
    return {
        'APP_ENV': 'production',
        'API_KEY': '',
        'AUTH_REQUIRE_LOGIN': 'true',
        'AUTH_JWT_SECRET': 'production-jwt-secret-32-plus-value',
        'SYNC_API_TOKEN': 'production-sync-token',
        'DB_PASSWORD': 'production-db-password-32-plus-value',
        'PORTAL_CREDENTIALS_ENCRYPTION_KEY': 'production-credential-key-32-plus-value',
        'AUTH_COOKIE_SECURE': 'true',
        'AUTH_COOKIE_SAMESITE': 'lax',
        'AUTH_CONSOLE_EMAIL': 'false',
        'SMTP_HOST': 'smtp.example.com',
        'SMTP_USER': 'mailer@example.com',
        'SMTP_PASSWORD': 'production-smtp-password-32-plus-value',
        'SMTP_FROM': 'noreply@example.com',
    }


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


def test_production_import_disables_docs_openapi_but_keeps_health_open():
    result = _run_import_api({})

    assert result.returncode == 0, result.stderr
    assert "None None None ['/health']" in result.stdout


def test_production_http_docs_routes_are_closed_but_health_is_open():
    result = _run_import_api({
        'PYTHON_CODE': '',
    })
    assert result.returncode == 0, result.stderr

    env = os.environ.copy()
    env.update({
        'PYTHONPATH': os.getcwd(),
        'APP_ENV': 'production',
        'API_KEY': '',
        'AUTH_REQUIRE_LOGIN': 'true',
        'AUTH_JWT_SECRET': 'production-jwt-secret-32-plus-value',
        'SYNC_API_TOKEN': 'production-sync-token',
        'DB_PASSWORD': 'production-db-password-32-plus-value',
        'PORTAL_CREDENTIALS_ENCRYPTION_KEY': 'production-credential-key-32-plus-value',
        'AUTH_COOKIE_SECURE': 'true',
        'AUTH_COOKIE_SAMESITE': 'lax',
        'AUTH_CONSOLE_EMAIL': 'false',
        'SMTP_HOST': 'smtp.example.com',
        'SMTP_USER': 'mailer@example.com',
        'SMTP_PASSWORD': 'production-smtp-password-32-plus-value',
        'SMTP_FROM': 'noreply@example.com',
    })
    http_result = subprocess.run(
        [
            sys.executable,
            '-c',
            (
                'from fastapi.testclient import TestClient; '
                'from api import app; '
                'client = TestClient(app); '
                'print(client.get("/health").status_code); '
                'print(client.get("/docs").status_code); '
                'print(client.get("/redoc").status_code); '
                'print(client.get("/openapi.json").status_code); '
                'print(client.get("/docs/oauth2-redirect").status_code)'
            ),
        ],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert http_result.returncode == 0, http_result.stderr
    assert http_result.stdout.strip().splitlines()[-5:] == ['200', '404', '404', '404', '404']


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
