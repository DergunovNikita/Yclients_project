from argparse import Namespace
from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import scripts.seed_demo as seed_demo
from models import (
    Account,
    Appointment,
    Base,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    Good,
    GoodCategory,
    GoodTransaction,
    Group,
    PortalAccount,
    PortalBranch,
    PortalUser,
    Service,
    ServiceCatalog,
    ServiceCategory,
    ServiceCategoryCatalog,
    Staff,
    StaffPosition,
    StaffSchedule,
    Storage,
    Transaction,
)
from sync_pipeline import sync_services


SEED_TABLES = [
    Group.__table__,
    Company.__table__,
    ServiceCategory.__table__,
    ServiceCategoryCatalog.__table__,
    Service.__table__,
    ServiceCatalog.__table__,
    StaffPosition.__table__,
    Staff.__table__,
    Client.__table__,
    Account.__table__,
    Storage.__table__,
    GoodCategory.__table__,
    Good.__table__,
    Appointment.__table__,
    Transaction.__table__,
    FinancialTransaction.__table__,
    GoodTransaction.__table__,
    Comment.__table__,
    StaffSchedule.__table__,
    PortalAccount.__table__,
    PortalBranch.__table__,
    PortalUser.__table__,
]


class FakeServicesAPI:
    def __init__(self, services):
        self._services = services

    def get_services(self, company_id, staff_id=None, category_id=None):
        return self._services


@contextmanager
def sqlite_session_with_system():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=SEED_TABLES)
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        yield db
    finally:
        db.close()
        engine.dispose()


def demo_args(**overrides):
    values = {
        'companies': 1,
        'days': 1,
        'seed': 42,
        'clients_per_company': 2,
        'staff_per_company': 1,
        'goods_per_company': 1,
        'appointments_per_day_min': 1,
        'appointments_per_day_max': 1,
        'skip_refresh_views': True,
    }
    values.update(overrides)
    return Namespace(**values)


def provision_demo(db, args=None):
    args = args or demo_args()
    account = seed_demo.get_or_create_demo_account(db)
    db.flush()
    company_ids = seed_demo._demo_company_ids(db, account.id)
    if not company_ids:
        company_ids = seed_demo.generate_demo_data(db, args, account.id)
    seed_demo.ensure_branches(db, account.id, company_ids)
    user = seed_demo.ensure_demo_user(db, account.id)
    db.commit()
    return account, user, company_ids


def seed_real_tenant_with_overlapping_external_ids(db):
    real_account = PortalAccount(id=1, label='Real tenant', is_demo=False, created_at=datetime.utcnow())
    group = Group(id=1, portal_account_id=1, external_id=1, title='Real group')
    company = Company(
        id=1,
        portal_account_id=1,
        external_id=1,
        source_type='yclients',
        title='Real branch',
        group_id=1,
    )
    client = Client(
        id=1,
        external_id=1,
        source_type='yclients',
        name='Real client',
        company_id=1,
    )
    staff = Staff(
        id=1,
        external_id=1,
        source_type='yclients',
        name='Real staff',
        company_id=1,
    )
    appointment = Appointment(
        id=1,
        external_id=1,
        source_type='yclients',
        company_id=1,
        staff_id=1,
        client_id=1,
        date=date.today(),
    )
    db.add_all([real_account, group, company, client, staff, appointment])
    db.commit()


def test_seed_demo_creates_embedded_tenant_without_touching_real_data():
    with sqlite_session_with_system() as db:
        seed_real_tenant_with_overlapping_external_ids(db)

        account, user, company_ids = provision_demo(db)

        assert account.is_demo is True
        assert user.email == seed_demo.DEMO_EMAIL
        assert user.is_demo is True
        assert user.portal_account_id == account.id
        assert user.onboarding_completed_at is not None
        assert len(company_ids) == 1

        real_company = db.get(Company, 1)
        assert real_company.portal_account_id == 1
        assert real_company.source_type == 'yclients'

        demo_company = db.get(Company, company_ids[0])
        assert demo_company.portal_account_id == account.id
        assert demo_company.source_type == seed_demo.DEMO_SOURCE_TYPE
        assert demo_company.external_id == 1

        branch = db.execute(select(PortalBranch).where(PortalBranch.company_id == demo_company.id)).scalar_one()
        assert branch.portal_account_id == account.id

        clients = db.execute(select(Client).where(Client.external_id == 1).order_by(Client.company_id)).scalars().all()
        assert {(client.company_id, client.source_type) for client in clients} == {
            (1, 'yclients'),
            (demo_company.id, seed_demo.DEMO_SOURCE_TYPE),
        }

        staff = db.execute(select(Staff).where(Staff.external_id == 1).order_by(Staff.company_id)).scalars().all()
        assert {(row.company_id, row.source_type) for row in staff} == {
            (1, 'yclients'),
            (demo_company.id, seed_demo.DEMO_SOURCE_TYPE),
        }

        appointments = (
            db.execute(select(Appointment).where(Appointment.external_id == 1).order_by(Appointment.company_id))
            .scalars()
            .all()
        )
        assert {(row.company_id, row.source_type) for row in appointments} == {
            (1, 'yclients'),
            (demo_company.id, seed_demo.DEMO_SOURCE_TYPE),
        }


def test_seed_demo_is_idempotent_for_account_user_branches_and_data():
    with sqlite_session_with_system() as db:
        first_account, first_user, first_company_ids = provision_demo(db)
        counts_before = {
            'accounts': db.scalar(select(func.count(PortalAccount.id))),
            'users': db.scalar(select(func.count(PortalUser.id))),
            'branches': db.scalar(select(func.count(PortalBranch.id))),
            'companies': db.scalar(select(func.count(Company.id))),
            'clients': db.scalar(select(func.count(Client.id))),
            'appointments': db.scalar(select(func.count(Appointment.id))),
        }

        second_account, second_user, second_company_ids = provision_demo(db)
        counts_after = {
            'accounts': db.scalar(select(func.count(PortalAccount.id))),
            'users': db.scalar(select(func.count(PortalUser.id))),
            'branches': db.scalar(select(func.count(PortalBranch.id))),
            'companies': db.scalar(select(func.count(Company.id))),
            'clients': db.scalar(select(func.count(Client.id))),
            'appointments': db.scalar(select(func.count(Appointment.id))),
        }

        assert second_account.id == first_account.id
        assert second_user.id == first_user.id
        assert second_company_ids == first_company_ids
        assert counts_after == counts_before


def test_seed_demo_child_primary_keys_do_not_scale_from_large_company_ids():
    with sqlite_session_with_system() as db:
        db.add(PortalAccount(id=1, label='Real tenant', is_demo=False, created_at=datetime.utcnow()))
        db.add(Group(id=1, portal_account_id=1, external_id=1, title='Real group'))
        db.add(
            Company(
                id=30_000,
                portal_account_id=1,
                external_id=1,
                source_type='yclients',
                title='Large real branch',
                group_id=1,
            )
        )
        db.commit()

        _account, _user, company_ids = provision_demo(db, demo_args(goods_per_company=2))

        demo_company_id = company_ids[0]
        assert demo_company_id < 0
        assert db.scalar(select(func.max(Client.id)).where(Client.company_id == demo_company_id)) < 0
        assert db.scalar(select(func.max(Staff.id)).where(Staff.company_id == demo_company_id)) < 0
        assert db.scalar(select(func.max(Service.id)).where(Service.company_id == demo_company_id)) < 0
        assert db.scalar(select(func.max(Good.good_id)).where(Good.company_id == demo_company_id)) < 0
        assert db.scalar(select(func.max(Appointment.id)).where(Appointment.company_id == demo_company_id)) < 0
        assert db.scalar(select(func.max(Transaction.id)).where(Transaction.company_id == demo_company_id)) < 0
        assert db.scalar(select(func.max(StaffSchedule.id)).where(StaffSchedule.company_id == demo_company_id)) < 0


def test_seed_demo_catalog_ids_do_not_collide_with_later_real_sync_ids():
    with sqlite_session_with_system() as db:
        _account, _user, company_ids = provision_demo(db)
        demo_company_id = company_ids[0]
        demo_services_before = {
            service.id: service.title
            for service in db.execute(select(Service).where(Service.company_id == demo_company_id)).scalars()
        }

        assert demo_services_before
        assert max(demo_services_before) < 0

        db.add(PortalAccount(id=100, label='Real tenant', is_demo=False, created_at=datetime.utcnow()))
        db.add(Group(id=100, portal_account_id=100, external_id=100, title='Real group'))
        db.add(
            Company(
                id=100,
                portal_account_id=100,
                external_id=1,
                source_type='yclients',
                title='Real branch',
                group_id=100,
            )
        )
        db.commit()

        assert sync_services(FakeServicesAPI([{'id': 1, 'title': 'Real Cut', 'price_min': 10.0}]), db, '100') is True

        real_service = db.get(Service, 1)
        assert real_service is not None
        assert real_service.company_id == 100
        assert real_service.title == 'Real Cut'

        demo_services_after = {
            service.id: service.title
            for service in db.execute(select(Service).where(Service.company_id == demo_company_id)).scalars()
        }
        assert demo_services_after == demo_services_before


def test_seed_demo_refuses_to_reuse_non_demo_user_email():
    with sqlite_session_with_system() as db:
        db.add(PortalAccount(id=1, label='Real tenant', is_demo=False, created_at=datetime.utcnow()))
        db.add(
            PortalUser(
                id=1,
                email=seed_demo.DEMO_EMAIL,
                password_hash='hash',
                role='viewer',
                is_active=True,
                is_demo=False,
                portal_account_id=1,
                created_at=datetime.utcnow(),
            )
        )
        db.commit()

        account = seed_demo.get_or_create_demo_account(db)
        db.flush()

        try:
            seed_demo.ensure_demo_user(db, account.id)
        except RuntimeError as error:
            assert 'Cannot reuse non-demo portal user' in str(error)
        else:
            raise AssertionError('expected seed_demo to reject a non-demo user email collision')

        real_user = db.get(PortalUser, 1)
        assert real_user.is_demo is False
        assert real_user.portal_account_id == 1
        assert real_user.role == 'viewer'
