from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import func, select, text

from models import SyncJob, SyncJobEvent
from sync_parsing import serialize_dt


class SyncJobService:
    def enqueue_job(
        self,
        db,
        mode: str,
        initiator: str,
        *,
        portal_account_id: int | None = None,
        credential_id: int | None = None,
        company_ids: list[int] | None = None,
    ) -> SyncJob:
        job = self._new_queued_job(
            mode,
            initiator,
            portal_account_id=portal_account_id,
            credential_id=credential_id,
            company_ids=company_ids,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def _new_queued_job(
        mode: str,
        initiator: str,
        *,
        portal_account_id: int | None = None,
        credential_id: int | None = None,
        company_ids: list[int] | None = None,
    ) -> SyncJob:
        return SyncJob(
            mode=(mode or 'incremental').strip().lower(),
            initiator=initiator,
            status='queued',
            requested_at=datetime.now(),
            portal_account_id=portal_account_id,
            credential_id=credential_id,
            company_ids=_normalize_company_ids(company_ids),
            progress_pct=0,
            current_stage='queued',
            step_results=[],
            cancel_requested=False,
        )

    def claim_next_job(self, db) -> Optional[SyncJob]:
        row = db.execute(text("""
            SELECT id
            FROM system.sync_jobs
            WHERE status = 'queued'
            ORDER BY id ASC
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        """)).first()
        if row is None:
            db.rollback()
            return None
        job = db.get(SyncJob, row.id)
        job.status = 'running'
        job.started_at = datetime.now()
        job.finished_at = None
        job.error_message = None
        job.current_stage = 'running'
        db.commit()
        db.refresh(job)
        return job

    def release_job_to_queue(self, db, job: SyncJob) -> SyncJob:
        job.status = 'queued'
        job.started_at = None
        job.finished_at = None
        job.run_id = None
        job.error_message = None
        job.progress_pct = 0
        job.current_stage = 'queued'
        db.commit()
        db.refresh(job)
        return job

    def finish_job(self, db, job: SyncJob, result: dict[str, Any]) -> SyncJob:
        job.run_id = result.get('run_id')
        job.finished_at = datetime.now()
        job.status = result.get('status', 'failed')
        job.progress_pct = 100 if job.status == 'success' else job.progress_pct
        job.current_stage = job.status
        if result.get('sync_result', {}).get('step_results') is not None:
            job.step_results = result['sync_result']['step_results']
        if job.status == 'success':
            job.error_message = None
        else:
            job.error_message = result.get('error') or result.get('detail') or result.get('status')
        db.commit()
        db.refresh(job)
        return job

    def reap_stale_jobs(self, db, *, max_running_minutes: int, now: datetime | None = None) -> int:
        """Fail 'running' jobs left orphaned by a worker that died mid-sync.

        Such a job stays 'running' forever: it blocks auto-enqueue for its tenant and shows a
        phantom run in the dashboard. Only jobs whose start is older than max_running_minutes are
        reaped, so a job actively processed by a live worker is never touched.
        """
        if max_running_minutes <= 0:
            return 0
        now = now or datetime.now()
        cutoff = now - timedelta(minutes=max_running_minutes)
        stale = (
            db.query(SyncJob)
            .filter(
                SyncJob.status == 'running',
                func.coalesce(SyncJob.started_at, SyncJob.requested_at) < cutoff,
            )
            .all()
        )
        for job in stale:
            job.status = 'failed'
            job.finished_at = now
            job.current_stage = 'failed'
            job.error_message = 'Reaped stale running job (worker likely died mid-sync)'
        if stale:
            db.commit()
        return len(stale)

    def _scoped_query(self, query, portal_account_id: int | None = None):
        if portal_account_id is not None:
            query = query.filter(SyncJob.portal_account_id == portal_account_id)
        return query

    def get_active_job(self, db, portal_account_id: int | None = None) -> Optional[SyncJob]:
        return (
            self._scoped_query(db.query(SyncJob), portal_account_id)
            .filter(SyncJob.status.in_(('running', 'queued')))
            .order_by(
                SyncJob.status.desc(),
                SyncJob.id.asc(),
            )
            .first()
        )

    def get_latest_job(self, db, portal_account_id: int | None = None) -> Optional[SyncJob]:
        return self._scoped_query(db.query(SyncJob), portal_account_id).order_by(SyncJob.id.desc()).first()

    def get_recent_events(self, db, job_id: int | None, limit: int = 10) -> list[dict[str, Any]]:
        if job_id is None:
            return []
        events = (
            db.query(SyncJobEvent)
            .filter(SyncJobEvent.job_id == job_id)
            .order_by(SyncJobEvent.id.desc())
            .limit(limit)
            .all()
        )
        return [self.serialize_event(event) for event in reversed(events)]

    def get_status_payload(self, db, portal_account_id: int | None = None) -> dict[str, Any]:
        current = self.get_active_job(db, portal_account_id)
        latest = self.get_latest_job(db, portal_account_id)
        base_query = self._scoped_query(db.query(SyncJob), portal_account_id)
        return {
            'queued_jobs': base_query.filter(SyncJob.status == 'queued').count(),
            'running_jobs': self._scoped_query(db.query(SyncJob), portal_account_id).filter(SyncJob.status == 'running').count(),
            'current_job': self.serialize(current),
            'last_job': self.serialize(latest),
            'events': self.get_recent_events(db, current.id if current else (latest.id if latest else None)),
        }

    @staticmethod
    def serialize(job: Optional[SyncJob]) -> Optional[dict[str, Any]]:
        if job is None:
            return None
        return {
            'id': job.id,
            'mode': job.mode,
            'initiator': job.initiator,
            'status': job.status,
            'portal_account_id': job.portal_account_id,
            'credential_id': job.credential_id,
            'company_ids': job.company_ids or [],
            'progress_pct': job.progress_pct,
            'current_stage': job.current_stage,
            'step_results': job.step_results or [],
            'cancel_requested': bool(job.cancel_requested),
            'requested_at': serialize_dt(job.requested_at),
            'started_at': serialize_dt(job.started_at),
            'finished_at': serialize_dt(job.finished_at),
            'run_id': job.run_id,
            'error_message': job.error_message,
        }

    @staticmethod
    def serialize_event(event: SyncJobEvent) -> dict[str, Any]:
        return {
            'id': event.id,
            'job_id': event.job_id,
            'portal_account_id': event.portal_account_id,
            'credential_id': event.credential_id,
            'company_id': event.company_id,
            'stage_key': event.stage_key,
            'status': event.status,
            'elapsed_seconds': event.elapsed_seconds,
            'message': event.message,
            'payload': event.payload or {},
            'created_at': serialize_dt(event.created_at),
        }

    def record_event(
        self,
        db,
        job_id: int | None,
        *,
        portal_account_id: int | None = None,
        credential_id: int | None = None,
        company_id: int | None = None,
        stage_key: str | None = None,
        status: str = 'info',
        elapsed_seconds: float | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> SyncJobEvent | None:
        if job_id is None:
            return None
        event = SyncJobEvent(
            job_id=job_id,
            portal_account_id=portal_account_id,
            credential_id=credential_id,
            company_id=company_id,
            stage_key=stage_key,
            status=status,
            elapsed_seconds=elapsed_seconds,
            message=message,
            payload=payload or {},
            created_at=datetime.now(),
        )
        db.add(event)
        if commit:
            db.commit()
        return event

    def update_progress(
        self,
        db,
        job_id: int | None,
        *,
        progress_pct: int | None = None,
        current_stage: str | None = None,
        step_results: list[dict[str, Any]] | None = None,
    ) -> None:
        if job_id is None:
            return
        job = db.get(SyncJob, job_id)
        if job is None:
            return
        if progress_pct is not None:
            job.progress_pct = max(0, min(100, int(progress_pct)))
        if current_stage is not None:
            job.current_stage = current_stage
        if step_results is not None:
            job.step_results = step_results
        db.commit()

    # --- Async methods (used by FastAPI endpoints) ---

    async def async_enqueue_job(
        self,
        db,
        mode: str,
        initiator: str,
        *,
        portal_account_id: int | None = None,
        credential_id: int | None = None,
        company_ids: list[int] | None = None,
    ) -> SyncJob:
        job = self._new_queued_job(
            mode,
            initiator,
            portal_account_id=portal_account_id,
            credential_id=credential_id,
            company_ids=company_ids,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
        return job

    async def async_get_status_payload(self, db, portal_account_id: int | None = None) -> dict[str, Any]:
        active_stmt = select(SyncJob).where(SyncJob.status.in_(('running', 'queued')))
        latest_stmt = select(SyncJob)
        queued_stmt = select(func.count()).where(SyncJob.status == 'queued')
        running_stmt = select(func.count()).where(SyncJob.status == 'running')
        if portal_account_id is not None:
            active_stmt = active_stmt.where(SyncJob.portal_account_id == portal_account_id)
            latest_stmt = latest_stmt.where(SyncJob.portal_account_id == portal_account_id)
            queued_stmt = queued_stmt.where(SyncJob.portal_account_id == portal_account_id)
            running_stmt = running_stmt.where(SyncJob.portal_account_id == portal_account_id)

        current_result = await db.execute(
            active_stmt
            .order_by(SyncJob.status.desc(), SyncJob.id.asc())
            .limit(1)
        )
        current = current_result.scalar_one_or_none()

        latest_result = await db.execute(
            latest_stmt.order_by(SyncJob.id.desc()).limit(1)
        )
        latest = latest_result.scalar_one_or_none()

        queued_result = await db.execute(queued_stmt)
        running_result = await db.execute(running_stmt)

        event_job_id = current.id if current else (latest.id if latest else None)
        events: list[dict[str, Any]] = []
        if event_job_id is not None:
            event_result = await db.execute(
                select(SyncJobEvent)
                .where(SyncJobEvent.job_id == event_job_id)
                .order_by(SyncJobEvent.id.desc())
                .limit(10)
            )
            events = [self.serialize_event(event) for event in reversed(event_result.scalars().all())]

        return {
            'queued_jobs': queued_result.scalar_one(),
            'running_jobs': running_result.scalar_one(),
            'current_job': self.serialize(current),
            'last_job': self.serialize(latest),
            'events': events,
        }


def _normalize_company_ids(company_ids: list[int] | None) -> list[int]:
    return [int(item) for item in dict.fromkeys(company_ids or [])]
