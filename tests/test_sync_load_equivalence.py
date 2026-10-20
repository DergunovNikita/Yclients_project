"""Pin the rows the sync load helpers write.

The loaders were rewritten from one SELECT per row to chunked set queries. These tests run
every loader through a fixture that exercises adoption of legacy rows, primary keys that
collide with another tenant's rows, ids that collide with a key the database itself handed
out, duplicates inside one payload and a purge-and-reload window, and compare the complete
resulting tables with a snapshot captured from the per-row implementation.
"""
from contextlib import redirect_stdout
import io

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from models import (
    Appointment,
    Base,
    Client,
    Comment,
    Company,
    FinancialTransaction,
    GoodTransaction,
    Group,
    Staff,
    StaffPositionMonth,
    SyncSourceState,
    Transaction,
)
from sync_pipeline import (
    sync_comments,
    sync_financial_transactions,
    sync_goods_transactions,
    sync_records,
    sync_staff,
)

TABLES = [
    Group.__table__,
    Company.__table__,
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


class FakeAPI:
    last_dated_fetch_complete = True

    def __init__(self, **payloads):
        self._payloads = payloads

    def get_staff(self, company_id):
        return self._payloads['staff']

    def get_records(self, company_id, start_date=None, end_date=None):
        return self._payloads['records']

    def get_financial_transactions(self, company_id, start_date=None, end_date=None):
        return self._payloads['financial']

    def get_goods_transactions(self, company_id, start_date=None, end_date=None):
        return self._payloads['goods']

    def get_comments(self, company_id, start_date=None, end_date=None):
        return self._payloads['comments']


def _financial(txn_id, day, amount):
    return {'id': txn_id, 'date': f'2025-01-{day:02d} 12:00:00', 'amount': amount, 'record_id': 0}


def _goods(txn_id, day, cost):
    return {'id': txn_id, 'create_date': f'2025-01-{day:02d} 12:00:00', 'cost': cost, 'good': {'id': 1, 'title': 'Wax'}}


def _comment(comment_id, day, text):
    return {'id': comment_id, 'date': f'2025-01-{day:02d} 12:00:00', 'text': text, 'rating': 5, 'master_id': 7}


def _record(record_id, client_id, services):
    return {
        'id': record_id,
        'client': {'id': client_id, 'name': f'Client {client_id}'},
        'staff_id': 7,
        'date': '2025-01-10',
        'datetime': '2025-01-10T10:00:00+0300',
        'services': [{'id': 10 + n, 'title': title, 'cost': 100.0 * (n + 1)} for n, title in enumerate(services)],
    }


def _seed(db):
    db.add(Group(id=1, title='G'))
    db.add_all([Company(id=1, title='Salon 1', group_id=1), Company(id=2, title='Salon 2', group_id=1)])
    db.commit()
    day = '2025-01-%02d 12:00:00'
    from sync_parsing import parse_datetime

    db.add_all([
        # Pre-migration rows: no external_id yet, the primary key doubles as the external id.
        Staff(id=7, name='Legacy staff', company_id=1),
        Client(id=42, name='Legacy client', company_id=1),
        FinancialTransaction(id=5, amount=1.0, date=parse_datetime(day % 5), company_id=1),
        GoodTransaction(id=5, cost=1.0, date=parse_datetime(day % 5), company_id=1),
        Comment(id=5, text='legacy', date=parse_datetime(day % 5), company_id=1),
        Appointment(id=500, date=date_of('2025-01-10'), company_id=1),
        # Already migrated and matched by external id.
        FinancialTransaction(id=6, external_id=6, source_type='yclients', amount=2.0,
                             date=parse_datetime(day % 6), company_id=1),
        # Other tenant: same primary key as ids company 1 is about to receive, never adopted.
        FinancialTransaction(id=7, amount=3.0, date=parse_datetime(day % 7), company_id=2),
        FinancialTransaction(id=100, external_id=100, source_type='yclients', amount=4.0,
                             date=parse_datetime(day % 8), company_id=2),
        GoodTransaction(id=7, cost=3.0, date=parse_datetime(day % 7), company_id=2),
        Comment(id=7, text='other tenant', date=parse_datetime(day % 7), company_id=2),
        Staff(id=8, external_id=8, source_type='yclients', name='Other tenant staff', company_id=2),
    ])
    db.commit()


def date_of(value):
    from sync_parsing import parse_date

    return parse_date(value)


def _dump(db):
    snapshot = {}
    for model in (Client, Staff, Appointment, Transaction, FinancialTransaction, GoodTransaction, Comment):
        columns = [column.key for column in model.__table__.columns]
        rows = db.query(model).order_by(model.id).all()
        snapshot[model.__tablename__] = [
            '|'.join(str(getattr(row, column)) for column in columns) for row in rows
        ]
    return snapshot


def run_scenario(db):
    """Load every table twice (append, then purge-and-reload) and return the rows after each pass."""
    _seed(db)
    first = {
        'staff': [
            {'id': 7, 'name': 'Legacy staff renamed', 'fired': 0},
            {'id': 8, 'name': 'Staff of salon 1', 'fired': 0},
            {'id': 9, 'name': 'New staff', 'fired': 0},
        ],
        'records': [
            _record(500, 42, ['Cut', 'Beard']),
            _record(501, 43, ['Cut']),
            _record(502, 44, []),
            _record(501, 43, ['Cut', 'Wax']),
        ],
        'financial': [_financial(tid, 10, float(tid)) for tid in (5, 6, 7, 100, 101, 102, 103, 7)],
        'goods': [_goods(tid, 10, float(tid)) for tid in (5, 7, 8, 9, 10, 11, 8)],
        'comments': [_comment(cid, 10, f'c{cid}') for cid in (5, 7, 8, 9, 10, 5)],
    }
    second = {
        'staff': first['staff'] + [{'id': 11, 'name': 'Later staff', 'fired': 1}],
        'records': [_record(500, 42, ['Cut']), _record(503, 45, ['Dye', 'Cut', 'Wax'])],
        'financial': [_financial(tid, 11, float(tid) * 10) for tid in (5, 6, 7, 101, 104, 105)],
        'goods': [_goods(tid, 11, float(tid) * 10) for tid in (5, 7, 8, 12, 13)],
        'comments': [_comment(cid, 11, f'd{cid}') for cid in (5, 7, 8, 14)],
    }
    snapshots = []
    out = io.StringIO()
    with redirect_stdout(out):
        for payloads, full_refresh in ((first, False), (second, True)):
            api = FakeAPI(**payloads)
            assert sync_staff(api, db, '1', db_company_id=1)
            assert sync_records(api, db, '1', db_company_id=1, full_refresh=full_refresh, **WINDOW)
            assert sync_financial_transactions(api, db, '1', db_company_id=1, full_refresh=full_refresh, **WINDOW)
            assert sync_goods_transactions(api, db, '1', db_company_id=1, full_refresh=full_refresh, **WINDOW)
            assert sync_comments(api, db, '1', db_company_id=1, full_refresh=full_refresh, **WINDOW)
            snapshots.append(_dump(db))
    return snapshots


def make_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=TABLES)
    return engine, sessionmaker(bind=engine)()


# Captured by running `run_scenario` against the per-row `db.get` implementation.
EXPECTED = [{'clients': ['42|42|yclients|Client 42|None|None|None|0|None|0.0|1',
              '43|43|yclients|Client 43|None|None|None|0|None|0.0|1',
              '44|44|yclients|Client 44|None|None|None|0|None|0.0|1'],
  'staff': ['7|7|yclients|Legacy staff renamed|None|None|None|None|None|None|True|0|None|None|1',
            '8|8|yclients|Other tenant staff|None|None|None|None|None|None|True|0|None|None|2',
            '9|8|yclients|Staff of salon 1|None|None|None|None|None|None|True|0|None|None|1',
            '10|9|yclients|New staff|None|None|None|None|None|None|True|0|None|None|1'],
  'appointments': ['500|500|yclients|1|7|42|None|2025-01-10|2025-01-10 07:00:00|None|None|0|None',
                   '501|501|yclients|1|7|43|None|2025-01-10|2025-01-10 07:00:00|None|None|0|None',
                   '502|502|yclients|1|7|44|None|2025-01-10|2025-01-10 07:00:00|None|None|0|None'],
  'transactions': ['1|500|10|Cut|100.0|None|1|1',
                   '2|500|11|Beard|200.0|None|1|1',
                   '3|501|10|Cut|100.0|None|1|1',
                   '4|501|10|Cut|100.0|None|1|1',
                   '5|501|11|Wax|200.0|None|1|1'],
  'financial_transactions': ['5|5|yclients|None|None|None|2025-01-10 '
                             '12:00:00|5.0|None|None|None|None|None|None|None|None|1',
                             '6|6|yclients|None|None|None|2025-01-10 '
                             '12:00:00|6.0|None|None|None|None|None|None|None|None|1',
                             '7|None|yclients|None|None|None|2025-01-07 '
                             '12:00:00|3.0|None|None|None|None|None|None|None|None|2',
                             '100|100|yclients|None|None|None|2025-01-08 '
                             '12:00:00|4.0|None|None|None|None|None|None|None|None|2',
                             '101|7|yclients|None|None|None|2025-01-10 '
                             '12:00:00|7.0|None|None|None|None|None|None|None|None|1',
                             '102|100|yclients|None|None|None|2025-01-10 '
                             '12:00:00|100.0|None|None|None|None|None|None|None|None|1',
                             '103|101|yclients|None|None|None|2025-01-10 '
                             '12:00:00|101.0|None|None|None|None|None|None|None|None|1',
                             '104|102|yclients|None|None|None|2025-01-10 '
                             '12:00:00|102.0|None|None|None|None|None|None|None|None|1',
                             '105|103|yclients|None|None|None|2025-01-10 '
                             '12:00:00|103.0|None|None|None|None|None|None|None|None|1'],
  'goods_transactions': ['5|5|yclients|None|None|1|Wax|None|None|None|None|5.0|None|None|None|1|2025-01-10 12:00:00',
                         '7|None|yclients|None|None|None|None|None|None|None|None|3.0|None|None|None|2|2025-01-07 '
                         '12:00:00',
                         '8|7|yclients|None|None|1|Wax|None|None|None|None|7.0|None|None|None|1|2025-01-10 12:00:00',
                         '9|8|yclients|None|None|1|Wax|None|None|None|None|8.0|None|None|None|1|2025-01-10 12:00:00',
                         '10|9|yclients|None|None|1|Wax|None|None|None|None|9.0|None|None|None|1|2025-01-10 12:00:00',
                         '11|10|yclients|None|None|1|Wax|None|None|None|None|10.0|None|None|None|1|2025-01-10 '
                         '12:00:00',
                         '12|11|yclients|None|None|1|Wax|None|None|None|None|11.0|None|None|None|1|2025-01-10 '
                         '12:00:00'],
  'comments': ['5|5|yclients|None|7|c5|2025-01-10 12:00:00|5.0|None|None|None|1',
               '7|None|yclients|None|None|other tenant|2025-01-07 12:00:00|None|None|None|None|2',
               '8|7|yclients|None|7|c7|2025-01-10 12:00:00|5.0|None|None|None|1',
               '9|8|yclients|None|7|c8|2025-01-10 12:00:00|5.0|None|None|None|1',
               '10|9|yclients|None|7|c9|2025-01-10 12:00:00|5.0|None|None|None|1',
               '11|10|yclients|None|7|c10|2025-01-10 12:00:00|5.0|None|None|None|1']},
 {'clients': ['42|42|yclients|Client 42|None|None|None|0|None|0.0|1',
              '43|43|yclients|Client 43|None|None|None|0|None|0.0|1',
              '44|44|yclients|Client 44|None|None|None|0|None|0.0|1',
              '45|45|yclients|Client 45|None|None|None|0|None|0.0|1'],
  'staff': ['7|7|yclients|Legacy staff renamed|None|None|None|None|None|None|True|0|None|None|1',
            '8|8|yclients|Other tenant staff|None|None|None|None|None|None|True|0|None|None|2',
            '9|8|yclients|Staff of salon 1|None|None|None|None|None|None|True|0|None|None|1',
            '10|9|yclients|New staff|None|None|None|None|None|None|True|0|None|None|1',
            '11|11|yclients|Later staff|None|None|None|None|None|None|True|1|None|None|1'],
  'appointments': ['1|500|yclients|1|7|42|None|2025-01-10|2025-01-10 07:00:00|None|None|0|None',
                   '2|503|yclients|1|7|45|None|2025-01-10|2025-01-10 07:00:00|None|None|0|None'],
  'transactions': ['1|1|10|Cut|100.0|None|1|1',
                   '2|2|10|Dye|100.0|None|1|1',
                   '3|2|11|Cut|200.0|None|1|1',
                   '4|2|12|Wax|300.0|None|1|1'],
  'financial_transactions': ['5|5|yclients|None|None|None|2025-01-11 '
                             '12:00:00|50.0|None|None|None|None|None|None|None|None|1',
                             '6|6|yclients|None|None|None|2025-01-11 '
                             '12:00:00|60.0|None|None|None|None|None|None|None|None|1',
                             '7|None|yclients|None|None|None|2025-01-07 '
                             '12:00:00|3.0|None|None|None|None|None|None|None|None|2',
                             '100|100|yclients|None|None|None|2025-01-08 '
                             '12:00:00|4.0|None|None|None|None|None|None|None|None|2',
                             '101|7|yclients|None|None|None|2025-01-11 '
                             '12:00:00|70.0|None|None|None|None|None|None|None|None|1',
                             '102|101|yclients|None|None|None|2025-01-11 '
                             '12:00:00|1010.0|None|None|None|None|None|None|None|None|1',
                             '104|104|yclients|None|None|None|2025-01-11 '
                             '12:00:00|1040.0|None|None|None|None|None|None|None|None|1',
                             '105|105|yclients|None|None|None|2025-01-11 '
                             '12:00:00|1050.0|None|None|None|None|None|None|None|None|1'],
  'goods_transactions': ['5|5|yclients|None|None|1|Wax|None|None|None|None|50.0|None|None|None|1|2025-01-11 12:00:00',
                         '7|None|yclients|None|None|None|None|None|None|None|None|3.0|None|None|None|2|2025-01-07 '
                         '12:00:00',
                         '8|7|yclients|None|None|1|Wax|None|None|None|None|70.0|None|None|None|1|2025-01-11 12:00:00',
                         '9|8|yclients|None|None|1|Wax|None|None|None|None|80.0|None|None|None|1|2025-01-11 12:00:00',
                         '12|12|yclients|None|None|1|Wax|None|None|None|None|120.0|None|None|None|1|2025-01-11 '
                         '12:00:00',
                         '13|13|yclients|None|None|1|Wax|None|None|None|None|130.0|None|None|None|1|2025-01-11 '
                         '12:00:00'],
  'comments': ['5|5|yclients|None|7|d5|2025-01-11 12:00:00|5.0|None|None|None|1',
               '7|None|yclients|None|None|other tenant|2025-01-07 12:00:00|None|None|None|None|2',
               '8|7|yclients|None|7|d7|2025-01-11 12:00:00|5.0|None|None|None|1',
               '9|8|yclients|None|7|d8|2025-01-11 12:00:00|5.0|None|None|None|1',
               '14|14|yclients|None|7|d14|2025-01-11 12:00:00|5.0|None|None|None|1']}]


def test_loaders_write_the_rows_the_per_row_implementation_wrote():
    engine, db = make_session()
    try:
        assert run_scenario(db) == EXPECTED
    finally:
        db.close()
        engine.dispose()


def test_loaders_write_the_same_rows_when_the_id_sets_span_several_chunks(monkeypatch):
    import sync_pipeline

    monkeypatch.setattr(sync_pipeline, 'DB_BATCH_SIZE', 2)
    engine, db = make_session()
    try:
        assert run_scenario(db) == EXPECTED
    finally:
        db.close()
        engine.dispose()


def _count_statements(engine, run):
    statements = []

    def record(conn, cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', record)
    try:
        run()
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    return statements


def test_statement_count_does_not_grow_with_the_number_of_new_rows():
    engine, db = make_session()
    try:
        _seed(db)
        ids = range(1000, 1060)
        api = FakeAPI(
            financial=[_financial(tid, 10, 1.0) for tid in ids],
            goods=[_goods(tid, 10, 1.0) for tid in ids],
            comments=[_comment(tid, 10, 'x') for tid in ids],
            records=[_record(tid, tid, ['Cut']) for tid in ids],
        )

        def run():
            with redirect_stdout(io.StringIO()):
                assert sync_financial_transactions(api, db, '1', db_company_id=1, **WINDOW)
                assert sync_goods_transactions(api, db, '1', db_company_id=1, **WINDOW)
                assert sync_comments(api, db, '1', db_company_id=1, **WINDOW)
                assert sync_records(api, db, '1', db_company_id=1, **WINDOW)

        appointment_flushes = []
        event.listen(
            db,
            'before_flush',
            lambda session, *_args: appointment_flushes.append(
                sum(isinstance(obj, Appointment) for obj in session.new)
            ),
        )
        statements = _count_statements(engine, run)
        selects = [statement for statement in statements if statement.lstrip().upper().startswith('SELECT')]
        # 60 new rows per table used to cost two single-row SELECTs each and one flush per appointment.
        assert len(selects) < 40
        assert [count for count in appointment_flushes if count] == [60]
        assert db.query(Transaction).count() == 60
    finally:
        db.close()
        engine.dispose()


def test_adoption_works_when_ids_arrive_as_a_generator():
    """The fact syncs pass a generator; it used to be drained by the first lookup, so adoption never ran."""
    from sync_pipeline import load_existing_or_adopt_legacy_source_map

    engine, db = make_session()
    try:
        _seed(db)
        found = load_existing_or_adopt_legacy_source_map(
            db, FinancialTransaction, 1, (tid for tid in (5, 6, 7)), FinancialTransaction.external_id
        )
        # 5 is a legacy row of this company, 6 is matched by external id, 7 belongs to another tenant.
        assert sorted(found) == [5, 6]
        assert found[5].external_id == 5
        assert db.query(FinancialTransaction).filter_by(id=7).one().external_id is None
    finally:
        db.close()
        engine.dispose()
