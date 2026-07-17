"""Set a permanent reusable password for an existing portal user.

Unlike `create_portal_admin.py`, this script does not create users or modify
roles — it only replaces the password hash and stamps `password_changed_at`
so the account is treated as having a permanent reusable password.
"""
from __future__ import annotations

import argparse
import getpass
from datetime import datetime

from sqlalchemy import select

from auth_service import hash_password, normalize_email
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import init_database
from models import PortalUser


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Set a permanent password for a portal user')
    parser.add_argument('--email', required=True, help='Portal user email')
    parser.add_argument('--password', help='New password (prompted if omitted)')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    email = normalize_email(args.email)
    password = args.password or getpass.getpass('New password: ')
    if len(password) < 8:
        print('Password must be at least 8 characters')
        return 2

    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    db = database.get_db()
    try:
        user = db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none()
        if user is None:
            print(f'Portal user not found: {email}')
            return 1

        user.password_hash = hash_password(password)
        user.password_changed_at = datetime.utcnow()
        db.commit()
        print(f'Password updated for user id={user.id} email={email}')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
