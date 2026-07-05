"""One-shot helper: move PARTNER_TOKEN/LOGIN/PASSWORD from .env into system.yclients_credentials.

Run inside the app container:
    docker compose run --rm api python scripts/migrate_env_credentials.py

The script is idempotent: re-running it skips accounts that already have an
identically-titled credential row, and it never deletes existing rows.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select  # noqa: E402

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER, LOGIN, PARTNER_TOKEN, PASSWORD  # noqa: E402
from database import init_database  # noqa: E402
from models import Company, PortalAccount, PortalBranch, YClientsCredential, YClientsCredentialCompany  # noqa: E402
from yclients_credentials import new_credential  # noqa: E402

DEFAULT_TITLE = 'Environment credentials (migrated)'


def main() -> int:
    if not (PARTNER_TOKEN.strip() and LOGIN.strip() and PASSWORD.strip()):
        print('! No env credentials present — nothing to migrate.')
        return 0
    if not os.getenv('PORTAL_CREDENTIALS_ENCRYPTION_KEY', '').strip():
        print('! PORTAL_CREDENTIALS_ENCRYPTION_KEY is not set. Aborting.')
        return 2

    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    db = database.get_db()

    try:
        account = db.execute(select(PortalAccount).order_by(PortalAccount.id.asc())).scalars().first()
        if account is None:
            print('! No PortalAccount in DB. Run main migrations first.')
            return 2

        existing = db.execute(
            select(YClientsCredential).where(
                YClientsCredential.portal_account_id == account.id,
                YClientsCredential.title == DEFAULT_TITLE,
            )
        ).scalar_one_or_none()

        if existing is not None:
            credential = existing
            print(f'· Reusing existing credential id={credential.id}')
        else:
            credential = new_credential(
                portal_account_id=account.id,
                title=DEFAULT_TITLE,
                partner_token=PARTNER_TOKEN,
                login=LOGIN,
                password=PASSWORD,
            )
            db.add(credential)
            db.flush()
            print(f'+ Created credential id={credential.id} for account "{account.label}"')

        branch_company_ids = [
            row[0]
            for row in db.execute(
                select(PortalBranch.company_id).where(PortalBranch.portal_account_id == account.id)
            ).all()
        ]
        if not branch_company_ids:
            branch_company_ids = [row[0] for row in db.execute(select(Company.id)).all()]
            for company_id in branch_company_ids:
                db.add(PortalBranch(portal_account_id=account.id, company_id=company_id))
            print(f'+ Created {len(branch_company_ids)} portal_branches')

        already_mapped = {
            row[0]
            for row in db.execute(
                select(YClientsCredentialCompany.company_id).where(
                    YClientsCredentialCompany.credential_id == credential.id
                )
            ).all()
        }
        added = 0
        for company_id in branch_company_ids:
            if company_id in already_mapped:
                continue
            db.add(YClientsCredentialCompany(credential_id=credential.id, company_id=int(company_id)))
            added += 1

        db.commit()
        print(f'✓ Linked credential to {added} companies (already had {len(already_mapped)})')
        return 0
    finally:
        db.close()


if __name__ == '__main__':
    raise SystemExit(main())
