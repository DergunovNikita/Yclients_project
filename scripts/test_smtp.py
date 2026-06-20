"""Send a test auth email. Usage: python scripts/test_smtp.py recipient@example.com"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from auth_service import _email_delivery_mode, send_auth_email
from config import APP_PUBLIC_URL, smtp_is_configured


def main() -> int:
    parser = argparse.ArgumentParser(description='Test SMTP / console email delivery')
    parser.add_argument('to', help='Recipient email address')
    args = parser.parse_args()

    mode = _email_delivery_mode()
    print(f'Delivery mode: {mode}')
    print(f'SMTP configured: {smtp_is_configured()}')
    print(f'APP_PUBLIC_URL: {APP_PUBLIC_URL}')

    if mode == 'console':
        print('Hint: set SMTP_HOST, SMTP_USER, SMTP_PASSWORD and AUTH_CONSOLE_EMAIL=false in .env')

    try:
        send_auth_email(
            args.to,
            'Тест — YClients Portal',
            (
                'Это тестовое письмо.\n\n'
                'Если вы видите его в почте, SMTP настроен правильно.\n'
            ),
        )
    except Exception as exc:
        print(f'FAILED: {exc}')
        return 1

    if mode == 'smtp':
        print(f'OK: letter sent to {args.to}')
    else:
        print('OK: link printed above (console mode)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
