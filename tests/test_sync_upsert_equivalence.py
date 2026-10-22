"""Pin the column values every sync writes on create and on update.

The create and update branches of each sync used to spell out their columns twice; they now share one field set.
Every payload below fills every column it can, then a second pass changes (or drops) each value, and the complete
tables after both passes are compared with a snapshot captured from the two-branch implementation.
"""
from contextlib import redirect_stdout
from datetime import datetime
import io

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


def _dump(db):
    snapshot = {}
    for model in (
        ServiceCategoryCatalog, ServiceCatalog, AccountCatalog, GoodCategoryCatalog, GoodCatalog,
        Client, Staff, StaffPositionMonth, Appointment, Transaction, FinancialTransaction, GoodTransaction, Comment,
    ):
        columns = [column.key for column in model.__table__.columns]
        order = [getattr(model, column.key) for column in model.__table__.primary_key.columns]
        snapshot[model.__tablename__] = [
            '|'.join(str(getattr(row, column)) for column in columns) for row in db.query(model).order_by(*order)
        ]
    return snapshot


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
            snapshots.append(_dump(db))
    return snapshots


def test_create_and_update_write_the_columns_the_two_branch_implementation_wrote(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=TABLES)
    db = sessionmaker(bind=engine)()
    try:
        snapshots = run_scenario(db, monkeypatch)
        assert snapshots == EXPECTED
    finally:
        db.close()
        engine.dispose()


# Captured by running `run_scenario` against the implementation that spelled the columns out in both branches.
EXPECTED = [{'account_catalog': ['1|5|Cash|0|till|2026-10-21 09:30:00', '1|6|Card|1|None|2026-10-21 09:30:00'],
  'appointments': ['1|50|yclients|1|30|1|7|2025-01-10|2025-01-10 07:00:00|2025-01-09 06:00:00|3600|1|first',
                   '2|51|yclients|1|31|2|None|2025-01-11|None|None|None|0|None'],
  'clients': ['1|40|yclients|Ivan|+7|i@example.com|1990-05-17|4|2025-01-08|5.0|1',
              '2|41|yclients|Petr|None|None|None|0|None|0.0|1'],
  'comments': ['80|80|yclients|1|30|great|2025-01-10 12:00:00|5.0|3|Olga|50|1',
               '81|81|yclients|None|None|ok|2025-01-11 12:00:00|None|None|None|None|1'],
  'financial_transactions': ['60|60|yclients|1|3|Rent|2025-01-10 12:00:00|1500.0|pay|5|1|30|50|8|10|service|1',
                             '61|61|yclients|None|None|None|2025-01-11 '
                             '12:00:00|10.0|None|None|None|None|None|None|None|None|1'],
  'good_catalog': ['1|20|Clay|900.0|800.0|4600|pcs|7|2025-01-05 07:00:00|2026-10-21 09:30:00',
                   '1|21|Gel|None|None|None|None|None|None|2026-10-21 09:30:00'],
  'good_category_catalog': ['1|7|Care|3|2026-10-21 09:30:00', '1|8|Styling|None|2026-10-21 09:30:00'],
  'goods_transactions': ['70|70|yclients|2|1|20|Clay|4|Main|-1.0|900.0|900.0|5.0|30|1|1|2025-01-10 12:00:00',
                         '71|71|yclients|None|None|None|None|None|None|None|None|5.0|None|None|None|1|2025-01-11 '
                         '12:00:00'],
  'service_catalog': ['1|10|Cut|1500.0|3600|1|Cuts|True|2026-10-21 09:30:00',
                      '1|11|Wax nose|300.0|600|None|None|True|2026-10-21 09:30:00'],
  'service_category_catalog': ['1|1|Cuts|3|a1|2026-10-21 09:30:00', '1|2|Wax|None|None|2026-10-21 09:30:00'],
  'staff': ['30|30|yclients|Rauf|rauf@example.com|barber|ТОП-барбер|141895|4.5|12|False|0|99|None|1',
            '31|31|yclients|Ana|None|None|Администратор|None|None|None|True|0|None|None|1'],
  'staff_position_months': ['30|2026-10-01|1|141895|ТОП-барбер|2026-10-21 09:30:00',
                            '31|2026-10-01|1|None|Администратор|2026-10-21 09:30:00'],
  'transactions': ['1|1|10|Cut|1500.0|1600.0|2|1']},
 {'account_catalog': ['1|5|Cash 2|None|None|2026-10-21 09:30:00', '1|6|Card|2|x|2026-10-21 09:30:00'],
  'appointments': ['1|50|yclients|1|31|1|None|2025-01-12|None|None|None|0|None',
                   '2|51|yclients|1|30|2|0|2025-01-11|2025-01-11 08:00:00|None|60|-1|late'],
  'clients': ['1|40|yclients|Ivan renamed|None|None|None|5|2025-01-20|0.0|1',
              '2|41|yclients|Petr|+8|None|1991-01-01|0|None|10.0|1'],
  'comments': ['80|80|yclients|None|31|changed|2025-01-12 12:00:00|None|None|None|None|1',
               '81|81|yclients|2|None|ok|2025-01-11 12:00:00|1.0|None|Z|51|1'],
  'financial_transactions': ['60|60|yclients|None|None|None|2025-01-12 '
                             '12:00:00|99.0|None|None|None|None|51|None|None|None|1',
                             '61|61|yclients|4|None|Tax|2025-01-11 '
                             '13:00:00|11.0|None|6|None|31|None|None|None|good|1'],
  'good_catalog': ['1|20|Clay 2|950.0|None|None|None|None|None|2026-10-21 09:30:00',
                   '1|21|Gel|None|None|1|None|None|2025-01-06 07:00:00|2026-10-21 09:30:00'],
  'good_category_catalog': ['1|7|Care 2|None|2026-10-21 09:30:00', '1|8|Styling|7|2026-10-21 09:30:00'],
  'goods_transactions': ['70|70|yclients|None|None|21|None|None|Second|3.0|None|1.0|None|None|None|1|2025-01-12 '
                         '12:00:00',
                         '71|71|yclients|9|2|None|None|None|None|None|None|None|None|31|2|1|2025-01-13 12:00:00'],
  'service_catalog': ['1|10|Cut renamed|1700.0|None|2|Wax|True|2026-10-21 09:30:00',
                      '1|11|Wax nose|300.0|600|None|None|False|2026-10-21 09:30:00',
                      '1|12|New service|None|60|None|None|True|2026-10-21 09:30:00'],
  'service_category_catalog': ['1|1|Cuts renamed|None|None|2026-10-21 09:30:00', '1|2|Wax|1|b|2026-10-21 09:30:00'],
  'staff': ['30|30|yclients|Rauf R|None|None|Барбер|None|None|None|True|1|None|None|1',
            '31|31|yclients|Ana|None|None|Администратор|None|3.0|None|True|0|1|None|1',
            '32|32|yclients|New|None|None|None|None|None|None|True|1|None|None|1'],
  'staff_position_months': ['30|2026-10-01|1|141895|ТОП-барбер|2026-10-21 09:30:00',
                            '31|2026-10-01|1|None|Администратор|2026-10-21 09:30:00'],
  'transactions': ['1|1|11|Wax nose|300.0|None|1|1']}]
