from datetime import datetime, timedelta

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sync_worker
from models import Base, SyncJob, SyncRun, SyncState, YClientsCredential
from sync_control import SyncControlService


def test_set_state_serializes_datetime_to_isoformat():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=[SyncState.__table__])
    session_local = sessionmaker(bind=engine)
    session = session_local()
    service = SyncControlService()
    value = datetime(2026, 3, 29, 10, 30, 45, 123456)

    try:
        service.set_state(session, 'last_run_started_at', value)

        saved = session.get(SyncState, 'last_run_started_at')
        assert saved is not None
        assert saved.value == '2026-03-29T10:30:45.123456'
    finally:
        session.close()
        engine.dispose()


def test_status_payload_includes_last_successful_sync_at():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=[SyncState.__table__, SyncRun.__table__])
    session_local = sessionmaker(bind=engine)
    session = session_local()
    service = SyncControlService()

    try:
        service.set_state(session, 'last_successful_sync_at', datetime(2026, 5, 29, 10, 3, 59))
        payload = service.get_status_payload(session)

        assert payload['last_successful_sync_at'] == '2026-05-29T10:03:59'
    finally:
        session.close()
        engine.dispose()


def _sync_state_session():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=[SyncState.__table__])
    session_local = sessionmaker(bind=engine)
    return engine, session_local()


def test_services_label_weekly_sync_skips_when_not_due(monkeypatch):
    engine, session = _sync_state_session()
    calls = {'count': 0}

    async def fake_import():
        calls['count'] += 1
        return {'imported': 1, 'processed': 1, 'skipped': [], 'warnings': []}

    monkeypatch.setattr(sync_worker, '_import_services_labels_async', fake_import)
    monkeypatch.setattr(sync_worker, 'SERVICES_LABEL_SYNC_INTERVAL_DAYS', 7)

    try:
        first = sync_worker.run_services_label_sync_if_due(session, datetime(2026, 5, 1, 10, 0, 0))
        second = sync_worker.run_services_label_sync_if_due(session, datetime(2026, 5, 3, 10, 0, 0))

        assert first['status'] == 'success'
        assert second == {'status': 'skipped', 'reason': 'not_due'}
        assert calls['count'] == 1
    finally:
        session.close()
        engine.dispose()


def test_services_label_weekly_sync_records_result_state(monkeypatch):
    engine, session = _sync_state_session()

    async def fake_import():
        return {'imported': 27, 'processed': 144, 'skipped': [], 'warnings': []}

    monkeypatch.setattr(sync_worker, '_import_services_labels_async', fake_import)
    monkeypatch.setattr(sync_worker, 'SERVICES_LABEL_SYNC_INTERVAL_DAYS', 7)

    try:
        result = sync_worker.run_services_label_sync_if_due(session, datetime(2026, 5, 1, 10, 0, 0))

        assert result['status'] == 'success'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_STATUS_KEY).value == 'success'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_IMPORTED_KEY).value == '27'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_PROCESSED_KEY).value == '144'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_SKIPPED_KEY).value == '0'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_SUCCESS_KEY).value == '2026-05-01T10:00:00'
    finally:
        session.close()
        engine.dispose()


def _auto_sync_session():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=[
        YClientsCredential.__table__,
        SyncJob.__table__,
        SyncRun.__table__,
        SyncState.__table__,
    ])
    session_local = sessionmaker(bind=engine)
    return engine, session_local()


def _credential(portal_account_id: int) -> YClientsCredential:
    now = datetime(2026, 5, 1, 10, 0, 0)
    return YClientsCredential(
        portal_account_id=portal_account_id,
        title=f'Tenant {portal_account_id}',
        partner_token_encrypted='token',
        login_encrypted='login',
        password_encrypted='password',
        is_active=True,
        needs_reauth=False,
        created_at=now,
        updated_at=now,
    )


def test_auto_sync_enqueues_due_tenant(monkeypatch):
    engine, session = _auto_sync_session()
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    try:
        session.add(_credential(7))
        session.commit()

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)
        jobs = session.query(SyncJob).all()

        assert result['enqueued'] == 1
        assert len(jobs) == 1
        assert jobs[0].portal_account_id == 7
        assert jobs[0].initiator == sync_worker.AUTO_SYNC_INITIATOR
    finally:
        session.close()
        engine.dispose()


def test_auto_sync_skips_recent_or_active_jobs(monkeypatch):
    engine, session = _auto_sync_session()
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    try:
        session.add_all([_credential(7), _credential(8)])
        session.add(SyncJob(
            mode='incremental',
            initiator='pytest',
            status='success',
            portal_account_id=7,
            company_ids=[],
            progress_pct=100,
            current_stage='success',
            step_results=[],
            cancel_requested=False,
            requested_at=now - timedelta(hours=1),
            finished_at=now - timedelta(hours=1),
        ))
        session.add(SyncJob(
            mode='incremental',
            initiator='pytest',
            status='queued',
            portal_account_id=8,
            company_ids=[],
            progress_pct=0,
            current_stage='queued',
            step_results=[],
            cancel_requested=False,
            requested_at=now - timedelta(hours=5),
        ))
        session.commit()

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)

        assert result['enqueued'] == 0
        assert result['skipped'] == 2
        assert session.query(SyncJob).count() == 2
    finally:
        session.close()
        engine.dispose()


def test_auto_sync_skips_after_recent_global_sync(monkeypatch):
    engine, session = _auto_sync_session()
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    try:
        session.add(_credential(7))
        SyncControlService().set_state(session, 'last_successful_sync_at', now - timedelta(minutes=30))

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)

        assert result == {
            'status': 'ok',
            'enqueued': 0,
            'skipped': 1,
            'reason': 'recent_global_sync',
        }
        assert session.query(SyncJob).count() == 0
    finally:
        session.close()
        engine.dispose()
