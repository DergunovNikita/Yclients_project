from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import (
    Base,
    Appointment,
    Client,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Group,
    Service,
    ServiceCatalog,
    ServiceCategoryCatalog,
    Staff,
    SyncSourceState,
    Transaction,
)
import sync_pipeline
from sync_pipeline import (
    execute_sync,
    full_sync_start_date,
    sync_clients,
    sync_financial_transactions,
    sync_goods_transactions,
    sync_records,
    sync_services,
    sync_staff,
)
from yclients_credentials import YClientsCredentialValue


class FakeYClientsAPI:
    def __init__(self, staff):
        self._staff = staff

    def get_staff(self, company_id):
        return self._staff


class FakeServicesAPI:
    def __init__(self, services, services_by_category=None):
        self._services = services
        self._services_by_category = services_by_category or {}

    def get_services(self, company_id, staff_id=None, category_id=None):
        if category_id is not None:
            return self._services_by_category.get(category_id, [])
        return self._services


class FakeGoodsTransactionsAPI:
    def __init__(self, txns):
        self._txns = txns

    def get_goods_transactions(self, company_id, start_date=None, end_date=None):
        return self._txns


class FakeFinancialTransactionsAPI:
    def __init__(self, txns):
        self._txns = txns

    def get_financial_transactions(self, company_id, start_date=None, end_date=None):
        return self._txns


class FakeClientsAPI:
    def __init__(self, clients):
        self._clients = clients

    def get_clients(self, company_id):
        return self._clients


class FakeRecordsAPI:
    def __init__(self, records):
        self._records = records

    def get_records(self, company_id, start_date=None, end_date=None):
        return self._records


class FakeSyncDatabase:
    def __init__(self, db):
        self._db = db

    def test_connection(self):
        return True

    def get_db(self):
        return self._db


class FakeSyncAPI:
    def get_groups(self):
        return [{'id': 1, 'title': 'G1', 'companies': [{'id': 10, 'title': 'Salon'}]}]


def test_full_sync_start_date_uses_history_start_when_sync_days_is_unlimited(monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'SYNC_DAYS', 0)
    monkeypatch.setattr(sync_pipeline, 'SYNC_HISTORY_START_DATE', date(2000, 1, 1))
    assert full_sync_start_date(date(2026, 6, 28)) == date(2000, 1, 1)


def test_execute_sync_skips_credentials_without_assigned_companies(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        credential = YClientsCredentialValue(
            id=1,
            title='Orphan credential',
            partner_token='partner',
            login='login',
            password='password',
            company_ids=(),
            portal_account_id=1,
        )
        monkeypatch.setattr(sync_pipeline, 'init_database', lambda *_args, **_kwargs: FakeSyncDatabase(db))
        monkeypatch.setattr(sync_pipeline, 'load_active_credentials_sync', lambda *_args, **_kwargs: [credential])
        monkeypatch.setattr(sync_pipeline, '_build_api_for_credential', lambda _credential: FakeSyncAPI())
        monkeypatch.setattr(sync_pipeline, 'mark_credential_success_sync', lambda *_args, **_kwargs: None)
        monkeypatch.setattr(sync_pipeline, 'mark_credential_failure_sync', lambda *_args, **_kwargs: None)
        monkeypatch.setattr(
            sync_pipeline,
            'resolve_sync_window',
            lambda _db, _end_date, requested_mode: (date(2026, 6, 28), requested_mode),
        )

        result = execute_sync(mode='incremental', end_date=date(2026, 6, 30))

        assert result['success'] is False
        assert result['companies_count'] == 0
        company = db.query(Company).filter(
            Company.portal_account_id == 1,
            Company.source_type == 'yclients',
            Company.external_id == 10,
        ).one_or_none()
        assert company is not None
    finally:
        db.close()
        engine.dispose()


def test_sync_clients_scopes_external_id_by_internal_company():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__, Client.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add_all([
            Company(id=1, title='Salon 1', group_id=1),
            Company(id=2, title='Salon 2', group_id=1),
        ])
        db.commit()

        payload = [{
            'id': 42,
            'name': 'Shared external client',
            'phone': '+100',
            'visits_count': 1,
        }]
        assert sync_clients(FakeClientsAPI(payload), db, '1') is True
        assert sync_clients(FakeClientsAPI(payload), db, '2') is True

        rows = db.query(Client).order_by(Client.company_id).all()
        assert [(row.company_id, row.source_type, row.external_id) for row in rows] == [
            (1, 'yclients', 42),
            (2, 'yclients', 42),
        ]
        assert rows[0].id != rows[1].id
    finally:
        db.close()
        engine.dispose()


def test_sync_records_uses_internal_client_and_appointment_keys():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[Group.__table__, Company.__table__, Client.__table__, Appointment.__table__, Transaction.__table__],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        records = [{
            'id': 500,
            'client': {'id': 42, 'name': 'Record client'},
            'staff_id': 7,
            'date': '2025-01-10',
            'datetime': '2025-01-10T10:00:00+0300',
            'services': [{'id': 10, 'title': 'Cut', 'cost': 1000.0}],
        }]

        assert sync_records(FakeRecordsAPI(records), db, '1') is True

        client = db.query(Client).filter(Client.company_id == 1, Client.external_id == 42).one()
        appointment = db.query(Appointment).filter(Appointment.company_id == 1, Appointment.external_id == 500).one()
        transaction = db.query(Transaction).one()

        assert appointment.client_id == client.id
        assert appointment.id != appointment.external_id
        assert transaction.appointment_id == appointment.id
    finally:
        db.close()
        engine.dispose()


def test_sync_financial_transactions_persists_expense_article_and_source_coverage():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            FinancialTransaction.__table__,
            SyncSourceState.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        api = FakeFinancialTransactionsAPI([{
            'id': 10,
            'date': '2025-01-10 12:00:00',
            'amount': 500,
            'expense': {'id': 7, 'title': 'Пополнение личного счета'},
            'account': {'id': 1},
        }])
        assert sync_financial_transactions(
            api, db, '1', start_date='2025-01-01', end_date='2025-01-31'
        ) is True

        transaction = db.get(FinancialTransaction, 10)
        assert transaction.expense_id == 7
        assert transaction.expense_title == 'Пополнение личного счета'
        state = db.get(
            SyncSourceState,
            {'company_id': 1, 'source': 'financial_transactions_detail'},
        )
        assert state.period_start == date(2025, 1, 1)
        assert state.period_end == date(2025, 1, 31)
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_marks_missing_staff_as_fired():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__, Staff.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.add(Staff(id=1, name='Existing', company_id=1, fired=0))
        db.add(Staff(id=2, name='Stale', company_id=1, fired=0))
        db.commit()

        api = FakeYClientsAPI([
            {
                'id': 1,
                'name': 'Existing',
                'email': 'Existing.Worker@Example.COM',
                'fired': 0,
                'position': {'title': 'Барбер'},
            },
        ])

        assert sync_staff(api, db, '1') is True

        active = db.get(Staff, 1)
        stale = db.get(Staff, 2)
        assert active.email == 'existing.worker@example.com'
        assert active.fired == 0
        assert stale.fired == 1
    finally:
        db.close()
        engine.dispose()


def test_sync_staff_ignores_invalid_staff_email():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=[Group.__table__, Company.__table__, Staff.__table__])
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon', group_id=1))
        db.commit()

        api = FakeYClientsAPI([
            {
                'id': 1,
                'name': 'Worker',
                'email': 'worker.1@portal.local',
                'fired': 0,
            },
        ])

        assert sync_staff(api, db, '1') is True
        assert db.get(Staff, 1).email is None
    finally:
        db.close()
        engine.dispose()


def test_sync_services_writes_shared_ids_to_branch_scoped_catalog():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[Group.__table__, Company.__table__, Service.__table__, ServiceCatalog.__table__],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon 1', group_id=1))
        db.add(Company(id=2, title='Salon 2', group_id=1))
        db.commit()

        service_payload = [{
            'id': 10,
            'title': 'Воск',
            'price_min': 500.0,
            'duration': 900,
            'category': {'id': 100, 'title': 'Уход'},
        }]

        assert sync_services(FakeServicesAPI(service_payload), db, '1') is True
        assert sync_services(FakeServicesAPI(service_payload), db, '2') is True

        assert db.query(Service).count() == 1
        rows = (
            db.query(ServiceCatalog)
            .filter(ServiceCatalog.service_id == 10)
            .order_by(ServiceCatalog.company_id)
            .all()
        )
        assert [(row.company_id, row.service_id, row.title) for row in rows] == [
            (1, 10, 'Воск'),
            (2, 10, 'Воск'),
        ]
    finally:
        db.close()
        engine.dispose()


def test_sync_services_fills_category_from_category_filtered_services():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[
            Group.__table__,
            Company.__table__,
            Service.__table__,
            ServiceCatalog.__table__,
            ServiceCategoryCatalog.__table__,
        ],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon 1', group_id=1))
        db.add(ServiceCategoryCatalog(company_id=1, category_id=100, title='Уход', updated_at=datetime(2025, 1, 1, 0, 0, 0)))
        db.commit()

        api = FakeServicesAPI(
            [{'id': 10, 'title': 'Воск', 'price_min': 500.0, 'duration': 900}],
            services_by_category={100: [{'id': 10, 'title': 'Воск'}]},
        )
        assert sync_services(api, db, '1') is True

        row = db.get(ServiceCatalog, {'company_id': 1, 'service_id': 10})
        assert row.category_id == 100
        assert row.category_title == 'Уход'
    finally:
        db.close()
        engine.dispose()


def test_sync_goods_transactions_preserves_embedded_titles():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(
        engine,
        tables=[Group.__table__, Company.__table__, GoodTransaction.__table__],
    )
    Session = sessionmaker(bind=engine)
    db = Session()
    try:
        db.add(Group(id=1, title='G1'))
        db.add(Company(id=1, title='Salon 1', group_id=1))
        db.commit()

        txns = [{
            'id': 100,
            'document_id': 10,
            'type_id': 1,
            'good': {'id': 200, 'title': 'Archived pomade'},
            'storage': {'id': 300, 'title': 'Archive shelf'},
            'amount': -1,
            'cost': 1200.0,
            'create_date': '2026-01-02T10:00:00+0300',
        }]

        assert sync_goods_transactions(FakeGoodsTransactionsAPI(txns), db, '1') is True

        row = db.get(GoodTransaction, 100)
        assert row.good_id == 200
        assert row.good_title == 'Archived pomade'
        assert row.storage_id == 300
        assert row.storage_title == 'Archive shelf'
    finally:
        db.close()
        engine.dispose()
