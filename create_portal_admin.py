"""Create a platform_admin portal user (also the production bootstrap run by the auto-deploy script).

An existing user is left untouched: the deploy runs this on every push, and rewriting the password,
role, tenant or active flag there would undo a password the admin changed and re-enable a deactivated
account. `--reset-password` is the explicit, manual way to do that.
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime

from sqlalchemy import select

from auth_service import PASSWORD_MAX_BYTES, hash_password, normalize_email
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import init_database
from models import PortalUser


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Create a platform_admin portal user (existing users are skipped)')
    parser.add_argument('--email', required=True)
    parser.add_argument(
        '--password',
        help='Password; defaults to the PORTAL_ADMIN_PASSWORD environment variable (keeps it out of argv)',
    )
    parser.add_argument('--full-name', default='Local Super Admin')
    parser.add_argument(
        '--reset-password',
        action='store_true',
        help='For an existing user: overwrite password, role, tenant and active flag (manual use only)',
    )
    args = parser.parse_args()
    args.password = args.password or os.environ.get('PORTAL_ADMIN_PASSWORD', '')
    if not args.password:
        parser.error('a password is required: pass --password or set PORTAL_ADMIN_PASSWORD')
    return args


def main() -> int:
    args = parse_args()
    email = normalize_email(args.email)

    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    db = database.get_db()
    try:
        user = db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none()
        # Only where a hash is made: an existing user is skipped, and that must stay true of a deploy
        # whose environment password is long.
        if (user is None or args.reset_password) and len(args.password.encode('utf-8')) > PASSWORD_MAX_BYTES:
            print(f'Password must not exceed {PASSWORD_MAX_BYTES} bytes in UTF-8')
            return 2
        if user is None:
            user = PortalUser(
                email=email,
                password_hash=hash_password(args.password),
                full_name=args.full_name,
                role='platform_admin',
                is_active=True,
                email_verified_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f'Created platform_admin user id={user.id} email={email}')
        elif not args.reset_password:
            print(f'platform_admin user id={user.id} email={email} exists, skipped')
        else:
            user.password_hash = hash_password(args.password)
            user.role = 'platform_admin'
            user.portal_account_id = None
            user.is_active = True
            user.email_verified_at = user.email_verified_at or datetime.utcnow()
            user.full_name = args.full_name
            db.commit()
            print(f'Reset platform_admin user id={user.id} email={email}')

        print(f'Email: {email}')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
