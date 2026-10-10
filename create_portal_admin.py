"""Create or update a platform_admin portal user (also the production bootstrap run by the auto-deploy script)."""
from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import select

from auth_service import hash_password, normalize_email
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import init_database
from models import PortalUser


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Create or update a platform_admin portal user')
    parser.add_argument('--email', required=True)
    parser.add_argument('--password', required=True)
    parser.add_argument('--full-name', default='Local Super Admin')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    email = normalize_email(args.email)

    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    if not database.test_connection():
        return 1

    db = database.get_db()
    try:
        user = db.execute(select(PortalUser).where(PortalUser.email == email)).scalar_one_or_none()
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
        else:
            user.password_hash = hash_password(args.password)
            user.role = 'platform_admin'
            user.portal_account_id = None
            user.is_active = True
            user.email_verified_at = user.email_verified_at or datetime.utcnow()
            user.full_name = args.full_name
            db.commit()
            print(f'Updated platform_admin user id={user.id} email={email}')

        print(f'Email: {email}')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
