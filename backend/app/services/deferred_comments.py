from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, Optional

from app.services.youtube import YouTubeOAuthService, YouTubeServiceError, YouTubeUploadService

logger = logging.getLogger(__name__)


@dataclass
class DeferredCommentTask:
    task_id: str
    browser_session_id: str
    video_id: str
    text: str
    created_at: datetime
    updated_at: datetime
    next_attempt_at: datetime
    publish_at: Optional[datetime] = None
    attempt_count: int = 0
    status: str = "pending"
    comment_id: Optional[str] = None
    last_error: Optional[str] = None


class DeferredCommentQueue:
    def __init__(
        self,
        queue_dir: Path,
        poll_seconds: int,
        oauth_service: YouTubeOAuthService,
        youtube_upload_service: YouTubeUploadService,
    ) -> None:
        self.queue_dir = queue_dir
        self.poll_seconds = poll_seconds
        self.oauth_service = oauth_service
        self.youtube_upload_service = youtube_upload_service
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self._stop_event = Event()
        self._lock = Lock()
        self._worker: Optional[Thread] = None

    def start(self) -> None:
        if self._worker is not None and self._worker.is_alive():
            return
        self._stop_event.clear()
        self._worker = Thread(target=self._run_loop, name="deferred-comment-worker", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop_event.set()

    def enqueue(
        self,
        *,
        browser_session_id: str,
        video_id: str,
        text: str,
        publish_at: Optional[datetime],
    ) -> DeferredCommentTask:
        now = datetime.now(timezone.utc)
        task = DeferredCommentTask(
            task_id=uuid.uuid4().hex,
            browser_session_id=browser_session_id,
            video_id=video_id,
            text=text.strip(),
            created_at=now,
            updated_at=now,
            next_attempt_at=max(now, publish_at.astimezone(timezone.utc)) if publish_at else now,
            publish_at=publish_at.astimezone(timezone.utc) if publish_at else None,
        )
        self._write_task(task)
        return task

    def cleanup(self, retention_days: int = 7) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
        removed = 0
        for task_path in self.queue_dir.glob("*.json"):
            task = self._read_task_path(task_path)
            if task is None:
                task_path.unlink(missing_ok=True)
                removed += 1
                continue
            if task.status in {"completed", "failed"} and task.updated_at <= cutoff:
                task_path.unlink(missing_ok=True)
                removed += 1
        return removed

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._process_due_tasks()
            except Exception:
                logger.exception("Deferred first-comment worker crashed while processing tasks.")
            self._stop_event.wait(self.poll_seconds)

    def _process_due_tasks(self) -> None:
        now = datetime.now(timezone.utc)
        for task_path in self.queue_dir.glob("*.json"):
            task = self._read_task_path(task_path)
            if task is None or task.status != "pending":
                continue
            if task.next_attempt_at > now:
                continue
            self._process_task(task)

    def _process_task(self, task: DeferredCommentTask) -> None:
        try:
            credentials = self.oauth_service.get_credentials(task.browser_session_id)
        except YouTubeServiceError as error:
            self._reschedule_task(task, str(error), self._next_delay_seconds(task.attempt_count))
            return

        if credentials is None:
            self._reschedule_task(task, "YouTube credentials are not available for deferred comment posting.", 30)
            return

        try:
            privacy_status = self.youtube_upload_service.get_video_privacy_status(credentials, task.video_id)
        except YouTubeServiceError as error:
            self._reschedule_task(task, str(error), self._next_delay_seconds(task.attempt_count))
            return

        if privacy_status == "private":
            self._reschedule_task(
                task,
                "The YouTube video is still private, so comment posting is waiting for it to become visible.",
                self._next_delay_seconds(task.attempt_count),
            )
            return

        try:
            comment_id = self.youtube_upload_service.post_first_comment(
                credentials=credentials,
                video_id=task.video_id,
                text=task.text,
            )
        except YouTubeServiceError as error:
            self._reschedule_task(task, str(error), self._next_delay_seconds(task.attempt_count))
            return

        task.status = "completed"
        task.comment_id = comment_id
        task.last_error = None
        task.updated_at = datetime.now(timezone.utc)
        self._write_task(task)

    def _reschedule_task(self, task: DeferredCommentTask, error: str, delay_seconds: int) -> None:
        task.attempt_count += 1
        task.last_error = error
        task.updated_at = datetime.now(timezone.utc)
        task.next_attempt_at = task.updated_at + timedelta(seconds=delay_seconds)
        if task.attempt_count >= 100:
            task.status = "failed"
        self._write_task(task)

    def _task_path(self, task_id: str) -> Path:
        return self.queue_dir / f"{task_id}.json"

    def _write_task(self, task: DeferredCommentTask) -> None:
        payload = {
            "task_id": task.task_id,
            "browser_session_id": task.browser_session_id,
            "video_id": task.video_id,
            "text": task.text,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "next_attempt_at": task.next_attempt_at.isoformat(),
            "publish_at": task.publish_at.isoformat() if task.publish_at else None,
            "attempt_count": task.attempt_count,
            "status": task.status,
            "comment_id": task.comment_id,
            "last_error": task.last_error,
        }
        with self._lock:
            self._task_path(task.task_id).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _read_task_path(self, task_path: Path) -> Optional[DeferredCommentTask]:
        try:
            payload = json.loads(task_path.read_text(encoding="utf-8"))
            return DeferredCommentTask(
                task_id=str(payload["task_id"]),
                browser_session_id=str(payload["browser_session_id"]),
                video_id=str(payload["video_id"]),
                text=str(payload["text"]),
                created_at=datetime.fromisoformat(payload["created_at"]),
                updated_at=datetime.fromisoformat(payload["updated_at"]),
                next_attempt_at=datetime.fromisoformat(payload["next_attempt_at"]),
                publish_at=datetime.fromisoformat(payload["publish_at"]) if payload.get("publish_at") else None,
                attempt_count=int(payload.get("attempt_count", 0)),
                status=str(payload.get("status", "pending")),
                comment_id=payload.get("comment_id"),
                last_error=payload.get("last_error"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @staticmethod
    def _next_delay_seconds(attempt_count: int) -> int:
        delays = [30, 60, 120, 300, 600, 1800, 3600]
        index = min(max(attempt_count, 0), len(delays) - 1)
        return delays[index]
