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
    THUMBNAIL_SIZE = (1280, 720)
    THUMBNAIL_MAX_TEXT_WIDTH_RATIO = 0.72

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

    def render_thumbnail_preview(
        self,
        upload_session: StoredUploadSession,
        text: str,
        preferred_timestamp_seconds: Optional[float] = None,
    ) -> Path:
        frame_sample = self._select_thumbnail_frame(upload_session, preferred_timestamp_seconds)
        if frame_sample is None:
            raise VideoProcessingServiceError("No sampled frames are available to build a thumbnail preview.")

        destination = upload_session.workspace_dir / "thumbnail-preview.jpg"
        self._compose_thumbnail_image(
            source_image=Path(frame_sample.image_path),
            text=text,
            destination=destination,
        )
        return destination

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

    def _select_thumbnail_frame(
        self,
        upload_session: StoredUploadSession,
        preferred_timestamp_seconds: Optional[float],
    ) -> Optional[FrameSample]:
        frame_samples = self._load_saved_frame_samples(upload_session)
        if not frame_samples:
            return None

        if preferred_timestamp_seconds is None:
            return frame_samples[len(frame_samples) // 2]

        return min(
            frame_samples,
            key=lambda sample: abs(sample.timestamp_seconds - preferred_timestamp_seconds),
        )

    def _load_saved_frame_samples(self, upload_session: StoredUploadSession) -> list[FrameSample]:
        frame_dir = upload_session.workspace_dir / "frames"
        if not frame_dir.exists():
            return []

        frame_samples: list[FrameSample] = []
        for frame_path in sorted(frame_dir.glob("*.jpg")):
            stem_parts = frame_path.stem.split("-")
            timestamp_ms = stem_parts[-1].removesuffix("ms") if stem_parts else ""
            try:
                timestamp_seconds = round(int(timestamp_ms) / 1000, 2)
            except ValueError:
                continue

            frame_samples.append(
                FrameSample(
                    timestamp_seconds=timestamp_seconds,
                    image_path=str(frame_path),
                )
            )

        return frame_samples

    def _compose_thumbnail_image(self, source_image: Path, text: str, destination: Path) -> None:
        image_module, image_color_module, image_draw_module, image_font_module = self._import_pillow_modules()

        with image_module.open(source_image) as raw_image:
            base_image = raw_image.convert("RGB")

        thumbnail = self._crop_to_thumbnail_canvas(image_module, base_image)
        canvas = thumbnail.convert("RGBA")
        overlay = image_module.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay_draw = image_draw_module.Draw(overlay)
        width, height = canvas.size

        overlay_draw.rounded_rectangle(
            (
                int(width * 0.28),
                int(height * 0.58),
                int(width * 0.72),
                int(height * 0.93),
            ),
            radius=36,
            fill=(13, 13, 13, 150),
        )
        overlay_draw.rounded_rectangle(
            (
                int(width * 0.31),
                int(height * 0.61),
                int(width * 0.69),
                int(height * 0.635),
            ),
            radius=12,
            fill=(255, 107, 53, 230),
        )

        composed = image_module.alpha_composite(canvas, overlay)
        draw = image_draw_module.Draw(composed)
        accent_color = image_color_module.getrgb("#fff8ef")
        font = self._load_thumbnail_font(image_font_module, size=88)
        wrapped_lines = self._wrap_thumbnail_text(
            draw=draw,
            text=text,
            font=font,
            max_width=int(width * 0.38),
        )

        current_font_size = getattr(font, "size", 88)
        while len(wrapped_lines) > 3 and current_font_size > 56:
            current_font_size -= 6
            font = self._load_thumbnail_font(image_font_module, size=current_font_size)
            wrapped_lines = self._wrap_thumbnail_text(
                draw=draw,
                text=text,
                font=font,
                max_width=int(width * 0.38),
            )

        line_gap = 12
        line_heights = []
        for line in wrapped_lines:
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=5)
            line_heights.append(max(1, bbox[3] - bbox[1]))
        total_height = sum(line_heights) + line_gap * max(0, len(wrapped_lines) - 1)
        current_y = int(height * 0.76 - total_height / 2)
        center_x = int(width * 0.5)

        for index, line in enumerate(wrapped_lines):
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=5)
            line_width = max(1, bbox[2] - bbox[0])
            x_position = center_x - int(line_width / 2)
            draw.text(
                (x_position, current_y),
                line,
                font=font,
                fill=accent_color,
                stroke_width=5,
                stroke_fill=(19, 17, 33),
            )
            current_y += line_heights[index] + line_gap

        destination.parent.mkdir(parents=True, exist_ok=True)
        composed.convert("RGB").save(destination, format="JPEG", quality=92, optimize=True)

    def _crop_to_thumbnail_canvas(self, image_module: Any, source_image: Any) -> Any:
        target_width, target_height = self.THUMBNAIL_SIZE
        target_ratio = target_width / target_height
        source_width, source_height = source_image.size
        source_ratio = source_width / source_height if source_height else target_ratio

        if source_ratio > target_ratio:
            crop_width = int(source_height * target_ratio)
            left = max(0, int((source_width - crop_width) / 2))
            top = 0
            right = left + crop_width
            bottom = source_height
        else:
            crop_height = int(source_width / target_ratio)
            top_bias = 0.38 if source_height > source_width else 0.5
            top = max(0, min(source_height - crop_height, int((source_height - crop_height) * top_bias)))
            left = 0
            right = source_width
            bottom = top + crop_height

        cropped = source_image.crop((left, top, right, bottom))
        return cropped.resize((target_width, target_height), image_module.Resampling.LANCZOS)

    def _wrap_thumbnail_text(
        self,
        draw: Any,
        text: str,
        font: Any,
        max_width: int,
    ) -> list[str]:
        words = text.strip().split()
        if not words:
            return ["WATCH THIS"]

        lines: list[str] = []
        current = words[0]

        for word in words[1:]:
            candidate = f"{current} {word}"
            bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=5)
            if bbox[2] <= max_width:
                current = candidate
                continue
            lines.append(current)
            current = word

        lines.append(current)
        return lines[:3]

    @staticmethod
    def _load_thumbnail_font(image_font_module: Any, size: int) -> Any:
        candidate_paths = [
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
            "/Library/Fonts/Arial Bold.ttf",
        ]

        for candidate in candidate_paths:
            if Path(candidate).exists():
                try:
                    return image_font_module.truetype(candidate, size=size)
                except OSError:
                    continue

        return image_font_module.load_default()

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

    @staticmethod
    def _import_pillow_modules() -> tuple[Any, Any, Any, Any]:
        try:
            from PIL import Image, ImageColor, ImageDraw, ImageFont  # type: ignore
        except ImportError as error:
            raise VideoProcessingServiceError(
                "Pillow is required for custom thumbnail generation. Install it with `pip install -r requirements.txt`."
            ) from error
        return Image, ImageColor, ImageDraw, ImageFont

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
