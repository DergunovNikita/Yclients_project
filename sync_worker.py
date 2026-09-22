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
from database import Database, init_database
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


def process_next_job(database: Database | None = None) -> bool:
    """Claim and run one queued sync job.

    Args:
        database: reused across poll ticks by main()'s loop, so a long-lived engine isn't
            rebuilt every SYNC_WORKER_POLL_INTERVAL. Falls back to a fresh one so this stays
            directly callable (tests, one-off invocations) without a caller-managed instance.
    """
    if database is None:
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
    # Built once and reused for the whole process below. process_next_job() and the idle
    # branch both used to call init_database() on every poll tick (default
    # SYNC_WORKER_POLL_INTERVAL=5s), building a brand new engine and pool each time and
    # never disposing the old one.
    #
    # Measured rather than assumed, because the obvious guesses are both wrong. Constructing
    # 200 throwaway engines back to back against the local copy of production: backends went
    # from a baseline of 2 to a band of 9-20 and stayed there, with no upward trend. So this
    # did NOT exhaust max_connections=100 on its own -- but the discarded engines are also
    # not reclaimed promptly (SQLAlchemy's Engine/Pool hold reference cycles, so they wait on
    # the cycle collector, not refcounting). What it cost was a standing tail of roughly
    # 7-18 idle backends out of a 100-connection budget shared with the API, sized by GC
    # timing rather than by anything the code controls, plus two fresh TCP+auth handshakes
    # every 5 seconds -- on the order of 34k new backends a day for no benefit.
    #
    # pool_pre_ping/pool_recycle on Database exist precisely so one long-lived engine stays
    # healthy across idle periods and DB restarts, which is what makes reusing it here safe.
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    while True:
        processed = process_next_job(database)
        if not processed and not args.once:
            db = database.get_db()
            try:
                jobs = SyncJobService()
                reaped = jobs.reap_stale_jobs(db, max_running_minutes=SYNC_STALE_JOB_MINUTES)
                if reaped:
                    print(f'✓ Reaped {reaped} stale running job(s)')
                auto_result = enqueue_auto_sync_jobs_if_due(db)
                processed = auto_result.get('enqueued', 0) > 0
                # Retention runs LAST and cannot escape this handler, both deliberately.
                # Scope note: this guard covers the two sweeps added here and nothing else.
                # reap_stale_jobs and enqueue_auto_sync_jobs_if_due above remain unguarded, as
                # they were before — a failure in either is still fatal to the worker. That is
                # left alone rather than audited: a reaper that cannot run arguably should
                # crash, whereas a retention sweep that cannot run must not take sync with it.
                # main() has no except of its own, so an exception here would kill the worker;
                # and anything placed before enqueue_auto_sync_jobs_if_due that can throw stops
                # the sync cadence entirely, because every tick afterwards finds nothing queued
                # and lands right back in this branch. These two sweeps only delete old
                # bookkeeping rows -- losing a sweep is a cost worth paying to never lose sync.
                # Two sweeps, two tables, two independent throttles: runs+step_runs here,
                # jobs+job_events there. Neither is the other's cascade. Both sit inside
                # the idle branch, so they never run under --once and never on a tick that
                # claimed a job: a permanently backlogged worker simply never prunes. That
                # is the right trade (draining the queue matters more than trimming logs),
                # but it is why retention can look like it 'never ran'.
                for sweep in (SyncControlService().purge_old_runs_if_due, jobs.purge_old_jobs_if_due):
                    try:
                        sweep(db)
                    except Exception as exc:  # noqa: BLE001 - maintenance must never stop sync
                        print(f'⚠ Retention sweep {sweep.__name__} failed: {exc}')
                        try:
                            db.rollback()
                        except Exception as rollback_exc:  # noqa: BLE001
                            # The likeliest reason a bulk DELETE failed is a dead connection,
                            # which is also the case where rollback() raises — unguarded, it
                            # would escape this handler, pass the finally below, leave main()
                            # (which has no except of its own) and kill the worker. That turns
                            # the designed degradation into a restart loop.
                            print(f'⚠ Rollback after {sweep.__name__} failed: {rollback_exc}')
            finally:
                db.close()
        if args.once:
            return 0 if processed else 1
        if not processed:
            time.sleep(SYNC_WORKER_POLL_INTERVAL)


if __name__ == '__main__':
    raise SystemExit(main())
