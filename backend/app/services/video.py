from __future__ import annotations

import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, Tuple

from fastapi import UploadFile

from app.schemas import FrameSample, VideoEnhancementOptions, VideoMetadata


@dataclass
class StoredUploadSession:
    upload_session_id: str
    browser_session_id: str
    workspace_dir: Path
    video_path: Path
    original_filename: str
    mime_type: Optional[str]
    created_at: datetime
    expires_at: datetime


class VideoProcessingServiceError(RuntimeError):
    """Raised when local video processing cannot complete."""


class VideoProcessingService:
    MAX_FRAME_WIDTH = 1024
    JPEG_QUALITY = 82

    def __init__(
        self,
        upload_dir: Path,
        frame_sample_seconds: int,
        upload_session_ttl_seconds: int,
    ) -> None:
        self.upload_dir = upload_dir
        self.frame_sample_seconds = frame_sample_seconds
        self.upload_session_ttl_seconds = upload_session_ttl_seconds
        self.sessions_dir = self.upload_dir / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    async def save_upload(self, upload: UploadFile, browser_session_id: str) -> StoredUploadSession:
        self.cleanup_stale_upload_sessions()
        safe_name = Path(upload.filename or "upload.mp4").name
        extension = Path(safe_name).suffix or ".mp4"
        upload_session_id = uuid.uuid4().hex
        workspace_dir = self._session_dir(upload_session_id)
        workspace_dir.mkdir(parents=True, exist_ok=True)
        destination = workspace_dir / f"source{extension}"

        with destination.open("wb") as output_file:
            while True:
                chunk = await upload.read(1024 * 1024)
                if not chunk:
                    break
                output_file.write(chunk)

        await upload.close()
        created_at = datetime.now(timezone.utc)
        record = StoredUploadSession(
            upload_session_id=upload_session_id,
            browser_session_id=browser_session_id,
            workspace_dir=workspace_dir,
            video_path=destination,
            original_filename=safe_name,
            mime_type=upload.content_type,
            created_at=created_at,
            expires_at=created_at + timedelta(seconds=self.upload_session_ttl_seconds),
        )
        self._write_session_manifest(record)
        return record

    def load_upload_session(self, upload_session_id: str) -> Optional[StoredUploadSession]:
        manifest_path = self._session_manifest_path(upload_session_id)
        if not manifest_path.exists():
            return None

        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            record = StoredUploadSession(
                upload_session_id=str(payload["upload_session_id"]),
                browser_session_id=str(payload["browser_session_id"]),
                workspace_dir=Path(payload["workspace_dir"]),
                video_path=Path(payload["video_path"]),
                original_filename=str(payload["original_filename"]),
                mime_type=payload.get("mime_type"),
                created_at=datetime.fromisoformat(payload["created_at"]),
                expires_at=datetime.fromisoformat(payload["expires_at"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

        if record.expires_at <= datetime.now(timezone.utc):
            self.delete_upload_session(upload_session_id)
            return None

        if not record.video_path.exists():
            self.delete_upload_session(upload_session_id)
            return None

        return record

    def delete_upload_session(self, upload_session_id: str) -> bool:
        workspace_dir = self._session_dir(upload_session_id)
        if not workspace_dir.exists():
            return False
        shutil.rmtree(workspace_dir, ignore_errors=True)
        return True

    def cleanup_stale_upload_sessions(self) -> int:
        removed = 0
        now = datetime.now(timezone.utc)

        for candidate in self.sessions_dir.iterdir():
            if not candidate.is_dir():
                continue
            manifest_path = candidate / "session.json"
            if not manifest_path.exists():
                shutil.rmtree(candidate, ignore_errors=True)
                removed += 1
                continue

            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                expires_at = datetime.fromisoformat(payload["expires_at"])
            except (KeyError, ValueError, json.JSONDecodeError, TypeError):
                shutil.rmtree(candidate, ignore_errors=True)
                removed += 1
                continue

            if expires_at <= now:
                shutil.rmtree(candidate, ignore_errors=True)
                removed += 1

        return removed

    def build_metadata(
        self,
        video_path: Path,
        original_name: str,
        mime_type: Optional[str],
    ) -> Tuple[VideoMetadata, list[str]]:
        notes: list[str] = []
        base_metadata = VideoMetadata(
            filename=original_name,
            mime_type=mime_type,
            size_bytes=video_path.stat().st_size,
        )

        opencv_metadata = self._build_metadata_with_opencv(video_path)
        if opencv_metadata is not None:
            metadata, opencv_notes = opencv_metadata
            metadata.filename = original_name
            metadata.mime_type = mime_type
            return metadata, opencv_notes

        ffprobe_result = self._build_metadata_with_ffprobe(video_path, base_metadata)
        if ffprobe_result is not None:
            metadata, ffprobe_notes = ffprobe_result
            metadata.filename = original_name
            metadata.mime_type = mime_type
            return metadata, ffprobe_notes

        notes.append("Neither OpenCV nor ffprobe could read video metadata, so duration and resolution are unavailable.")
        return base_metadata, notes

    def extract_frames(self, video_path: Path, timestamps: list[float]) -> Tuple[list[FrameSample], list[str]]:
        cv2 = self._import_cv2()
        if cv2 is None:
            return [], ["OpenCV is not installed, so the backend cannot sample frames from the uploaded video."]

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return [], ["OpenCV could not open the video file for frame extraction."]

        frame_dir = video_path.parent / "frames"
        frame_dir.mkdir(parents=True, exist_ok=True)

        frame_samples: list[FrameSample] = []
        for index, timestamp in enumerate(timestamps):
            capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000)
            success, frame = capture.read()
            if not success or frame is None:
                continue

            frame = self._resize_frame_if_needed(cv2, frame)

            destination = frame_dir / f"frame-{index + 1}-{int(timestamp * 1000)}ms.jpg"
            wrote_frame = cv2.imwrite(
                str(destination),
                frame,
                [int(cv2.IMWRITE_JPEG_QUALITY), self.JPEG_QUALITY],
            )
            if not wrote_frame:
                continue

            frame_samples.append(
                FrameSample(
                    timestamp_seconds=round(timestamp, 2),
                    image_path=str(destination),
                )
            )

        capture.release()

        if not frame_samples:
            return [], ["No frames could be extracted from the uploaded video."]

        return frame_samples, [f"Extracted {len(frame_samples)} sampled frames for visual object analysis."]

    def build_upload_expiry(self, record: StoredUploadSession) -> str:
        return record.expires_at.isoformat()

    def prepare_publish_video(
        self,
        upload_session: StoredUploadSession,
        enhancements: VideoEnhancementOptions,
    ) -> tuple[Path, list[str], list[str]]:
        notes: list[str] = []
        applied: list[str] = []

        if not enhancements.visual_pop and not enhancements.audio_cleanup:
            return upload_session.video_path, ["Publishing the original uploaded video without pre-upload enhancements."], applied

        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise VideoProcessingServiceError(
                "ffmpeg is required for pre-upload enhancements. Install ffmpeg on the backend machine or publish without enhancement toggles."
            )

        has_audio_stream = self._has_audio_stream(upload_session.video_path)
        video_filters: list[str] = []
        audio_filters: list[str] = []

        if enhancements.visual_pop:
            video_filters.extend(
                [
                    "eq=contrast=1.08:saturation=1.18:brightness=0.02:gamma=1.02",
                    "unsharp=5:5:0.8:3:3:0.4",
                ]
            )
            applied.append("Visual Pop")
            notes.append("Applied a pop look with stronger contrast, color, and sharpness before upload.")

        if enhancements.audio_cleanup:
            if has_audio_stream:
                audio_filters.extend(
                    [
                        "highpass=f=80",
                        "lowpass=f=12000",
                        "afftdn",
                        "loudnorm=I=-16:TP=-1.5:LRA=11",
                    ]
                )
                applied.append("Audio Cleanup")
                notes.append("Applied audio cleanup before upload with rumble reduction, denoise, and loudness normalization.")
            else:
                notes.append("Audio cleanup was requested, but the upload does not include a detectable audio stream.")

        if not applied:
            return upload_session.video_path, notes or ["No compatible enhancements were applied; publishing the original upload."], applied

        output_path = upload_session.workspace_dir / "publish-enhanced.mp4"
        command = [
            ffmpeg_path,
            "-y",
            "-i",
            str(upload_session.video_path),
            "-map",
            "0:v:0",
        ]

        if video_filters:
            command.extend(["-vf", ",".join(video_filters)])

        command.extend(
            [
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "0",
            ]
        )

        if has_audio_stream:
            command.extend(["-map", "0:a?"])
            if audio_filters:
                command.extend(["-af", ",".join(audio_filters), "-c:a", "aac", "-b:a", "192k"])
            else:
                command.extend(["-c:a", "aac", "-b:a", "192k"])
        else:
            command.append("-an")

        command.append(str(output_path))

        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output_path.exists():
            raise VideoProcessingServiceError(
                f"ffmpeg failed while preparing the video for YouTube upload: {result.stderr.strip() or 'unknown ffmpeg error'}"
            )

        return output_path, notes, applied

    def _build_metadata_with_opencv(self, video_path: Path) -> Optional[Tuple[VideoMetadata, list[str]]]:
        cv2 = self._import_cv2()
        if cv2 is None:
            return None

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return None

        fps_raw = capture.get(cv2.CAP_PROP_FPS) or 0
        frame_count_raw = capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0
        width_raw = capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0
        height_raw = capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0
        capture.release()

        fps = round(float(fps_raw), 2) if fps_raw and fps_raw > 0 else None
        frame_count = float(frame_count_raw) if frame_count_raw and frame_count_raw > 0 else None
        duration = round(frame_count / fps_raw, 2) if fps_raw and frame_count else None
        width = int(width_raw) if width_raw else None
        height = int(height_raw) if height_raw else None

        metadata = VideoMetadata(
            filename=video_path.name,
            size_bytes=video_path.stat().st_size,
            duration_seconds=duration,
            width=width,
            height=height,
            fps=fps,
        )
        notes = ["Video metadata extracted with OpenCV."]
        return metadata, notes

    def _build_metadata_with_ffprobe(
        self,
        video_path: Path,
        base_metadata: VideoMetadata,
    ) -> Optional[Tuple[VideoMetadata, list[str]]]:
        if not shutil.which("ffprobe"):
            return None

        command = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate:format=duration",
            "-of",
            "json",
            str(video_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)

        if result.returncode != 0:
            return None

        try:
            payload = json.loads(result.stdout or "{}")
            stream = (payload.get("streams") or [{}])[0]
            duration = payload.get("format", {}).get("duration")
            fps = self._parse_fps(stream.get("r_frame_rate"))
            base_metadata.duration_seconds = round(float(duration), 2) if duration else None
            base_metadata.width = stream.get("width")
            base_metadata.height = stream.get("height")
            base_metadata.fps = fps
            notes = ["Video metadata probed successfully with ffprobe."]
            return base_metadata, notes
        except (ValueError, TypeError, json.JSONDecodeError):
            return None

    def sample_timestamps(self, metadata: VideoMetadata) -> list[float]:
        duration = metadata.duration_seconds or 12
        if duration <= self.frame_sample_seconds:
            return [0.0]

        timestamps: list[float] = []
        current = 0.0
        while current < duration and len(timestamps) < 6:
            timestamps.append(round(current, 2))
            current += self.frame_sample_seconds

        if duration not in timestamps:
            timestamps.append(round(max(duration - 1, 0), 2))

        return timestamps[:6]

    @staticmethod
    def _parse_fps(raw_fps: Optional[str]) -> Optional[float]:
        if not raw_fps:
            return None
        if "/" not in raw_fps:
            try:
                return round(float(raw_fps), 2)
            except ValueError:
                return None

        numerator, denominator = raw_fps.split("/", maxsplit=1)
        try:
            denominator_value = float(denominator)
            if denominator_value == 0:
                return None
            return round(float(numerator) / denominator_value, 2)
        except ValueError:
            return None

    @staticmethod
    def _has_audio_stream(video_path: Path) -> bool:
        ffprobe_path = shutil.which("ffprobe")
        if not ffprobe_path:
            return False

        command = [
            ffprobe_path,
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "json",
            str(video_path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            return False

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            return False

        return bool(payload.get("streams"))

    @staticmethod
    def _import_cv2() -> Optional[Any]:
        try:
            import cv2  # type: ignore
        except ImportError:
            return None
        return cv2

    def _resize_frame_if_needed(self, cv2: Any, frame: Any) -> Any:
        height, width = frame.shape[:2]
        if width <= self.MAX_FRAME_WIDTH:
            return frame

        scale = self.MAX_FRAME_WIDTH / float(width)
        target_size = (self.MAX_FRAME_WIDTH, max(1, int(height * scale)))
        return cv2.resize(frame, target_size)

    def _session_dir(self, upload_session_id: str) -> Path:
        return self.sessions_dir / upload_session_id

    def _session_manifest_path(self, upload_session_id: str) -> Path:
        return self._session_dir(upload_session_id) / "session.json"

    def _write_session_manifest(self, record: StoredUploadSession) -> None:
        payload = {
            "upload_session_id": record.upload_session_id,
            "browser_session_id": record.browser_session_id,
            "workspace_dir": str(record.workspace_dir),
            "video_path": str(record.video_path),
            "original_filename": record.original_filename,
            "mime_type": record.mime_type,
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
        }
        self._session_manifest_path(record.upload_session_id).write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )
