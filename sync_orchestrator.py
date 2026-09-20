from datetime import datetime
import traceback

from config import (
    DB_HOST,
    DB_NAME,
    DB_PASSWORD,
    DB_PORT,
    DB_USER,
    SYNC_LOG_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from database import init_database
from sync_control import SyncControlService
from sync_jobs import SyncJobService
from sync_logging import build_log_path, stream_run_output
from sync_notifier import TelegramNotifier, build_sync_message
from sync_pipeline import execute_sync


def run_sync_job(
    mode: str,
    trigger_type: str,
    initiator: str = 'system',
    *,
    job_id: int | None = None,
    portal_account_id: int | None = None,
    credential_id: int | None = None,
    company_ids: list[int] | None = None,
) -> dict:
    normalized_mode = (mode or 'incremental').strip().lower()
    normalized_trigger = (trigger_type or 'manual').strip().lower()
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    control_db = database.get_db()
    # pg_advisory_lock is session-level: it belongs to one physical connection, not to a
    # transaction. An ORM Session hands its connection back to the pool on every commit(),
    # and this function commits many times (create_run, set_state, every progress_callback)
    # while the pipeline session works on the same pool — so the lock and the unlock landed
    # on different connections, pg_advisory_unlock() returned false, and the lock stayed
    # held on an abandoned pooled connection that init_database's module-level global keeps
    # alive forever. Every later run then reported 'already_running'. Reproduced against
    # real PostgreSQL; the lock therefore gets a connection of its own that nothing else
    # can check out.
    lock_conn = database.engine.connect().execution_options(isolation_level='AUTOCOMMIT')
    control = SyncControlService()
    jobs = SyncJobService()
    notifier = TelegramNotifier(TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID)

    try:
        lock_acquired = control.acquire_lock(lock_conn)
    except Exception:
        # acquire_lock() itself can raise (dropped connection, statement timeout) before it
        # ever tells us whether the lock was taken. Either way lock_conn and control_db were
        # already checked out of the pool and must go back.
        lock_conn.close()
        control_db.close()
        raise

    if not lock_acquired:
        try:
            status = control.get_status_payload(control_db)
        finally:
            # get_status_payload() issues three queries; if any of them raises, lock_conn and
            # control_db must still be closed here. acquire_lock() returned False, so this
            # connection never held the lock -- but both connections were already checked out
            # of the pool, and init_database's module-level global keeps the engine (and its
            # pool) alive, so a long-running worker leaks one connection per failed attempt
            # until the pool is exhausted.
            lock_conn.close()
            control_db.close()
        return {
            'started': False,
            'status': 'already_running',
            'detail': status,
        }

    try:
        log_path = build_log_path(SYNC_LOG_DIR, normalized_mode, normalized_trigger)
        control.cleanup_stale_runs(control_db)
        run = control.create_run(control_db, normalized_mode, normalized_trigger, initiator, log_path)
        # Captured now, while control_db is known-good, and kept as a plain value rather than
        # read off `run` later: the Session expires every object it holds on each commit()
        # (see below), so a late `run.started_at` needs a fresh SELECT, and that SELECT is
        # exactly what must not be allowed to stand between the lock and its release.
        run_started_at = run.started_at
    except Exception:
        # These still run before the try/finally below that owns lock cleanup. Without this,
        # a failure here (build_log_path()'s mkdir hitting a permissions problem, a read-only
        # mount or a full disk; or a DB error in cleanup_stale_runs/create_run) leaves lock_conn
        # open and the advisory lock held on it forever -- the same failure mode the dedicated
        # connection above was introduced to fix, just reached before that block starts.
        # release_lock() itself can raise (lock_conn went stale while idle) -- it must not
        # skip the close() calls, or this collapses back into the same leak.
        try:
            control.release_lock(lock_conn)
        finally:
            lock_conn.close()
            control_db.close()
        raise

    result = {
        'started': True,
        'status': 'running',
        'run_id': run.id,
        'log_path': log_path,
    }
    step_results = []
    warning_count = 0
    finished_status = 'failed'
    finished_message = 'Sync interrupted'

    def progress_callback(event: dict) -> None:
        stage_key = event.get('stage_key') or event.get('key') or event.get('current_stage')
        status = event.get('status') or 'info'
        jobs.record_event(
            control_db,
            job_id,
            portal_account_id=event.get('portal_account_id', portal_account_id),
            credential_id=event.get('credential_id', credential_id),
            company_id=event.get('company_id'),
            stage_key=stage_key,
            status=status,
            elapsed_seconds=event.get('elapsed_seconds'),
            message=event.get('message'),
            payload=event.get('payload') or {},
        )
        jobs.update_progress(
            control_db,
            job_id,
            progress_pct=event.get('progress_pct'),
            current_stage=stage_key,
            step_results=event.get('step_results'),
        )

    try:
        with stream_run_output(log_path):
            started_at = datetime.now().isoformat()
            print(f'▶ Sync run started at {started_at}')
            progress_callback({
                'status': 'running',
                'current_stage': 'started',
                'progress_pct': 1,
                'message': 'Sync started',
            })
            sync_result = execute_sync(
                mode=normalized_mode,
                portal_account_id=portal_account_id,
                credential_id=credential_id,
                company_ids=company_ids,
                progress_callback=progress_callback,
                database=database,
            )
            step_results = list(sync_result.get('step_results', []))

            warning_count = sum(1 for item in step_results if not item.get('success'))
            finished_status = 'success' if sync_result.get('success') else 'failed'
            finished_message = (
                f"Sync completed with window {sync_result.get('window_start')}..{sync_result.get('window_end')}; "
                f"warnings={warning_count}; companies={sync_result.get('companies_count', 0)}"
            )
            result.update({
                'status': finished_status,
                'run_id': run.id,
                'log_path': log_path,
                'sync_result': sync_result,
            })
            progress_callback({
                'status': finished_status,
                'current_stage': finished_status,
                'progress_pct': 100 if finished_status == 'success' else 99,
                'message': finished_message,
                'step_results': step_results,
            })
        control.finish_run(control_db, run, finished_status, finished_message, step_results)
    except Exception as exc:
        with stream_run_output(log_path):
            print(traceback.format_exc())
        finished_status = 'failed'
        finished_message = str(exc)
        control.finish_run(control_db, run, finished_status, finished_message, step_results)
        result.update({
            'status': finished_status,
            'error': str(exc),
            'run_id': run.id,
            'log_path': log_path,
        })
        progress_callback({
            'status': finished_status,
            'current_stage': 'failed',
            'progress_pct': 99,
            'message': str(exc),
            'step_results': step_results,
        })
    finally:
        finished_at = datetime.now()
        # Lock release and connection cleanup come first, before any notification work, and
        # nothing below is allowed to run ahead of it. The previous ordering built the Telegram
        # message and sent it before releasing the lock; TelegramNotifier.send() catches
        # requests.RequestException internally, so that call alone could not escape -- but
        # build_sync_message() used to read run.started_at, and this Session expires every
        # object it holds on each commit() (create_run, every set_state, every
        # progress_callback), so that attribute needs a fresh SELECT by this point. If an
        # earlier failure left control_db's transaction aborted (e.g. finish_run's own commit
        # failing partway, then the except block's retry of finish_run failing again on the
        # still-poisoned session), that SELECT raises too -- directly inside this finally,
        # before the release/close block below, leaking the lock exactly like the windows
        # already closed above. Keeping this block first makes that impossible regardless of
        # what a future change to the notification path does.
        try:
            control.release_lock(lock_conn)
        finally:
            lock_conn.close()
            control_db.close()
        try:
            message = build_sync_message(
                mode=normalized_mode,
                trigger_type=normalized_trigger,
                status=finished_status,
                started_at=run_started_at,
                finished_at=finished_at,
                log_path=log_path,
                warning_count=warning_count,
                error_message=None if finished_status == 'success' else finished_message,
            )
            notifier.send(message)
        except Exception:
            # Best-effort from here on: the lock is already released and `result` already
            # holds the true outcome, so a notification failure must not raise out of this
            # function and turn a successful sync into an unhandled exception for the caller.
            with stream_run_output(log_path):
                print(traceback.format_exc())

    return result


def get_sync_status() -> dict:
    database = init_database(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)
    db = database.get_db()
    try:
        return SyncControlService().get_status_payload(db)
    finally:
        db.close()
