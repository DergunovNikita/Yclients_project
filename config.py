"""
Конфигурационный файл с настройками.
Значения берутся из переменных окружения (.env файл) с fallback-значениями.
"""
import os
from datetime import date
from urllib.parse import urlsplit

from dotenv import load_dotenv

load_dotenv()

PRODUCTION_ENV_NAMES = {'prod', 'production'}
APP_ENV = os.getenv('APP_ENV', 'local').strip().lower() or 'local'
IS_PRODUCTION = APP_ENV in PRODUCTION_ENV_NAMES
PLACEHOLDER_SECRET_VALUES = {
    'change_me_local_jwt_secret',
    'change_me_sync_api_token',
    'change_me_strong_api_key',
    'change_me_long_random_secret',
    'changeme',
    'replace_with_openssl_rand_hex_32',
    'your_app_password',
    'your_login@yandex.ru',
}
PLACEHOLDER_SECRET_PREFIXES = ('change_me', 'replace_with_', 'your_')


def _get_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {'1', 'true', 'yes', 'on'}


def _get_date(name: str, default: date) -> date:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        return default


def _is_blank(value: str | None) -> bool:
    return value is None or not value.strip()


def _is_placeholder(value: str | None) -> bool:
    if _is_blank(value):
        return True
    normalized = value.strip().lower()
    return (
        normalized in PLACEHOLDER_SECRET_VALUES
        or any(normalized.startswith(prefix) for prefix in PLACEHOLDER_SECRET_PREFIXES)
    )


def _cors_origins_list(value: str | None) -> list[str]:
    return [origin.strip() for origin in (value or '').split(',') if origin.strip()]


def _is_exact_cors_origin(value: str) -> bool:
    if value in {'*', 'null'} or '*' in value:
        return False
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme in {'http', 'https'}
        and bool(parsed.netloc)
        and bool(parsed.hostname)
        and parsed.path == ''
        and parsed.query == ''
        and parsed.fragment == ''
        and parsed.username is None
        and parsed.password is None
    )

# ============================================================================
# Настройки YClients API
# ============================================================================
PARTNER_TOKEN = os.getenv('PARTNER_TOKEN', '')
LOGIN = os.getenv('YCLIENTS_LOGIN', '')
PASSWORD = os.getenv('YCLIENTS_PASSWORD', '')
YCLIENTS_REQUEST_DELAY = _get_float('YCLIENTS_REQUEST_DELAY', 0.25)
YCLIENTS_TIMEOUT = _get_float('YCLIENTS_TIMEOUT', 30.0)
YCLIENTS_RETRY_TOTAL = _get_int('YCLIENTS_RETRY_TOTAL', 3)
YCLIENTS_RETRY_BACKOFF = _get_float('YCLIENTS_RETRY_BACKOFF', 1.0)
# Cap (seconds) on a 429 Retry-After the client will honor, so a hostile/large header cannot
# stall a sync step for minutes. Each request still gives up after retry_total attempts.
YCLIENTS_RETRY_AFTER_MAX = _get_float('YCLIENTS_RETRY_AFTER_MAX', 60.0)

# ============================================================================
# Настройки PostgreSQL
# ============================================================================
DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = int(os.getenv('DB_PORT', '5432'))
DB_NAME = os.getenv('DB_NAME', 'yclients_db')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

# ============================================================================
# Параметры синхронизации
# ============================================================================
SYNC_DAYS = _get_int('SYNC_DAYS', 0)
SYNC_HISTORY_START_DATE = _get_date('SYNC_HISTORY_START_DATE', date(2000, 1, 1))
SCHEDULE_DAYS = _get_int('SCHEDULE_DAYS', 60)
ANALYTICS_DAYS = _get_int('ANALYTICS_DAYS', 30)
DB_BATCH_SIZE = _get_int('DB_BATCH_SIZE', 1000)
SYNC_INCREMENTAL = _get_bool('SYNC_INCREMENTAL', True)
SYNC_LOOKBACK_DAYS = _get_int('SYNC_LOOKBACK_DAYS', 2)

# ============================================================================
# Служебные параметры синхронизации
# ============================================================================
SYNC_LOG_DIR = os.getenv('SYNC_LOG_DIR', 'logs')
SYNC_LOCK_ID = _get_int('SYNC_LOCK_ID', 826451)
SYNC_API_TOKEN = os.getenv('SYNC_API_TOKEN', '')
SYNC_WORKER_POLL_INTERVAL = _get_float('SYNC_WORKER_POLL_INTERVAL', 5.0)
SYNC_AUTO_ENQUEUE_ENABLED = _get_bool('SYNC_AUTO_ENQUEUE_ENABLED', True)
SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES = _get_int('SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)
# A 'running' job older than this is treated as orphaned (worker died mid-sync) and reaped.
SYNC_STALE_JOB_MINUTES = _get_int('SYNC_STALE_JOB_MINUTES', 120)
SERVICES_LABEL_SYNC_INTERVAL_DAYS = _get_int('SERVICES_LABEL_SYNC_INTERVAL_DAYS', 7)

# ============================================================================
# API runtime
# ============================================================================
API_HOST = os.getenv('API_HOST', '0.0.0.0')
API_PORT = _get_int('API_PORT', 8000)
API_KEY = os.getenv('API_KEY', '')

# Comma-separated origins for dashboard SPA (e.g. Vercel preview). Empty = no CORS middleware.
DASHBOARD_CORS_ORIGINS = os.getenv('DASHBOARD_CORS_ORIGINS', '')
DASHBOARD_CORS_ORIGIN_REGEX = os.getenv('DASHBOARD_CORS_ORIGIN_REGEX', '')
DASHBOARD_CORS_ALLOW_METHODS = ('GET', 'POST', 'PATCH', 'DELETE')
DASHBOARD_CORS_ALLOW_HEADERS = (
    'Content-Type',
    'X-CSRF-Token',
    'X-Portal-Account-Id',
    'X-API-Key',
)

# Published Google Sheets CSV URL with branch plan values for /dashboard/widget/plan_fact.
PLAN_SHEET_CSV_URL = os.getenv('PLAN_SHEET_CSV_URL', '')
# Service-account fallback for the plan sheet when PLAN_SHEET_CSV_URL is empty or private.
PLAN_SHEET_ID = os.getenv('PLAN_SHEET_ID', '')
PLAN_SHEET_NAME = os.getenv('PLAN_SHEET_NAME', 'plan')
# Optional published CSV URL for the services labels sheet. If empty, the importer
# tries to read sheet=services from the same spreadsheet as PLAN_SHEET_CSV_URL.
SERVICES_SHEET_CSV_URL = os.getenv('SERVICES_SHEET_CSV_URL', '')
GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv('GOOGLE_SERVICE_ACCOUNT_FILE', '')
GOOGLE_SERVICE_ACCOUNT_JSON_B64 = os.getenv('GOOGLE_SERVICE_ACCOUNT_JSON_B64', '')
SERVICES_SHEET_ID = os.getenv('SERVICES_SHEET_ID', '')
SERVICES_SHEET_NAME = os.getenv('SERVICES_SHEET_NAME', 'services')

# ============================================================================
# Уведомления
# ============================================================================
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

# ============================================================================
# Portal auth (personal cabinets)
# ============================================================================
AUTH_JWT_SECRET = os.getenv('AUTH_JWT_SECRET', 'change_me_local_jwt_secret')
AUTH_JWT_EXPIRE_MINUTES = _get_int('AUTH_JWT_EXPIRE_MINUTES', 30)
AUTH_REFRESH_TOKEN_EXPIRE_DAYS = _get_int('AUTH_REFRESH_TOKEN_EXPIRE_DAYS', 30)
AUTH_REQUIRE_LOGIN = _get_bool('AUTH_REQUIRE_LOGIN', not bool(API_KEY))
AUTH_PUBLIC_REGISTRATION_ENABLED = _get_bool(
    'AUTH_PUBLIC_REGISTRATION_ENABLED',
    APP_ENV in {'local', 'test'},
)
AUTH_EMAIL_VERIFY_REQUIRED = _get_bool('AUTH_EMAIL_VERIFY_REQUIRED', True)
AUTH_EMAIL_RESEND_COOLDOWN_SECONDS = _get_int('AUTH_EMAIL_RESEND_COOLDOWN_SECONDS', 60)
AUTH_COOKIE_SECURE = _get_bool('AUTH_COOKIE_SECURE', False)
AUTH_COOKIE_SAMESITE = os.getenv('AUTH_COOKIE_SAMESITE', 'lax').strip().lower()
AUTH_COOKIE_DOMAIN = os.getenv('AUTH_COOKIE_DOMAIN', '').strip()
AUTH_CSRF_COOKIE_NAME = os.getenv('AUTH_CSRF_COOKIE_NAME', 'portal_csrf').strip()
AUTH_CSRF_HEADER_NAME = os.getenv('AUTH_CSRF_HEADER_NAME', 'X-CSRF-Token').strip()
AUTH_RATE_LIMIT_MAX_REQUESTS = _get_int('AUTH_RATE_LIMIT_MAX_REQUESTS', 10)
AUTH_RATE_LIMIT_IP_MAX_REQUESTS = _get_int(
    'AUTH_RATE_LIMIT_IP_MAX_REQUESTS',
    max(AUTH_RATE_LIMIT_MAX_REQUESTS * 5, AUTH_RATE_LIMIT_MAX_REQUESTS),
)
AUTH_RATE_LIMIT_WINDOW_SECONDS = _get_float('AUTH_RATE_LIMIT_WINDOW_SECONDS', 60.0)
APP_PUBLIC_URL = os.getenv('APP_PUBLIC_URL', 'http://127.0.0.1:5173')
SMTP_HOST = os.getenv('SMTP_HOST', '').strip()
SMTP_PORT = _get_int('SMTP_PORT', 587)
SMTP_USER = os.getenv('SMTP_USER', '').strip()
SMTP_PASSWORD = os.getenv('SMTP_PASSWORD', '')
SMTP_FROM = os.getenv('SMTP_FROM', '').strip()
SMTP_USE_TLS = _get_bool('SMTP_USE_TLS', True)
SMTP_USE_SSL = _get_bool('SMTP_USE_SSL', SMTP_PORT == 465)


def smtp_is_configured() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD)


_console_email_env = os.getenv('AUTH_CONSOLE_EMAIL')
if _console_email_env is None:
    AUTH_CONSOLE_EMAIL = not smtp_is_configured()
else:
    AUTH_CONSOLE_EMAIL = _get_bool('AUTH_CONSOLE_EMAIL', True)

PORTAL_CREDENTIALS_ENCRYPTION_KEY = os.getenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', '').strip()
PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD = os.getenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY_OLD', '').strip()


def collect_production_config_errors(
    *,
    api_key: str | None = API_KEY,
    auth_require_login: bool = AUTH_REQUIRE_LOGIN,
    auth_public_registration_enabled: bool = AUTH_PUBLIC_REGISTRATION_ENABLED,
    auth_jwt_secret: str | None = AUTH_JWT_SECRET,
    sync_api_token: str | None = SYNC_API_TOKEN,
    db_password: str | None = DB_PASSWORD,
    portal_credentials_encryption_key: str | None = PORTAL_CREDENTIALS_ENCRYPTION_KEY,
    auth_cookie_secure: bool = AUTH_COOKIE_SECURE,
    auth_cookie_samesite: str | None = AUTH_COOKIE_SAMESITE,
    auth_console_email: bool = AUTH_CONSOLE_EMAIL,
    smtp_host: str | None = SMTP_HOST,
    smtp_user: str | None = SMTP_USER,
    smtp_password: str | None = SMTP_PASSWORD,
    smtp_from: str | None = SMTP_FROM,
    dashboard_cors_origins: str | None = DASHBOARD_CORS_ORIGINS,
    dashboard_cors_origin_regex: str | None = DASHBOARD_CORS_ORIGIN_REGEX,
) -> list[str]:
    """Return production-only startup configuration errors.

    Keep this side-effect free so tests can validate the policy without
    reloading module globals or depending on the host process environment.
    """
    errors: list[str] = []
    if _is_placeholder(auth_jwt_secret) or len(auth_jwt_secret.strip()) < 32:
        errors.append('AUTH_JWT_SECRET must be set to a strong non-default value')
    if _is_placeholder(sync_api_token):
        errors.append('SYNC_API_TOKEN must be set to a non-default value')
    if _is_placeholder(db_password):
        errors.append('DB_PASSWORD must be set to a non-default value')
    if (
        _is_placeholder(portal_credentials_encryption_key)
        or len(portal_credentials_encryption_key.strip()) < 32
    ):
        errors.append('PORTAL_CREDENTIALS_ENCRYPTION_KEY must be set to a strong non-default value')
    if not _is_blank(api_key) and (_is_placeholder(api_key) or len(api_key.strip()) < 32):
        errors.append('API_KEY must be unset or set to a strong non-default value')
    if not auth_require_login and _is_blank(api_key):
        errors.append('AUTH_REQUIRE_LOGIN must be true unless API_KEY is set')
    if auth_public_registration_enabled:
        errors.append('AUTH_PUBLIC_REGISTRATION_ENABLED must be false')
    if not auth_cookie_secure:
        errors.append('AUTH_COOKIE_SECURE must be true')
    if (auth_cookie_samesite or '').strip().lower() not in {'lax', 'strict', 'none'}:
        errors.append('AUTH_COOKIE_SAMESITE must be one of: lax, strict, none')
    if auth_console_email:
        errors.append('AUTH_CONSOLE_EMAIL must be false')
    if any(_is_placeholder(value) for value in (smtp_host, smtp_user, smtp_password, smtp_from)):
        errors.append('SMTP_HOST, SMTP_USER, SMTP_PASSWORD and SMTP_FROM must be configured')
    if not _is_blank(dashboard_cors_origin_regex):
        errors.append('DASHBOARD_CORS_ORIGIN_REGEX must be empty in production')
    if any(not _is_exact_cors_origin(origin) for origin in _cors_origins_list(dashboard_cors_origins)):
        errors.append(
            'DASHBOARD_CORS_ORIGINS must contain exact http(s) origins without wildcards, '
            'paths, query strings or fragments'
        )
    return errors


def validate_production_config() -> None:
    if not IS_PRODUCTION:
        return
    errors = collect_production_config_errors()
    if errors:
        details = '; '.join(errors)
        raise RuntimeError(f'Unsafe production configuration: {details}')


validate_production_config()
