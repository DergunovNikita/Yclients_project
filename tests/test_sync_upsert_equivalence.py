"""Pin the column values every sync writes on create and on update.

Every payload below fills every column it can; a second pass then changes (or drops) each value.
After both passes the named columns of each row are compared, rows are addressed by their YClients id
(or catalog key), and foreign keys are read back as YClients ids, never as surrogate primary keys.
"""
from contextlib import redirect_stdout
from datetime import date, datetime
import io

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import sync_pipeline
from models import (
    AccountCatalog,
    Appointment,
    Base,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodCatalog,
    GoodCategoryCatalog,
    GoodTransaction,
    Group,
    ServiceCatalog,
    ServiceCategoryCatalog,
    Staff,
    StaffPositionMonth,
    SyncSourceState,
    Transaction,
)
from sync_pipeline import (
    sync_accounts,
    sync_clients,
    sync_comments,
    sync_financial_transactions,
    sync_good_categories,
    sync_goods,
    sync_goods_transactions,
    sync_records,
    sync_service_categories,
    sync_services,
    sync_staff,
)

TABLES = [
    Group.__table__,
    Company.__table__,
    ServiceCategoryCatalog.__table__,
    ServiceCatalog.__table__,
    AccountCatalog.__table__,
    GoodCategoryCatalog.__table__,
    GoodCatalog.__table__,
    Client.__table__,
    Staff.__table__,
    StaffPositionMonth.__table__,
    Appointment.__table__,
    Transaction.__table__,
    FinancialTransaction.__table__,
    GoodTransaction.__table__,
    Comment.__table__,
    SyncSourceState.__table__,
]
WINDOW = {'start_date': '2025-01-01', 'end_date': '2025-01-31'}
NOW = datetime(2026, 10, 21, 9, 30)


class FakeAPI:
    last_dated_fetch_complete = True

    def __init__(self, payloads):
        self._payloads = payloads

    def get_service_categories(self, company_id):
        return self._payloads['service_categories']

    def get_services(self, company_id, category_id=None):
        return self._payloads['services']

    def get_accounts(self, company_id):
        return self._payloads['accounts']

    def get_good_categories(self, company_id):
        return self._payloads['good_categories']

    def get_goods(self, company_id):
        return self._payloads['goods']

    def get_staff(self, company_id):
        return self._payloads['staff']

    def get_clients(self, company_id):
        return self._payloads['clients']

    def get_records(self, company_id, start_date=None, end_date=None):
        return self._payloads['records']

    def get_financial_transactions(self, company_id, start_date=None, end_date=None):
        return self._payloads['financial']

    def get_goods_transactions(self, company_id, start_date=None, end_date=None):
        return self._payloads['goods_transactions']

    def get_comments(self, company_id, start_date=None, end_date=None):
        return self._payloads['comments']


FIRST = {
    'service_categories': [{'id': 1, 'title': 'Cuts', 'weight': 3, 'api_id': 'a1'}, {'id': 2, 'title': 'Wax'}],
    'services': [
        {'id': 10, 'title': 'Cut', 'price_min': 1500.0, 'duration': 3600, 'category': {'id': 1, 'title': 'Cuts'}},
        {'id': 11, 'title': 'Wax nose', 'price_min': 300.0, 'duration': 600},
    ],
    'accounts': [{'id': 5, 'title': 'Cash', 'type': 0, 'comment': 'till'}, {'id': 6, 'title': 'Card', 'type': 1}],
    'good_categories': [{'id': 7, 'title': 'Care', 'parent_category_id': 3}, {'id': 8, 'title': 'Styling'}],
    'goods': [
        {'good_id': 20, 'title': 'Clay', 'cost': 900.0, 'actual_cost': 800.0, 'barcode': '4600',
         'unit_short_title': 'pcs', 'category_id': 7, 'last_change_date': '2025-01-05T10:00:00+0300'},
        {'id': 21, 'title': 'Gel'},
    ],
    'staff': [
        {'id': 30, 'name': 'Rauf', 'email': 'rauf@example.com', 'specialization': 'barber',
         'position': {'id': 141895, 'title': 'ТОП-барбер'}, 'rating': 4.5, 'votes_count': 12, 'bookable': False,
         'user_id': 99, 'is_fired': False},
        {'id': 31, 'name': 'Ana', 'position': 'Администратор', 'fired': 0},
    ],
    'clients': [
        {'id': 40, 'name': 'Ivan', 'phone': '+7', 'email': 'i@example.com', 'birth_date': '1990-05-17',
         'visits_count': 4, 'last_visit_date': '2025-01-08', 'discount': 5},
        {'id': 41, 'name': 'Petr'},
    ],
    'records': [
        {'id': 50, 'client': {'id': 40, 'name': 'Ivan', 'phone': '+7'}, 'staff_id': 30, 'created_user_id': 7,
         'date': '2025-01-10', 'datetime': '2025-01-10T10:00:00+0300', 'create_date': '2025-01-09T09:00:00+0300',
         'seance_length': 3600, 'attendance': 1, 'comment': 'first',
         'services': [{'id': 10, 'title': 'Cut', 'cost': 1500.0, 'first_cost': 1600.0, 'amount': 2}]},
        {'id': 51, 'client': {'id': 41}, 'staff_id': 31, 'date': '2025-01-11', 'services': []},
    ],
    'financial': [
        {'id': 60, 'document_id': 1, 'expense': {'id': 3, 'title': 'Rent'}, 'date': '2025-01-10 12:00:00',
         'amount': 1500.0, 'comment': 'pay', 'account': {'id': 5}, 'client': {'id': 40}, 'master': {'id': 30},
         'record_id': 50, 'visit_id': 8, 'sold_item_id': 10, 'sold_item_type': 'service'},
        {'id': 61, 'date': '2025-01-11 12:00:00', 'amount': 10.0, 'record_id': 0},
    ],
    'goods_transactions': [
        {'id': 70, 'document_id': 2, 'type_id': 1, 'good': {'id': 20, 'title': 'Clay'},
         'storage': {'id': 4, 'title': 'Main'}, 'amount': -1, 'cost_per_unit': 900.0, 'cost': 900.0,
         'discount': 5.0, 'master': {'id': 30}, 'client': {'id': 40}, 'create_date': '2025-01-10 12:00:00'},
        {'id': 71, 'date': '2025-01-11 12:00:00', 'cost': 5.0},
    ],
    'comments': [
        {'id': 80, 'type': 1, 'master_id': 30, 'text': 'great', 'date': '2025-01-10 12:00:00', 'rating': 5,
         'user_id': 3, 'user_name': 'Olga', 'record_id': 50},
        {'id': 81, 'text': 'ok', 'date': '2025-01-11 12:00:00'},
    ],
}
SECOND = {
    'service_categories': [{'id': 1, 'title': 'Cuts renamed'}, {'id': 2, 'title': 'Wax', 'weight': 1, 'api_id': 'b'}],
    'services': [
        {'id': 10, 'title': 'Cut renamed', 'price_min': 1700.0, 'category': {'id': 2, 'title': 'Wax'}},
        {'id': 12, 'title': 'New service', 'duration': 60},
    ],
    'accounts': [{'id': 5, 'title': 'Cash 2'}, {'id': 6, 'title': 'Card', 'type': 2, 'comment': 'x'}],
    'good_categories': [{'id': 7, 'title': 'Care 2'}, {'id': 8, 'title': 'Styling', 'parent_category_id': 7}],
    'goods': [
        {'good_id': 20, 'title': 'Clay 2', 'cost': 950.0},
        {'id': 21, 'title': 'Gel', 'barcode': '1', 'last_change_date': '2025-01-06T10:00:00+0300'},
    ],
    'staff': [
        {'id': 30, 'name': 'Rauf R', 'email': 'not-an-email', 'position': {'title': 'Барбер'}, 'fired': 1},
        {'id': 31, 'name': 'Ana', 'position': 'Администратор', 'rating': 3.0, 'bookable': True, 'user_id': 1},
        {'id': 32, 'name': 'New', 'is_fired': True},
    ],
    'clients': [
        {'id': 40, 'name': 'Ivan II', 'visits_count': 5, 'last_visit_date': '2025-01-20'},
        {'id': 41, 'name': 'Petr', 'phone': '+8', 'birth_date': '1991-01-01', 'discount': 10},
    ],
    'records': [
        {'id': 50, 'client': {'id': 40, 'name': 'Ivan renamed'}, 'staff_id': 31, 'date': '2025-01-12',
         'services': [{'id': 11, 'title': 'Wax nose', 'cost': 300.0}]},
        {'id': 51, 'client': {'id': 41}, 'staff_id': 30, 'created_user_id': 0, 'date': '2025-01-11',
         'datetime': '2025-01-11T11:00:00+0300', 'seance_length': 60, 'attendance': -1, 'comment': 'late',
         'services': []},
    ],
    'financial': [
        {'id': 60, 'date': '2025-01-12 12:00:00', 'amount': 99.0, 'record_id': 51},
        {'id': 61, 'document_id': 4, 'expense': {'title': 'Tax'}, 'date': '2025-01-11 13:00:00', 'amount': 11.0,
         'account': {'id': 6}, 'master': {'id': 31}, 'record_id': 0, 'sold_item_type': 'good'},
    ],
    'goods_transactions': [
        {'id': 70, 'good': {'id': 21}, 'storage': {'title': 'Second'}, 'amount': 3, 'cost': 1.0,
         'create_date': '2025-01-12 12:00:00'},
        {'id': 71, 'document_id': 9, 'type_id': 2, 'master': {'id': 31}, 'client': {'id': 41},
         'date': '2025-01-13 12:00:00'},
    ],
    'comments': [
        {'id': 80, 'text': 'changed', 'date': '2025-01-12 12:00:00', 'master_id': 31},
        {'id': 81, 'type': 2, 'text': 'ok', 'date': '2025-01-11 12:00:00', 'rating': 1, 'user_name': 'Z',
         'record_id': 51},
    ],
}


def _state(db):
    """Named columns of every row, keyed by YClients id; foreign keys translated to YClients ids."""
    staff_ext = {row.id: row.external_id for row in db.query(Staff)}
    client_ext = {row.id: row.external_id for row in db.query(Client)}
    appointment_ext = {row.id: row.external_id for row in db.query(Appointment)}

    def ext(mapping, key):
        return None if key is None else mapping[key]

    def fields(row, *names):
        return {name: getattr(row, name) for name in names}

    return {
        'service_category': {
            row.category_id: fields(row, 'title', 'weight', 'api_id') for row in db.query(ServiceCategoryCatalog)
        },
        'service': {
            row.service_id: fields(row, 'title', 'price_min', 'duration', 'category_id', 'category_title', 'is_active')
            for row in db.query(ServiceCatalog)
        },
        'account': {row.account_id: fields(row, 'title', 'type', 'comment') for row in db.query(AccountCatalog)},
        'good_category': {
            row.category_id: fields(row, 'title', 'parent_category_id') for row in db.query(GoodCategoryCatalog)
        },
        'good': {
            row.good_id: fields(
                row, 'title', 'cost', 'actual_cost', 'barcode', 'unit_short_title', 'category_id', 'last_change_date'
            )
            for row in db.query(GoodCatalog)
        },
        'staff': {
            row.external_id: fields(
                row, 'name', 'email', 'specialization', 'position', 'position_id', 'rating', 'votes_count',
                'bookable', 'fired', 'user_id',
            )
            for row in db.query(Staff)
        },
        'staff_position': {
            staff_ext[row.staff_id]: (row.position_id, row.position_title) for row in db.query(StaffPositionMonth)
        },
        'client': {
            row.external_id: fields(
                row, 'name', 'phone', 'email', 'birth_date', 'visits_count', 'last_visit_date', 'discount'
            )
            for row in db.query(Client)
        },
        'appointment': {
            row.external_id: {
                **fields(row, 'created_user_id', 'date', 'datetime', 'create_date', 'seance_length', 'attendance',
                         'comment'),
                'staff': ext(staff_ext, row.staff_id),
                'client': ext(client_ext, row.client_id),
            }
            for row in db.query(Appointment)
        },
        'service_line': [
            {**fields(row, 'service_id', 'service_title', 'cost', 'first_cost', 'amount'),
             'appointment': appointment_ext[row.appointment_id]}
            for row in db.query(Transaction).order_by(Transaction.id)
        ],
        'financial': {
            row.external_id: {
                **fields(row, 'document_id', 'expense_id', 'expense_title', 'date', 'amount', 'comment', 'account_id',
                         'record_id', 'visit_id', 'sold_item_id', 'sold_item_type'),
                'client': ext(client_ext, row.client_id),
                'master': ext(staff_ext, row.master_id),
            }
            for row in db.query(FinancialTransaction)
        },
        'goods_transaction': {
            row.external_id: {
                **fields(row, 'document_id', 'type_id', 'good_id', 'good_title', 'storage_id', 'storage_title',
                         'amount', 'cost_per_unit', 'cost', 'discount', 'date'),
                'client': ext(client_ext, row.client_id),
                'master': ext(staff_ext, row.master_id),
            }
            for row in db.query(GoodTransaction)
        },
        'comment': {
            row.external_id: {
                **fields(row, 'type', 'text', 'date', 'rating', 'user_id', 'user_name', 'record_id'),
                'master': ext(staff_ext, row.master_id),
            }
            for row in db.query(Comment)
        },
    }


def run_scenario(db, monkeypatch):
    monkeypatch.setattr(sync_pipeline, 'utcnow', lambda: NOW)
    monkeypatch.setattr(sync_pipeline, '_branch_today', lambda *_args: NOW.date())
    db.add(Group(id=1, title='G'))
    db.add(Company(id=1, title='Salon', group_id=1))
    db.commit()
    snapshots = []
    with redirect_stdout(io.StringIO()):
        for payloads in (FIRST, SECOND):
            api = FakeAPI(payloads)
            assert sync_service_categories(api, db, '1', db_company_id=1)
            assert sync_services(api, db, '1', db_company_id=1)
            assert sync_accounts(api, db, '1', db_company_id=1)
            assert sync_good_categories(api, db, '1', db_company_id=1)
            assert sync_goods(api, db, '1', db_company_id=1)
            assert sync_staff(api, db, '1', db_company_id=1)
            assert sync_clients(api, db, '1', db_company_id=1)
            assert sync_records(api, db, '1', db_company_id=1, **WINDOW)
            assert sync_financial_transactions(api, db, '1', db_company_id=1, **WINDOW)
            assert sync_goods_transactions(api, db, '1', db_company_id=1, **WINDOW)
            assert sync_comments(api, db, '1', db_company_id=1, **WINDOW)
            snapshots.append(_state(db))
    return snapshots


def test_create_writes_every_column_the_payload_carries():
    first, _ = _run()

    assert first['service_category'] == {
        1: {'title': 'Cuts', 'weight': 3, 'api_id': 'a1'},
        2: {'title': 'Wax', 'weight': None, 'api_id': None},
    }
    assert first['service'][10] == {
        'title': 'Cut', 'price_min': 1500.0, 'duration': 3600, 'category_id': 1, 'category_title': 'Cuts',
        'is_active': True,
    }
    assert first['account'][5] == {'title': 'Cash', 'type': 0, 'comment': 'till'}
    assert first['good_category'][7] == {'title': 'Care', 'parent_category_id': 3}
    assert first['good'][20] == {
        'title': 'Clay', 'cost': 900.0, 'actual_cost': 800.0, 'barcode': '4600', 'unit_short_title': 'pcs',
        'category_id': 7, 'last_change_date': datetime(2025, 1, 5, 7, 0),
    }
    assert first['staff'][30] == {
        'name': 'Rauf', 'email': 'rauf@example.com', 'specialization': 'barber', 'position': 'ТОП-барбер',
        'position_id': 141895, 'rating': 4.5, 'votes_count': 12, 'bookable': False, 'fired': 0, 'user_id': 99,
    }
    assert first['staff_position'] == {30: (141895, 'ТОП-барбер'), 31: (None, 'Администратор')}
    assert first['client'][40] == {
        'name': 'Ivan', 'phone': '+7', 'email': 'i@example.com', 'birth_date': date(1990, 5, 17),
        'visits_count': 4, 'last_visit_date': date(2025, 1, 8), 'discount': 5.0,
    }
    assert first['appointment'][50] == {
        'created_user_id': 7, 'date': date(2025, 1, 10), 'datetime': datetime(2025, 1, 10, 7, 0),
        'create_date': datetime(2025, 1, 9, 6, 0), 'seance_length': 3600, 'attendance': 1, 'comment': 'first',
        'staff': 30, 'client': 40,
    }
    assert first['service_line'] == [{
        'service_id': 10, 'service_title': 'Cut', 'cost': 1500.0, 'first_cost': 1600.0, 'amount': 2,
        'appointment': 50,
    }]
    assert first['financial'][60] == {
        'document_id': 1, 'expense_id': 3, 'expense_title': 'Rent', 'date': datetime(2025, 1, 10, 12, 0),
        'amount': 1500.0, 'comment': 'pay', 'account_id': 5, 'record_id': 50, 'visit_id': 8, 'sold_item_id': 10,
        'sold_item_type': 'service', 'client': 40, 'master': 30,
    }
    assert first['goods_transaction'][70] == {
        'document_id': 2, 'type_id': 1, 'good_id': 20, 'good_title': 'Clay', 'storage_id': 4,
        'storage_title': 'Main', 'amount': -1.0, 'cost_per_unit': 900.0, 'cost': 900.0, 'discount': 5.0,
        'date': datetime(2025, 1, 10, 12, 0), 'client': 40, 'master': 30,
    }
    assert first['comment'][80] == {
        'type': '1', 'text': 'great', 'date': datetime(2025, 1, 10, 12, 0), 'rating': 5.0, 'user_id': 3,
        'user_name': 'Olga', 'record_id': 50, 'master': 30,
    }


def test_update_overwrites_every_column_including_the_ones_the_payload_dropped():
    _, second = _run()

    assert second['service_category'] == {
        1: {'title': 'Cuts renamed', 'weight': None, 'api_id': None},
        2: {'title': 'Wax', 'weight': 1, 'api_id': 'b'},
    }
    assert second['service'][10] == {
        'title': 'Cut renamed', 'price_min': 1700.0, 'duration': None, 'category_id': 2, 'category_title': 'Wax',
        'is_active': True,
    }
    # Gone from the listing: kept, but no longer active; a new one is created.
    assert second['service'][11]['is_active'] is False
    assert second['service'][12]['title'] == 'New service' and second['service'][12]['is_active'] is True
    assert second['account'] == {
        5: {'title': 'Cash 2', 'type': None, 'comment': None},
        6: {'title': 'Card', 'type': 2, 'comment': 'x'},
    }
    assert second['good_category'] == {
        7: {'title': 'Care 2', 'parent_category_id': None},
        8: {'title': 'Styling', 'parent_category_id': 7},
    }
    assert second['good'][20] == {
        'title': 'Clay 2', 'cost': 950.0, 'actual_cost': None, 'barcode': None, 'unit_short_title': None,
        'category_id': None, 'last_change_date': None,
    }
    assert second['good'][21]['barcode'] == '1'
    assert second['good'][21]['last_change_date'] == datetime(2025, 1, 6, 7, 0)
    assert second['staff'][30] == {
        'name': 'Rauf R', 'email': None, 'specialization': None, 'position': 'Барбер', 'position_id': None,
        'rating': None, 'votes_count': None, 'bookable': True, 'fired': 1, 'user_id': None,
    }
    assert second['staff'][31]['rating'] == 3.0 and second['staff'][31]['user_id'] == 1
    assert second['staff'][32]['fired'] == 1
    # The month's snapshot is written once; a fired employee is not re-snapshotted.
    assert second['staff_position'] == {30: (141895, 'ТОП-барбер'), 31: (None, 'Администратор')}
    assert second['client'][40] == {
        'name': 'Ivan renamed', 'phone': None, 'email': None, 'birth_date': None, 'visits_count': 5,
        'last_visit_date': date(2025, 1, 20), 'discount': 0.0,
    }
    assert second['client'][41]['phone'] == '+8' and second['client'][41]['discount'] == 10.0
    assert second['appointment'][50] == {
        'created_user_id': None, 'date': date(2025, 1, 12), 'datetime': None, 'create_date': None,
        'seance_length': None, 'attendance': 0, 'comment': None, 'staff': 31, 'client': 40,
    }
    assert second['appointment'][51] == {
        'created_user_id': 0, 'date': date(2025, 1, 11), 'datetime': datetime(2025, 1, 11, 8, 0),
        'create_date': None, 'seance_length': 60, 'attendance': -1, 'comment': 'late', 'staff': 30, 'client': 41,
    }
    assert second['service_line'] == [{
        'service_id': 11, 'service_title': 'Wax nose', 'cost': 300.0, 'first_cost': None, 'amount': 1,
        'appointment': 50,
    }]
    assert second['financial'][60] == {
        'document_id': None, 'expense_id': None, 'expense_title': None, 'date': datetime(2025, 1, 12, 12, 0),
        'amount': 99.0, 'comment': None, 'account_id': None, 'record_id': 51, 'visit_id': None,
        'sold_item_id': None, 'sold_item_type': None, 'client': None, 'master': None,
    }
    assert second['financial'][61]['expense_title'] == 'Tax' and second['financial'][61]['sold_item_type'] == 'good'
    assert second['financial'][61]['master'] == 31 and second['financial'][61]['record_id'] is None
    assert second['goods_transaction'][70] == {
        'document_id': None, 'type_id': None, 'good_id': 21, 'good_title': None, 'storage_id': None,
        'storage_title': 'Second', 'amount': 3.0, 'cost_per_unit': None, 'cost': 1.0, 'discount': None,
        'date': datetime(2025, 1, 12, 12, 0), 'client': None, 'master': None,
    }
    assert second['goods_transaction'][71]['master'] == 31 and second['goods_transaction'][71]['client'] == 41
    assert second['comment'][80] == {
        'type': None, 'text': 'changed', 'date': datetime(2025, 1, 12, 12, 0), 'rating': None, 'user_id': None,
        'user_name': None, 'record_id': None, 'master': 31,
    }
    assert second['comment'][81]['user_name'] == 'Z' and second['comment'][81]['record_id'] == 51


def test_the_second_pass_updates_rows_in_place_instead_of_adding_new_ones():
    first, second = _run()

    for table in ('service_category', 'account', 'good_category', 'good', 'client', 'appointment', 'financial',
                  'goods_transaction', 'comment', 'staff_position'):
        assert set(second[table]) == set(first[table]), table
    # Only the brand-new ids appear.
    assert set(second['service']) - set(first['service']) == {12}
    assert set(second['staff']) - set(first['staff']) == {32}
    assert set(first['service']) <= set(second['service']) and set(first['staff']) <= set(second['staff'])


def _run():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=TABLES)
    db = sessionmaker(bind=engine)()
    monkeypatch = pytest.MonkeyPatch()
    try:
        return run_scenario(db, monkeypatch)
    finally:
        monkeypatch.undo()
        db.close()
        engine.dispose()
