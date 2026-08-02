from datetime import datetime
from typing import Any, Optional

from sqlalchemy import text

from config import SYNC_LOCK_ID
from models import SyncRun, SyncState, SyncStepRun
from sync_parsing import serialize_dt


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
        db.execute(
            text('SELECT pg_advisory_unlock(:lock_id)'),
            {'lock_id': self._lock_id},
        )
        db.commit()

    def cleanup_stale_runs(self, db) -> None:
        for run in db.query(SyncRun).filter(SyncRun.status == 'running').all():
            run.status = 'abandoned'
            run.finished_at = datetime.now()
            run.message = 'Run marked as abandoned before a new lock-acquired start'
        db.commit()

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
