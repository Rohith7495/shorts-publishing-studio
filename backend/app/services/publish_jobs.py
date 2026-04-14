from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from time import monotonic
from typing import Any, Optional


@dataclass
class PublishJobRecord:
    job_id: str
    browser_session_id: str
    state: str = "queued"
    stage: str = "Queued"
    detail: Optional[str] = "Waiting to start."
    progress_percent: Optional[float] = None
    uploaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    remaining_seconds: Optional[float] = None
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    started_monotonic: float = field(default_factory=monotonic)


class PublishJobStore:
    def __init__(self, retention_seconds: int = 6 * 60 * 60) -> None:
        self.retention_seconds = retention_seconds
        self._jobs: dict[str, PublishJobRecord] = {}
        self._lock = Lock()

    def create_job(self, browser_session_id: str) -> PublishJobRecord:
        self.cleanup_stale_jobs()
        record = PublishJobRecord(
            job_id=uuid.uuid4().hex,
            browser_session_id=browser_session_id,
        )
        with self._lock:
            self._jobs[record.job_id] = record
        return record

    def get_job(self, job_id: str, browser_session_id: Optional[str] = None) -> Optional[PublishJobRecord]:
        self.cleanup_stale_jobs()
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return None
            if browser_session_id is not None and record.browser_session_id != browser_session_id:
                return None
            return record

    def update_job(
        self,
        job_id: str,
        *,
        state: Optional[str] = None,
        stage: Optional[str] = None,
        detail: Optional[str] = None,
        progress_percent: Optional[float] = None,
        uploaded_bytes: Optional[int] = None,
        total_bytes: Optional[int] = None,
        remaining_seconds: Optional[float] = None,
    ) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if state is not None:
                record.state = state
            if stage is not None:
                record.stage = stage
            if detail is not None:
                record.detail = detail
            record.progress_percent = progress_percent
            record.uploaded_bytes = uploaded_bytes
            record.total_bytes = total_bytes
            record.remaining_seconds = remaining_seconds
            record.updated_at = datetime.now(timezone.utc)

    def complete_job(self, job_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record.state = "succeeded"
            record.stage = "Complete"
            record.detail = "The YouTube upload workflow finished successfully."
            record.progress_percent = 100.0
            record.remaining_seconds = 0.0
            record.result = result
            record.error = None
            record.updated_at = datetime.now(timezone.utc)

    def fail_job(self, job_id: str, error: str) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            record.state = "failed"
            record.stage = "Failed"
            record.detail = "The YouTube upload workflow stopped before completion."
            record.error = error
            record.remaining_seconds = None
            record.updated_at = datetime.now(timezone.utc)

    def serialize_job(self, record: PublishJobRecord) -> dict[str, Any]:
        elapsed_ms = int(max(0.0, monotonic() - record.started_monotonic) * 1000)
        return {
            "job_id": record.job_id,
            "state": record.state,
            "stage": record.stage,
            "detail": record.detail,
            "progress_percent": record.progress_percent,
            "uploaded_bytes": record.uploaded_bytes,
            "total_bytes": record.total_bytes,
            "remaining_seconds": record.remaining_seconds,
            "elapsed_ms": elapsed_ms,
            "result": record.result,
            "error": record.error,
        }

    def cleanup_stale_jobs(self) -> int:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=self.retention_seconds)
        removed = 0

        with self._lock:
            stale_job_ids = [
                job_id
                for job_id, record in self._jobs.items()
                if record.updated_at <= cutoff and record.state in {"succeeded", "failed"}
            ]
            for job_id in stale_job_ids:
                self._jobs.pop(job_id, None)
                removed += 1

        return removed
