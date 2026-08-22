import argparse
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
    SYNC_STALE_JOB_MINUTES,
    SYNC_WORKER_POLL_INTERVAL,
)
from database import init_database
from models import SyncJob, YClientsCredential
from sync_control import SyncControlService
from sync_jobs import SyncJobService
from sync_orchestrator import run_sync_job


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


def _job_activity_at(job: SyncJob) -> datetime | None:
    return job.finished_at or job.started_at or job.requested_at


def enqueue_auto_sync_jobs_if_due(db, now: datetime | None = None) -> dict:
    if not SYNC_AUTO_ENQUEUE_ENABLED:
        return {'status': 'disabled', 'enqueued': 0, 'skipped': 0}
    if SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES <= 0:
        return {'status': 'disabled', 'enqueued': 0, 'skipped': 0}

    now = now or datetime.now()
    due_before = now - timedelta(minutes=SYNC_AUTO_ENQUEUE_INTERVAL_MINUTES)
    control = SyncControlService()
    if control.get_running_run(db) is not None:
        return {'status': 'ok', 'enqueued': 0, 'skipped': 1, 'reason': 'sync_running'}
    last_successful_sync_at = _parse_state_datetime(
        control.get_state_values(db, ['last_successful_sync_at']).get('last_successful_sync_at')
    )
    if last_successful_sync_at is not None and last_successful_sync_at > due_before:
        return {'status': 'ok', 'enqueued': 0, 'skipped': 1, 'reason': 'recent_global_sync'}

    jobs = SyncJobService()
    # needs_reauth is intentionally not filtered here: a credential that failed auth once
    # (expired token, transient YClients outage) must still be retried, otherwise the tenant
    # silently drops out of auto-sync forever. The per-tenant cooldown below throttles retries
    # to one per interval, and a successful run clears needs_reauth.
    portal_account_ids = [
        int(portal_account_id)
        for (portal_account_id,) in (
            db.query(YClientsCredential.portal_account_id)
            .filter(YClientsCredential.is_active.is_(True))
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
                reaped = SyncJobService().reap_stale_jobs(db, max_running_minutes=SYNC_STALE_JOB_MINUTES)
                if reaped:
                    print(f'✓ Reaped {reaped} stale running job(s)')
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
