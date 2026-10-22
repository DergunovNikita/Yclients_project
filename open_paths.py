"""The one list of routes that answer without authentication.

Exact paths, not prefixes: a prefix such as `/auth/logout` silently opened `/auth/logout-all`
and anything added under it later. A new public route must be added here on purpose.
"""

from config import IS_PRODUCTION

_PUBLIC_AUTH_ACTIONS = (
    'register',
    'login',
    'demo-login',
    'refresh',
    'logout',
    'verify-email',
    'forgot-password',
    'reset-password',
)

# The auth router is mounted twice, under `/auth` and `/dashboard/auth`.
OPEN_PATHS: set[str] = {'/health', '/health/ready'} | {
    f'{prefix}/{action}' for prefix in ('/auth', '/dashboard/auth') for action in _PUBLIC_AUTH_ACTIONS
}
if not IS_PRODUCTION:
    OPEN_PATHS.update({'/openapi.json', '/docs', '/redoc'})
