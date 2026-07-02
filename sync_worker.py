import argparse
import asyncio
import time
from datetime import datetime, timedelta

from config import (
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    SYNC_AUTO_ENQUEUE_ENABLED,
    SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES,
    SERVICES_LABEL_SYNC_INTERVAL_DAYS,
    SYNC_WORKER_POLL_INTERVAL,
)
from database import build_async_database_url, init_database
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from models import SyncJob, SyncState, YClientsCredential
from plan_import import import_services_sheet_from_config
from sync_control import SyncControlService
from sync_jobs import SyncJobService
from sync_orchestrator import run_sync_job


SERVICES_LABEL_SYNC_ATTEMPT_KEY = 'services_labels_last_attempt_at'
SERVICES_LABEL_SYNC_SUCCESS_KEY = 'services_labels_last_success_at'
SERVICES_LABEL_SYNC_STATUS_KEY = 'services_labels_last_status'
SERVICES_LABEL_SYNC_IMPORTED_KEY = 'services_labels_last_imported'
SERVICES_LABEL_SYNC_PROCESSED_KEY = 'services_labels_last_processed'
SERVICES_LABEL_SYNC_SKIPPED_KEY = 'services_labels_last_skipped'
SERVICES_LABEL_SYNC_ERROR_KEY = 'services_labels_last_error'
AUTO_SYNC_INITIATOR = 'auto-worker'


def parse_args():
    parser = argparse.ArgumentParser(description='YClients BI sync worker')
    parser.add_argument('--once', action='store_true', help='Обработать максимум одну задачу и завершиться')
    return parser.parse_args()


def _parse_state_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _services_label_sync_due(db, now: datetime | None = None) -> bool:
    state = db.get(SyncState, SERVICES_LABEL_SYNC_ATTEMPT_KEY)
    return _services_label_sync_due_from_value(
        getattr(state, 'value', None),
        now,
    )


def _services_label_sync_due_from_value(value: str | None, now: datetime | None = None) -> bool:
    if SERVICES_LABEL_SYNC_INTERVAL_DAYS <= 0:
        return False
    now = now or datetime.now()
    last_attempt_at = _parse_state_datetime(value)
    if last_attempt_at is None:
        return True
    return now - last_attempt_at >= timedelta(days=SERVICES_LABEL_SYNC_INTERVAL_DAYS)


async def _import_services_labels_async() -> dict:
    engine = create_async_engine(
        build_async_database_url(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD),
        pool_pre_ping=True,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            return await import_services_sheet_from_config(session)
    finally:
        await engine.dispose()


def run_services_label_sync_if_due(db, now: datetime | None = None) -> dict:
    now = now or datetime.now()
    state = db.get(SyncState, SERVICES_LABEL_SYNC_ATTEMPT_KEY)
    if not _services_label_sync_due_from_value(getattr(state, 'value', None), now):
        return {'status': 'skipped', 'reason': 'not_due'}

    control = SyncControlService()
    control.set_state(db, SERVICES_LABEL_SYNC_ATTEMPT_KEY, now)
    try:
        result = asyncio.run(_import_services_labels_async())
    except Exception as exc:
        control.set_state(db, SERVICES_LABEL_SYNC_STATUS_KEY, 'failed')
        control.set_state(db, SERVICES_LABEL_SYNC_ERROR_KEY, str(exc))
        print(f'! Weekly services labels sync failed: {exc}')
        return {'status': 'failed', 'error': str(exc)}

    skipped = result.get('skipped') or []
    imported = int(result.get('imported') or 0)
    processed = int(result.get('processed') or 0)
    status = 'success' if processed > 0 and not skipped else 'warning'
    control.set_state(db, SERVICES_LABEL_SYNC_STATUS_KEY, status)
    control.set_state(db, SERVICES_LABEL_SYNC_IMPORTED_KEY, str(imported))
    control.set_state(db, SERVICES_LABEL_SYNC_PROCESSED_KEY, str(processed))
    control.set_state(db, SERVICES_LABEL_SYNC_SKIPPED_KEY, str(len(skipped)))
    control.set_state(db, SERVICES_LABEL_SYNC_ERROR_KEY, '; '.join(skipped[:5]) if skipped else None)
    if status == 'success':
        control.set_state(db, SERVICES_LABEL_SYNC_SUCCESS_KEY, now)
    print(
        '✓ Weekly services labels sync '
        f'{status}: imported={imported}; processed={processed}; skipped={len(skipped)}'
    )
    return {'status': status, 'result': result}


def _job_activity_at(job: SyncJob) -> datetime | None:
    return job.finished_at or job.started_at or job.requested_at


def enqueue_auto_sync_jobs_if_due(db, now: datetime | None = None) -> dict:
    if not SYNC_AUTO_ENQUEUE_ENABLED:
        return {'status': 'disabled', 'enqueued': 0, 'skipped': 0}
    if SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES <= 0:
        return {'status': 'disabled', 'enqueued': 0, 'skipped': 0}

    now = now or datetime.now()
    due_before = now - timedelta(minutes=SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES)
    jobs = SyncJobService()
    portal_account_ids = [
        int(portal_account_id)
        for (portal_account_id,) in (
            db.query(YClientsCredential.portal_account_id)
            .filter(
                YClientsCredential.is_active.is_(True),
                YClientsCredential.needs_reauth.is_(False),
            )
            .distinct()
            .all()
        )
        if portal_account_id is not None
    ]

    enqueued = 0
    skipped = 0
    for portal_account_id in portal_account_ids:
        active = jobs.get_active_job(db, portal_account_id=portal_account_id)
        if active is not None:
            skipped += 1
            continue

        latest = jobs.get_latest_job(db, portal_account_id=portal_account_id)
        latest_activity_at = _job_activity_at(latest) if latest is not None else None
        if latest_activity_at is not None and latest_activity_at > due_before:
            skipped += 1
            continue

        jobs.enqueue_job(
            db,
            'incremental',
            AUTO_SYNC_INITIATOR,
            portal_account_id=portal_account_id,
        )
        enqueued += 1

    if enqueued:
        print(f'✓ Auto sync enqueued {enqueued} tenant job(s)')
    return {'status': 'ok', 'enqueued': enqueued, 'skipped': skipped}


def process_next_job() -> bool:
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    db = database.get_db()
    jobs = SyncJobService()
    try:
        job = jobs.claim_next_job(db)
        if job is None:
            return False

        result = run_sync_job(
            mode=job.mode,
            trigger_type='queued',
            initiator=job.initiator or 'worker',
            job_id=job.id,
            portal_account_id=job.portal_account_id,
            credential_id=job.credential_id,
            company_ids=job.company_ids or None,
        )
        if result.get('status') == 'already_running':
            jobs.release_job_to_queue(db, job)
            return False

        jobs.finish_job(db, job, result)
        return True
    except Exception as exc:
        if 'job' in locals() and job is not None:
            jobs.finish_job(db, job, {'status': 'failed', 'error': str(exc)})
        raise
    finally:
        db.close()


def main():
    args = parse_args()
    while True:
        processed = process_next_job()
        if not processed and not args.once:
            database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
            db = database.get_db()
            try:
                auto_result = enqueue_auto_sync_jobs_if_due(db)
                processed = auto_result.get('enqueued', 0) > 0
            finally:
                db.close()
        if args.once:
            return 0 if processed else 1
        if not processed:
            time.sleep(SYNC_WORKER_POLL_INTERVAL)


if __name__ == '__main__':
    raise SystemExit(main())
