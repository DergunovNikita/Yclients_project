"""Create or update a local super_admin portal user."""
from __future__ import annotations

import argparse
from datetime import datetime

from sqlalchemy import delete, select

from auth_service import hash_password, normalize_email
from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER
from database import init_database
from models import Company, PortalUser, PortalUserBranch


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Create local super_admin portal user')
    parser.add_argument('--email', default='admin@local.dev')
    parser.add_argument('--password', default='Admin12345!')
    parser.add_argument('--full-name', default='Local Super Admin')
    parser.add_argument('--assign-all-branches', action='store_true', help='Assign all companies as branches')
    return parser.parse_args()


def _set_user_branches(db, user_id: int, company_ids: list[int]) -> None:
    db.execute(delete(PortalUserBranch).where(PortalUserBranch.user_id == user_id))
    for company_id in sorted(set(company_ids)):
        db.add(PortalUserBranch(user_id=user_id, company_id=company_id))
    db.commit()


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
                role='super_admin',
                is_active=True,
                email_verified_at=datetime.utcnow(),
                created_at=datetime.utcnow(),
            )
            db.add(user)
            db.commit()
            db.refresh(user)
            print(f'Created super_admin user id={user.id} email={email}')
        else:
            user.password_hash = hash_password(args.password)
            user.role = 'super_admin'
            user.is_active = True
            user.email_verified_at = user.email_verified_at or datetime.utcnow()
            user.full_name = args.full_name
            db.commit()
            print(f'Updated super_admin user id={user.id} email={email}')

        if args.assign_all_branches:
            company_ids = [row[0] for row in db.execute(select(Company.id)).all()]
            _set_user_branches(db, user.id, company_ids)
            print(f'Assigned branches: {company_ids}')

        print('Login at http://127.0.0.1:5173/login.html')
        print(f'Email: {email}')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
