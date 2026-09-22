from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import text

from config import SYNC_LOCK_ID, SYNC_RUN_RETENTION_DAYS, SYNC_RUN_RETENTION_INTERVAL_HOURS
from models import SyncJob, SyncRun, SyncState, SyncStepRun
from sync_parsing import parse_datetime, serialize_dt

_RETENTION_STATE_KEY = 'last_run_retention_at'


class SyncControlService:
    def __init__(self, lock_id: int = SYNC_LOCK_ID):
        self._lock_id = lock_id

    def acquire_lock(self, db) -> bool:
        result = db.execute(
            text('SELECT pg_try_advisory_lock(:lock_id)'),
            {'lock_id': self._lock_id},
        ).scalar()
        return bool(result)

    def release_lock(self, db) -> None:
        released = db.execute(
            text('SELECT pg_advisory_unlock(:lock_id)'),
            {'lock_id': self._lock_id},
        ).scalar()
        db.commit()
        if not released:
            # False means this connection did not hold the lock: acquire_lock() and
            # release_lock() were called on different physical connections, so the real
            # lock is still held elsewhere and every future sync will see "already
            # running" until that connection is closed. Should not happen once the lock
            # lives on its own dedicated connection (see run_sync_job) — print loudly if
            # it ever does, instead of a discarded return value hiding it again.
            print(f'⚠ pg_advisory_unlock({self._lock_id}) returned false — lock was not held on this connection')

    def cleanup_stale_runs(self, db) -> None:
        for run in db.query(SyncRun).filter(SyncRun.status == 'running').all():
            run.status = 'abandoned'
            run.finished_at = datetime.now()
            run.message = 'Run marked as abandoned before a new lock-acquired start'
        db.commit()

    def purge_old_runs(
        self,
        db,
        retention_days: int = SYNC_RUN_RETENTION_DAYS,
        *,
        now: datetime | None = None,
    ) -> int:
        """Delete finished sync_runs older than retention_days, and their sync_step_runs.

        A run still 'running' is never touched regardless of age, and the single most recent
        run is always kept so get_status_payload() can still report a last_run even if sync has
        been broken longer than the retention window.

        A sync_jobs row can keep pointing at an old run via its run_id column (set once in
        SyncJobService.finish_job and cleared nowhere) — that FK has no ON DELETE, so it is
        nulled out first. The job row itself, its status/timings/step_results and its
        sync_job_events are untouched; it just stops naming a run_id that is about to stop
        existing.

        Why that matters is NOT "most runs are referenced" — measured on a copy of production,
        969 sync_runs, 778 of them stale, and exactly ONE referenced by a job (4 sync_jobs
        exist in total, 3 of those with run_id already NULL). Most runs never went through the
        queue at all. The reason is worse than volume: that single reference is enough to abort
        the whole bulk DELETE on a foreign-key violation, and purge_old_runs_if_due commits the
        throttle key BEFORE calling this, so the sweep would then back off a full interval and
        fail the same way on every subsequent run — retention dead permanently, silently, with
        the table growing behind it. Nulling first is what keeps one stray reference from
        costing the whole sweep.
        """
        if retention_days <= 0:
            return 0

        now = now or datetime.now()
        cutoff = now - timedelta(days=retention_days)
        latest = self.get_latest_run(db)
        latest_id = latest.id if latest is not None else None

        # A subquery, not a materialised id list. The list form sent every stale id back as
        # bind parameters, and its size is whatever accumulated while retention was disabled,
        # the worker was down, or the throttle key was stuck -- the long-gap case this design
        # deliberately makes possible. Today that is 778 rows; there is no cap on what it
        # could be, and the statements below reuse it three times.
        stale = db.query(SyncRun.id).filter(
            SyncRun.status != 'running',
            SyncRun.started_at < cutoff,
        )
        if latest_id is not None:
            stale = stale.filter(SyncRun.id != latest_id)
        stale_select = stale.scalar_subquery()

        db.query(SyncJob).filter(SyncJob.run_id.in_(stale_select)).update(
            {SyncJob.run_id: None}, synchronize_session=False
        )
        db.query(SyncStepRun).filter(SyncStepRun.run_id.in_(stale_select)).delete(synchronize_session=False)
        deleted = db.query(SyncRun).filter(SyncRun.id.in_(stale_select)).delete(synchronize_session=False)
        db.commit()
        if deleted:
            print(f'✓ Purged {deleted} sync run(s) older than {retention_days}d')
        return deleted

    def purge_old_runs_if_due(
        self,
        db,
        *,
        retention_days: int = SYNC_RUN_RETENTION_DAYS,
        interval_hours: int = SYNC_RUN_RETENTION_INTERVAL_HOURS,
        now: datetime | None = None,
    ) -> int | None:
        """Throttled purge_old_runs(): runs at most once per interval_hours.

        Meant to be called from the worker's idle branch, which is reached on every poll tick
        (a few seconds) — without this check the retention query would run far more often than
        the cheap, infrequent maintenance task it is meant to be. Returns None when skipped.
        """
        now = now or datetime.now()
        last_at = parse_datetime(self.get_state_values(db, [_RETENTION_STATE_KEY]).get(_RETENTION_STATE_KEY))
        if last_at is not None and now - last_at < timedelta(hours=interval_hours):
            return None
        # The attempt is recorded (and committed) before the delete, not after: a sweep that
        # fails deterministically would otherwise never write the key and so retry its bulk
        # DELETE on every poll tick — every few seconds, forever. Backing off to the normal
        # interval is the right answer for best-effort maintenance; the caller logs the failure.
        self.set_state(db, _RETENTION_STATE_KEY, now)
        return self.purge_old_runs(db, retention_days=retention_days, now=now)

    def create_run(self, db, mode: str, trigger_type: str, initiator: str, log_path: str) -> SyncRun:
        now = datetime.now()
        run = SyncRun(
            mode=mode,
            trigger_type=trigger_type,
            status='running',
            initiator=initiator,
            started_at=now,
            log_path=log_path,
        )
        db.add(run)
        db.commit()
        db.refresh(run)

        self.set_state(db, 'last_run_id', str(run.id))
        self.set_state(db, 'last_run_status', run.status)
        self.set_state(db, 'last_run_mode', mode)
        self.set_state(db, 'last_run_trigger_type', trigger_type)
        self.set_state(db, 'last_run_started_at', now)
        return run

    def finish_run(
        self,
        db,
        run: SyncRun,
        status: str,
        message: str,
        step_results: list[dict[str, Any]],
    ) -> SyncRun:
        now = datetime.now()
        run.status = status
        run.finished_at = now
        run.message = message
        db.commit()

        self._replace_step_results(db, run.id, step_results)
        self.set_state(db, 'last_run_status', status)
        self.set_state(db, 'last_run_finished_at', now)
        if status == 'success':
            self.set_state(db, 'last_successful_run_id', str(run.id))
            self.set_state(db, 'last_successful_sync_at', now)
        return run

    def _replace_step_results(self, db, run_id: int, step_results: list[dict[str, Any]]) -> None:
        db.query(SyncStepRun).filter(SyncStepRun.run_id == run_id).delete()
        created_at = datetime.now()
        for step in step_results:
            db.add(SyncStepRun(
                run_id=run_id,
                step_name=step['name'],
                step_key=step.get('key'),
                status='success' if step.get('success') else 'warning',
                elapsed_seconds=step.get('elapsed'),
                created_at=created_at,
            ))
        db.commit()

    def set_state(self, db, key: str, value: str | datetime | None) -> None:
        state = db.get(SyncState, key)
        if not state:
            state = SyncState(key=key)
            db.add(state)
        state.value = serialize_dt(value)
        state.updated_at = datetime.now()
        db.commit()

    def get_latest_run(self, db) -> Optional[SyncRun]:
        return db.query(SyncRun).order_by(SyncRun.id.desc()).first()

    def get_running_run(self, db) -> Optional[SyncRun]:
        return (
            db.query(SyncRun)
            .filter(SyncRun.status == 'running')
            .order_by(SyncRun.id.desc())
            .first()
        )

    def get_status_payload(self, db) -> dict[str, Any]:
        running = self.get_running_run(db)
        latest = self.get_latest_run(db)
        state_values = self.get_state_values(db, ['last_successful_sync_at'])
        return {
            'running': running is not None,
            'current_run': self._serialize_run(running),
            'last_run': self._serialize_run(latest),
            'last_successful_sync_at': state_values.get('last_successful_sync_at'),
        }

    @staticmethod
    def get_state_values(db, keys: list[str]) -> dict[str, str | None]:
        rows = db.query(SyncState).filter(SyncState.key.in_(keys)).all()
        values = {row.key: row.value for row in rows}
        return {key: values.get(key) for key in keys}

    @staticmethod
    def _serialize_run(run: Optional[SyncRun]) -> Optional[dict[str, Any]]:
        if run is None:
            return None
        return {
            'id': run.id,
            'mode': run.mode,
            'trigger_type': run.trigger_type,
            'status': run.status,
            'initiator': run.initiator,
            'started_at': serialize_dt(run.started_at),
            'finished_at': serialize_dt(run.finished_at),
            'log_path': run.log_path,
            'message': run.message,
        }
