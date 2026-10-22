"""Pin the rows the sync loaders write.

Every loader runs through a fixture that exercises adoption of legacy rows, primary keys that
collide with another tenant's rows, ids that collide with a key the database itself handed
out, duplicates inside one payload and a purge-and-reload window. Rows are compared by what
a branch owns: (company, YClients id) -> values, never by the surrogate primary key.
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


def _by_external_id(rows, value):
    """{(company_id, external_id): value(row)}; a duplicated key fails the comparison instead of hiding."""
    keyed = [((row.company_id, row.external_id), value(row)) for row in rows if row.external_id is not None]
    assert len({key for key, _ in keyed}) == len(keyed), 'a YClients id was stored twice for one branch'
    return dict(keyed)


def _facts(db):
    services = {}
    for item in db.query(Transaction).all():
        services.setdefault(item.appointment_id, set()).add(item.service_title)
    appointments = db.query(Appointment).all()
    return {
        'staff': _by_external_id(db.query(Staff), lambda row: (row.name, row.fired)),
        'clients': _by_external_id(db.query(Client), lambda row: row.name),
        'appointments': _by_external_id(db.query(Appointment), lambda row: services.get(row.id, set())),
        'financial': _by_external_id(db.query(FinancialTransaction), lambda row: row.amount),
        'goods': _by_external_id(db.query(GoodTransaction), lambda row: row.cost),
        'comments': _by_external_id(db.query(Comment), lambda row: row.text),
        # Rows of the other tenant that carry no YClients id must come through untouched.
        'other_tenant_unlinked': {
            (model.__tablename__, row.id): getattr(row, column)
            for model, column in ((FinancialTransaction, 'amount'), (GoodTransaction, 'cost'), (Comment, 'text'))
            for row in db.query(model).filter(model.company_id == 2, model.external_id.is_(None))
        },
        # Pre-migration rows of the branch are adopted by the payload's ids, never left beside a twin.
        'unadopted': sorted(
            (model.__tablename__, row.id)
            for model in (Staff, Client, Appointment, FinancialTransaction, GoodTransaction, Comment)
            for row in db.query(model).filter(model.company_id == 1, model.external_id.is_(None))
        ),
        'appointment_ids': {row.external_id: row.id for row in appointments},
    }


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
            snapshots.append(_facts(db))
    return snapshots


def make_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine, tables=TABLES)
    return engine, sessionmaker(bind=engine)()


def _c1(mapping):
    return {(1, external_id): value for external_id, value in mapping.items()}


FIRST_PASS = {
    'staff': {
        **_c1({7: ('Legacy staff renamed', 0), 8: ('Staff of salon 1', 0), 9: ('New staff', 0)}),
        (2, 8): ('Other tenant staff', 0),
    },
    'clients': _c1({42: 'Client 42', 43: 'Client 43', 44: 'Client 44'}),
    # 501 is listed twice in the payload and is stored as one appointment. Its lines are compared as a set on
    # purpose: a repeat inside one payload keeps the first occurrence's lines too; dated fetches de-duplicate by id
    # before the loader sees them, so production never feeds it a repeat.
    'appointments': _c1({500: {'Cut', 'Beard'}, 501: {'Cut', 'Wax'}, 502: set()}),
    'financial': {
        **_c1({5: 5.0, 6: 6.0, 7: 7.0, 100: 100.0, 101: 101.0, 102: 102.0, 103: 103.0}),
        (2, 100): 4.0,
    },
    'goods': _c1({5: 5.0, 7: 7.0, 8: 8.0, 9: 9.0, 10: 10.0, 11: 11.0}),
    'comments': _c1({5: 'c5', 7: 'c7', 8: 'c8', 9: 'c9', 10: 'c10'}),
    'other_tenant_unlinked': {
        ('financial_transactions', 7): 3.0,
        ('goods_transactions', 7): 3.0,
        ('comments', 7): 'other tenant',
    },
    'unadopted': [],
}
SECOND_PASS = {
    'staff': {**FIRST_PASS['staff'], (1, 11): ('Later staff', 1)},
    'clients': _c1({42: 'Client 42', 43: 'Client 43', 44: 'Client 44', 45: 'Client 45'}),
    # The purge-and-reload window drops what the payload no longer lists (501, 502).
    'appointments': _c1({500: {'Cut'}, 503: {'Dye', 'Cut', 'Wax'}}),
    'financial': {**_c1({5: 50.0, 6: 60.0, 7: 70.0, 101: 1010.0, 104: 1040.0, 105: 1050.0}), (2, 100): 4.0},
    'goods': _c1({5: 50.0, 7: 70.0, 8: 80.0, 12: 120.0, 13: 130.0}),
    'comments': _c1({5: 'd5', 7: 'd7', 8: 'd8', 14: 'd14'}),
    'other_tenant_unlinked': FIRST_PASS['other_tenant_unlinked'],
    'unadopted': [],
}


def _check(snapshots):
    first, second = snapshots
    # Compared without surrogate keys; `appointment_ids` is checked separately below.
    assert {key: value for key, value in first.items() if key != 'appointment_ids'} == FIRST_PASS
    assert {key: value for key, value in second.items() if key != 'appointment_ids'} == SECOND_PASS
    # Pre-migration rows are adopted in place: the primary key that doubled as the YClients id survives.
    assert first['appointment_ids'][500] == 500


def test_loaders_adopt_isolate_and_reload_the_rows_of_one_branch():
    engine, db = make_session()
    try:
        _check(run_scenario(db))
    finally:
        db.close()
        engine.dispose()


def test_loaders_write_the_same_rows_when_the_id_sets_span_several_chunks(monkeypatch):
    import sync_pipeline

    monkeypatch.setattr(sync_pipeline, 'DB_BATCH_SIZE', 2)
    engine, db = make_session()
    try:
        _check(run_scenario(db))
    finally:
        db.close()
        engine.dispose()


def _count_selects(engine, run):
    statements = []

    def record(conn, cursor, statement, *_args):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', record)
    try:
        run()
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    return sum(statement.lstrip().upper().startswith('SELECT') for statement in statements)


def _selects_for_new_rows(row_count):
    engine, db = make_session()
    try:
        _seed(db)
        ids = range(1000, 1000 + row_count)
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

        selects = _count_selects(engine, run)
        assert db.query(Transaction).count() == row_count
        return selects
    finally:
        db.close()
        engine.dispose()


def test_select_count_does_not_grow_with_the_number_of_new_rows():
    """The loaders look rows up in chunked set queries, not one SELECT per row."""
    assert _selects_for_new_rows(60) <= _selects_for_new_rows(6) + 2
