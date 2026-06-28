"""
Конфигурационный файл с настройками.
Значения берутся из переменных окружения (.env файл) с fallback-значениями.
"""
import os
from dotenv import load_dotenv

load_dotenv()


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
SYNC_DAYS = _get_int('SYNC_DAYS', 365)
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
AUTH_EMAIL_VERIFY_REQUIRED = _get_bool('AUTH_EMAIL_VERIFY_REQUIRED', True)
AUTH_EMAIL_RESEND_COOLDOWN_SECONDS = _get_int('AUTH_EMAIL_RESEND_COOLDOWN_SECONDS', 60)
AUTH_COOKIE_SECURE = _get_bool('AUTH_COOKIE_SECURE', False)
AUTH_COOKIE_SAMESITE = os.getenv('AUTH_COOKIE_SAMESITE', 'lax').strip().lower()
AUTH_COOKIE_DOMAIN = os.getenv('AUTH_COOKIE_DOMAIN', '').strip()
AUTH_CSRF_COOKIE_NAME = os.getenv('AUTH_CSRF_COOKIE_NAME', 'portal_csrf').strip()
AUTH_CSRF_HEADER_NAME = os.getenv('AUTH_CSRF_HEADER_NAME', 'X-CSRF-Token').strip()
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
