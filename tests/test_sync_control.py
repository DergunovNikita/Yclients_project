from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import sync_worker
from models import Base, SyncJob, SyncRun, SyncState, YClientsCredential
from sync_control import SyncControlService
from sync_jobs import SyncJobService


@contextmanager
def _sqlite_session(tables):
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        future=True,
        connect_args={'check_same_thread': False},
        poolclass=StaticPool,
    )
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS system"))
    Base.metadata.create_all(engine, tables=tables)
    session_local = sessionmaker(bind=engine)
    session = session_local()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def test_set_state_serializes_datetime_to_isoformat():
    with _sqlite_session([SyncState.__table__]) as session:
        service = SyncControlService()
        value = datetime(2026, 3, 29, 10, 30, 45, 123456)

        service.set_state(session, 'last_run_started_at', value)

        saved = session.get(SyncState, 'last_run_started_at')
        assert saved is not None
        assert saved.value == '2026-03-29T10:30:45.123456'


def test_status_payload_includes_last_successful_sync_at():
    with _sqlite_session([SyncState.__table__, SyncRun.__table__]) as session:
        service = SyncControlService()

        service.set_state(session, 'last_successful_sync_at', datetime(2026, 5, 29, 10, 3, 59))
        payload = service.get_status_payload(session)

        assert payload['last_successful_sync_at'] == '2026-05-29T10:03:59'


def _sync_state_session():
    return _sqlite_session([SyncState.__table__])


def test_services_label_weekly_sync_skips_when_not_due(monkeypatch):
    calls = {'count': 0}

    async def fake_import():
        calls['count'] += 1
        return {'imported': 1, 'processed': 1, 'skipped': [], 'warnings': []}

    monkeypatch.setattr(sync_worker, '_import_services_labels_async', fake_import)
    monkeypatch.setattr(sync_worker, 'SERVICES_LABEL_SYNC_INTERVAL_DAYS', 7)

    with _sync_state_session() as session:
        first = sync_worker.run_services_label_sync_if_due(session, datetime(2026, 5, 1, 10, 0, 0))
        second = sync_worker.run_services_label_sync_if_due(session, datetime(2026, 5, 3, 10, 0, 0))

        assert first['status'] == 'success'
        assert second == {'status': 'skipped', 'reason': 'not_due'}
        assert calls['count'] == 1


def test_services_label_weekly_sync_records_result_state(monkeypatch):
    async def fake_import():
        return {'imported': 27, 'processed': 144, 'skipped': [], 'warnings': []}

    monkeypatch.setattr(sync_worker, '_import_services_labels_async', fake_import)
    monkeypatch.setattr(sync_worker, 'SERVICES_LABEL_SYNC_INTERVAL_DAYS', 7)

    with _sync_state_session() as session:
        result = sync_worker.run_services_label_sync_if_due(session, datetime(2026, 5, 1, 10, 0, 0))

        assert result['status'] == 'success'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_STATUS_KEY).value == 'success'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_IMPORTED_KEY).value == '27'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_PROCESSED_KEY).value == '144'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_SKIPPED_KEY).value == '0'
        assert session.get(SyncState, sync_worker.SERVICES_LABEL_SYNC_SUCCESS_KEY).value == '2026-05-01T10:00:00'


def _auto_sync_session():
    return _sqlite_session([
        YClientsCredential.__table__,
        SyncJob.__table__,
        SyncRun.__table__,
        SyncState.__table__,
    ])


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
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
        session.add(_credential(7))
        session.commit()

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)
        jobs = session.query(SyncJob).all()

        assert result['enqueued'] == 1
        assert len(jobs) == 1
        assert jobs[0].portal_account_id == 7
        assert jobs[0].initiator == sync_worker.AUTO_SYNC_INITIATOR


@pytest.mark.asyncio
async def test_async_enqueue_job_persists_normalized_queued_defaults(async_session):
    job = await SyncJobService().async_enqueue_job(
        async_session,
        ' Full ',
        'dashboard',
        portal_account_id=7,
        credential_id=11,
        company_ids=[2, 2, 3],
    )

    saved = await async_session.get(SyncJob, job.id)

    assert saved is not None
    assert saved.id == job.id
    assert saved.mode == 'full'
    assert saved.initiator == 'dashboard'
    assert saved.status == 'queued'
    assert saved.portal_account_id == 7
    assert saved.credential_id == 11
    assert saved.company_ids == [2, 3]
    assert saved.progress_pct == 0
    assert saved.current_stage == 'queued'
    assert saved.step_results == []
    assert saved.cancel_requested is False
    assert saved.requested_at is not None


def test_auto_sync_retries_tenant_needing_reauth(monkeypatch):
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
        credential = _credential(7)
        credential.needs_reauth = True
        session.add(credential)
        session.commit()

        result = sync_worker.enqueue_auto_sync_jobs_if_due(session, now)
        jobs = session.query(SyncJob).all()

        assert result['enqueued'] == 1
        assert len(jobs) == 1
        assert jobs[0].portal_account_id == 7


def test_reap_stale_jobs_fails_orphaned_running_and_keeps_fresh():
    now = datetime(2026, 5, 1, 12, 0, 0)

    with _auto_sync_session() as session:
        stale = SyncJob(
            mode='incremental', initiator='auto-worker', status='running',
            portal_account_id=7, company_ids=[], progress_pct=50,
            current_stage='Сотрудники', step_results=[], cancel_requested=False,
            requested_at=now - timedelta(hours=30), started_at=now - timedelta(hours=30),
        )
        fresh = SyncJob(
            mode='incremental', initiator='auto-worker', status='running',
            portal_account_id=8, company_ids=[], progress_pct=10,
            current_stage='Клиенты', step_results=[], cancel_requested=False,
            requested_at=now - timedelta(minutes=3), started_at=now - timedelta(minutes=3),
        )
        session.add_all([stale, fresh])
        session.commit()

        reaped = SyncJobService().reap_stale_jobs(session, max_running_minutes=120, now=now)

        session.refresh(stale)
        session.refresh(fresh)
        assert reaped == 1
        assert stale.status == 'failed'
        assert stale.finished_at is not None
        assert 'stale' in (stale.error_message or '').lower()
        assert fresh.status == 'running'


def test_auto_sync_skips_recent_or_active_jobs(monkeypatch):
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
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


def test_auto_sync_skips_after_recent_global_sync(monkeypatch):
    now = datetime(2026, 5, 1, 12, 0, 0)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_ENABLED', True)
    monkeypatch.setattr(sync_worker, 'SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES', 240)

    with _auto_sync_session() as session:
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
