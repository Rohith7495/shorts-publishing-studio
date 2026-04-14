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
    SHORTS_COVER_SIZE = (1080, 1920)
    SHORTS_COVER_INTRO_SECONDS = 1.0
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

    @staticmethod
    def uses_shorts_cover_preview(metadata: VideoMetadata) -> bool:
        if metadata.width is None or metadata.height is None:
            return False
        return metadata.height > metadata.width

    def supports_custom_thumbnail_upload(self, metadata: VideoMetadata) -> bool:
        return not self.uses_shorts_cover_preview(metadata)

    def build_upload_expiry(self, record: StoredUploadSession) -> str:
        return record.expires_at.isoformat()

    def get_thumbnail_frame_image_path(
        self,
        upload_session: StoredUploadSession,
        preferred_timestamp_seconds: Optional[float] = None,
    ) -> Path:
        frame_sample = self._select_thumbnail_frame(upload_session, preferred_timestamp_seconds)
        if frame_sample is None:
            raise VideoProcessingServiceError("No sampled frames are available to build a cover preview.")
        return Path(frame_sample.image_path)

    def render_thumbnail_preview(
        self,
        upload_session: StoredUploadSession,
        text: str,
        preferred_timestamp_seconds: Optional[float] = None,
        visual_basis: Optional[str] = None,
        frame_summary: Optional[str] = None,
    ) -> Path:
        source_image = self.get_thumbnail_frame_image_path(upload_session, preferred_timestamp_seconds)
        destination = upload_session.workspace_dir / "cover-preview.jpg"
        self._compose_cover_image(
            source_image=source_image,
            text=text,
            destination=destination,
            visual_basis=visual_basis,
            frame_summary=frame_summary,
        )
        return destination

    def prepare_publish_video(
        self,
        upload_session: StoredUploadSession,
        enhancements: VideoEnhancementOptions,
        metadata: VideoMetadata,
    ) -> tuple[Path, list[str], list[str]]:
        notes: list[str] = []
        applied: list[str] = []
        working_video_path = upload_session.video_path

        ffmpeg_path = self._find_ffmpeg()
        if (enhancements.visual_pop or enhancements.audio_cleanup) and not ffmpeg_path:
            raise VideoProcessingServiceError(
                "ffmpeg is required for video preparation before upload. Install ffmpeg on the backend machine and try again."
            )

        has_audio_stream = self._has_audio_stream(working_video_path)
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

        if video_filters or audio_filters:
            output_path = upload_session.workspace_dir / "publish-enhanced.mp4"
            command = [
                ffmpeg_path,
                "-y",
                "-i",
                str(working_video_path),
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

            working_video_path = output_path

        if not notes:
            notes.append("Publishing the original uploaded video without pre-upload enhancements.")

        return working_video_path, notes, applied

    def _prepend_shorts_cover_intro(
        self,
        upload_session: StoredUploadSession,
        source_video_path: Path,
        metadata: VideoMetadata,
        text: str,
        preferred_timestamp_seconds: Optional[float],
        ffmpeg_path: Optional[str],
        visual_basis: Optional[str] = None,
        frame_summary: Optional[str] = None,
    ) -> Path:
        if not ffmpeg_path:
            raise VideoProcessingServiceError(
                "ffmpeg is required to bake the Shorts cover into the first second of the upload."
            )

        cover_image_path = self.render_thumbnail_preview(
            upload_session=upload_session,
            text=text,
            preferred_timestamp_seconds=preferred_timestamp_seconds,
            visual_basis=visual_basis,
            frame_summary=frame_summary,
        )
        target_width, target_height = self._target_publish_size(metadata)
        fps = self._target_publish_fps(metadata.fps)
        scale_filter = (
            f"scale={target_width}:{target_height}:force_original_aspect_ratio=increase,"
            f"crop={target_width}:{target_height},setsar=1,format=yuv420p"
        )
        output_path = upload_session.workspace_dir / "publish-with-cover-intro.mp4"

        if self._has_audio_stream(source_video_path):
            filter_graph = (
                f"[0:v]fps={fps},{scale_filter}[coverv];"
                "[1:a]aformat=sample_rates=48000:channel_layouts=stereo[covera];"
                f"[2:v]fps={fps},{scale_filter}[mainv];"
                "[2:a]aformat=sample_rates=48000:channel_layouts=stereo[maina];"
                "[coverv][covera][mainv][maina]concat=n=2:v=1:a=1[outv][outa]"
            )
            command = [
                ffmpeg_path,
                "-y",
                "-loop",
                "1",
                "-framerate",
                fps,
                "-t",
                str(self.SHORTS_COVER_INTRO_SECONDS),
                "-i",
                str(cover_image_path),
                "-f",
                "lavfi",
                "-t",
                str(self.SHORTS_COVER_INTRO_SECONDS),
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-i",
                str(source_video_path),
                "-filter_complex",
                filter_graph,
                "-map",
                "[outv]",
                "-map",
                "[outa]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "2",
                str(output_path),
            ]
        else:
            filter_graph = (
                f"[0:v]fps={fps},{scale_filter}[coverv];"
                f"[1:v]fps={fps},{scale_filter}[mainv];"
                "[coverv][mainv]concat=n=2:v=1:a=0[outv]"
            )
            command = [
                ffmpeg_path,
                "-y",
                "-loop",
                "1",
                "-framerate",
                fps,
                "-t",
                str(self.SHORTS_COVER_INTRO_SECONDS),
                "-i",
                str(cover_image_path),
                "-i",
                str(source_video_path),
                "-filter_complex",
                filter_graph,
                "-map",
                "[outv]",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-an",
                "-movflags",
                "+faststart",
                "-map_metadata",
                "1",
                str(output_path),
            ]

        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output_path.exists():
            raise VideoProcessingServiceError(
                "ffmpeg failed while baking the Shorts cover into the first second of the upload: "
                f"{result.stderr.strip() or 'unknown ffmpeg error'}"
            )

        return output_path

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
        ffprobe_path = self._find_ffprobe()
        if not ffprobe_path:
            return None

        command = [
            ffprobe_path,
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

    def _compose_cover_image(
        self,
        source_image: Path,
        text: str,
        destination: Path,
        visual_basis: Optional[str] = None,
        frame_summary: Optional[str] = None,
    ) -> None:
        (
            image_module,
            image_draw_module,
            image_enhance_module,
            image_filter_module,
            image_font_module,
        ) = self._import_pillow_modules()

        with image_module.open(source_image) as raw_image:
            base_image = raw_image.convert("RGB")

        context_text = " ".join(
            part.strip() for part in [visual_basis or "", frame_summary or "", text] if part and part.strip()
        )
        theme = self._select_cover_theme(context_text)
        badge_text = self._build_cover_badge_text(visual_basis, frame_summary)
        use_shorts_cover = self._is_portrait_size(base_image.size)
        target_size = self.SHORTS_COVER_SIZE if use_shorts_cover else self.THUMBNAIL_SIZE
        canvas_image = self._crop_to_cover_canvas(image_module, base_image, target_size)
        canvas_image = self._apply_cover_grade(
            image_enhance_module=image_enhance_module,
            image_filter_module=image_filter_module,
            canvas_image=canvas_image,
            context_text=context_text,
            use_shorts_cover=use_shorts_cover,
        )
        canvas = canvas_image.convert("RGBA")
        overlay = image_module.new("RGBA", canvas.size, (0, 0, 0, 0))
        overlay = self._apply_cover_lighting(
            image_module=image_module,
            image_draw_module=image_draw_module,
            image_filter_module=image_filter_module,
            overlay=overlay,
            size=canvas.size,
            theme=theme,
            use_shorts_cover=use_shorts_cover,
        )
        overlay_draw = image_draw_module.Draw(overlay)
        width, height = canvas.size

        if use_shorts_cover:
            panel_box = (
                int(width * 0.06),
                int(height * 0.47),
                int(width * 0.94),
                int(height * 0.8),
            )
            accent_box = (
                int(width * 0.1),
                int(height * 0.51),
                int(width * 0.62),
                int(height * 0.535),
            )
            badge_box = (
                int(width * 0.08),
                int(height * 0.09),
                int(width * 0.46),
                int(height * 0.145),
            )
            panel_radius = 44
            initial_font_size = 108
            minimum_font_size = 68
            max_text_width = int(width * 0.68)
            line_gap = 18
            text_center_y = int(height * 0.66)
            stroke_width = 6
            badge_font_size = 42
        else:
            panel_box = (
                int(width * 0.2),
                int(height * 0.54),
                int(width * 0.8),
                int(height * 0.9),
            )
            accent_box = (
                int(width * 0.24),
                int(height * 0.58),
                int(width * 0.6),
                int(height * 0.61),
            )
            badge_box = (
                int(width * 0.07),
                int(height * 0.08),
                int(width * 0.3),
                int(height * 0.17),
            )
            panel_radius = 36
            initial_font_size = 88
            minimum_font_size = 56
            max_text_width = int(width * 0.5)
            line_gap = 12
            text_center_y = int(height * 0.72)
            stroke_width = 5
            badge_font_size = 28

        overlay_draw.rounded_rectangle(panel_box, radius=panel_radius, fill=theme["panel_fill"])
        overlay_draw.rounded_rectangle(accent_box, radius=14, fill=theme["accent_fill"])
        if badge_text:
            overlay_draw.rounded_rectangle(badge_box, radius=18, fill=theme["badge_fill"])

        composed = image_module.alpha_composite(canvas, overlay)
        draw = image_draw_module.Draw(composed)
        accent_color = theme["text_fill"]
        font = self._load_thumbnail_font(image_font_module, size=initial_font_size)
        badge_font = self._load_thumbnail_font(image_font_module, size=badge_font_size)
        wrapped_lines = self._wrap_thumbnail_text(
            draw=draw,
            text=text,
            font=font,
            max_width=max_text_width,
            stroke_width=stroke_width,
        )

        current_font_size = getattr(font, "size", initial_font_size)
        while len(wrapped_lines) > 3 and current_font_size > minimum_font_size:
            current_font_size -= 6
            font = self._load_thumbnail_font(image_font_module, size=current_font_size)
            wrapped_lines = self._wrap_thumbnail_text(
                draw=draw,
                text=text,
                font=font,
                max_width=max_text_width,
                stroke_width=stroke_width,
            )

        line_heights = []
        for line in wrapped_lines:
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
            line_heights.append(max(1, bbox[3] - bbox[1]))
        total_height = sum(line_heights) + line_gap * max(0, len(wrapped_lines) - 1)
        current_y = int(text_center_y - total_height / 2)
        center_x = int(width * 0.5)

        if badge_text:
            badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
            badge_width = max(1, badge_bbox[2] - badge_bbox[0])
            badge_height = max(1, badge_bbox[3] - badge_bbox[1])
            badge_x = badge_box[0] + max(16, int((badge_box[2] - badge_box[0] - badge_width) / 2))
            badge_y = badge_box[1] + max(10, int((badge_box[3] - badge_box[1] - badge_height) / 2))
            draw.text(
                (badge_x, badge_y),
                badge_text,
                font=badge_font,
                fill=theme["badge_text_fill"],
            )

        for index, line in enumerate(wrapped_lines):
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=stroke_width)
            line_width = max(1, bbox[2] - bbox[0])
            x_position = center_x - int(line_width / 2)
            draw.text(
                (x_position, current_y),
                line,
                font=font,
                fill=accent_color,
                stroke_width=stroke_width,
                stroke_fill=theme["stroke_fill"],
            )
            current_y += line_heights[index] + line_gap

        destination.parent.mkdir(parents=True, exist_ok=True)
        composed.convert("RGB").save(destination, format="JPEG", quality=92, optimize=True)

    def _apply_cover_grade(
        self,
        image_enhance_module: Any,
        image_filter_module: Any,
        canvas_image: Any,
        context_text: str,
        use_shorts_cover: bool,
    ) -> Any:
        lower_context = context_text.lower()
        contrast = 1.22 if any(keyword in lower_context for keyword in ("tesla", "car", "drive", "tech", "screen")) else 1.16
        color = 1.2 if "night" in lower_context else 1.14
        brightness = 1.03 if use_shorts_cover else 1.02
        sharpness = 1.3 if use_shorts_cover else 1.22

        enhanced = image_enhance_module.Contrast(canvas_image).enhance(contrast)
        enhanced = image_enhance_module.Color(enhanced).enhance(color)
        enhanced = image_enhance_module.Brightness(enhanced).enhance(brightness)
        enhanced = image_enhance_module.Sharpness(enhanced).enhance(sharpness)
        return enhanced.filter(image_filter_module.UnsharpMask(radius=2, percent=135, threshold=2))

    def _apply_cover_lighting(
        self,
        image_module: Any,
        image_draw_module: Any,
        image_filter_module: Any,
        overlay: Any,
        size: tuple[int, int],
        theme: dict[str, tuple[int, ...]],
        use_shorts_cover: bool,
    ) -> Any:
        width, height = size
        glow_overlay = image_module.new("RGBA", size, (0, 0, 0, 0))
        glow_draw = image_draw_module.Draw(glow_overlay)
        primary_glow = (
            int(width * -0.08),
            int(height * (0.02 if use_shorts_cover else 0.08)),
            int(width * 0.66),
            int(height * 0.5),
        )
        secondary_glow = (
            int(width * 0.5),
            int(height * 0.02),
            int(width * 1.06),
            int(height * (0.34 if use_shorts_cover else 0.42)),
        )
        glow_draw.ellipse(primary_glow, fill=theme["glow_primary"])
        glow_draw.ellipse(secondary_glow, fill=theme["glow_secondary"])
        glow_overlay = glow_overlay.filter(image_filter_module.GaussianBlur(radius=max(28, width // 9)))
        overlay = image_module.alpha_composite(overlay, glow_overlay)

        top_gradient = self._build_vertical_gradient(
            image_module=image_module,
            size=size,
            color=theme["top_tint"],
            start_alpha=110,
            end_alpha=0,
            from_top=True,
        )
        bottom_gradient = self._build_vertical_gradient(
            image_module=image_module,
            size=size,
            color=theme["bottom_shade"],
            start_alpha=210 if use_shorts_cover else 195,
            end_alpha=0,
            from_top=False,
        )
        overlay = image_module.alpha_composite(overlay, top_gradient)
        overlay = image_module.alpha_composite(overlay, bottom_gradient)
        return overlay

    def _build_vertical_gradient(
        self,
        image_module: Any,
        size: tuple[int, int],
        color: tuple[int, int, int],
        start_alpha: int,
        end_alpha: int,
        from_top: bool,
    ) -> Any:
        width, height = size
        mask = image_module.new("L", (1, height))
        pixels = mask.load()

        for y in range(height):
            progress = y / max(1, height - 1)
            if not from_top:
                progress = 1 - progress
            alpha = int(start_alpha + (end_alpha - start_alpha) * progress)
            pixels[0, y] = max(0, min(255, alpha))

        mask = mask.resize((width, height))
        gradient = image_module.new("RGBA", size, (*color, 0))
        gradient.putalpha(mask)
        return gradient

    def _select_cover_theme(self, context_text: str) -> dict[str, tuple[int, ...]]:
        lower_context = context_text.lower()

        if any(keyword in lower_context for keyword in ("tesla", "car", "drive", "driving", "navigation", "traffic", "dashboard", "screen", "tech", "technology", "ev")):
            return {
                "panel_fill": (8, 14, 20, 182),
                "accent_fill": (255, 120, 44, 235),
                "badge_fill": (29, 197, 224, 228),
                "text_fill": (255, 248, 238),
                "badge_text_fill": (7, 24, 31),
                "stroke_fill": (14, 18, 24),
                "glow_primary": (255, 120, 44, 142),
                "glow_secondary": (29, 197, 224, 108),
                "top_tint": (18, 31, 44),
                "bottom_shade": (4, 6, 11),
            }

        if any(keyword in lower_context for keyword in ("food", "kitchen", "recipe", "restaurant", "cook", "drink")):
            return {
                "panel_fill": (24, 12, 10, 178),
                "accent_fill": (255, 91, 52, 235),
                "badge_fill": (255, 196, 74, 224),
                "text_fill": (255, 248, 236),
                "badge_text_fill": (56, 23, 5),
                "stroke_fill": (26, 11, 8),
                "glow_primary": (255, 91, 52, 148),
                "glow_secondary": (255, 196, 74, 110),
                "top_tint": (46, 19, 10),
                "bottom_shade": (14, 7, 5),
            }

        if any(keyword in lower_context for keyword in ("travel", "nature", "outdoor", "beach", "mountain", "lifestyle", "street", "city")):
            return {
                "panel_fill": (12, 18, 14, 176),
                "accent_fill": (255, 170, 54, 235),
                "badge_fill": (132, 209, 84, 224),
                "text_fill": (255, 249, 240),
                "badge_text_fill": (12, 33, 16),
                "stroke_fill": (13, 18, 14),
                "glow_primary": (255, 170, 54, 142),
                "glow_secondary": (132, 209, 84, 108),
                "top_tint": (26, 40, 24),
                "bottom_shade": (6, 10, 8),
            }

        return {
            "panel_fill": (16, 14, 14, 182),
            "accent_fill": (255, 122, 58, 235),
            "badge_fill": (255, 216, 92, 225),
            "text_fill": (255, 248, 240),
            "badge_text_fill": (46, 29, 6),
            "stroke_fill": (18, 14, 12),
            "glow_primary": (255, 122, 58, 145),
            "glow_secondary": (255, 216, 92, 108),
            "top_tint": (40, 28, 18),
            "bottom_shade": (8, 7, 6),
        }

    def _build_cover_badge_text(self, visual_basis: Optional[str], frame_summary: Optional[str]) -> str:
        context = " ".join(part for part in [visual_basis or "", frame_summary or ""] if part).lower()

        keyword_badges = [
            (("tesla",), "TESLA POV"),
            (("self-driving", "autonomous"), "AUTO TEST"),
            (("navigation", "route", "traffic"), "LIVE ROUTE"),
            (("screen", "dashboard"), "ON SCREEN"),
            (("reaction", "surprise"), "REAL REACTION"),
            (("watch",), "WATCH CLOSE"),
        ]

        for keywords, badge in keyword_badges:
            if any(keyword in context for keyword in keywords):
                return badge

        words: list[str] = []
        for raw_word in context.upper().split():
            cleaned = "".join(character for character in raw_word if character.isalnum())
            if len(cleaned) < 4 or cleaned in {"THIS", "WITH", "FROM", "THERE", "WHILE", "ABOUT"}:
                continue
            if cleaned not in words:
                words.append(cleaned)
            if len(words) == 2:
                break

        if words:
            return " ".join(words)

        return "WATCH CLOSE"

    def _crop_to_cover_canvas(self, image_module: Any, source_image: Any, target_size: tuple[int, int]) -> Any:
        target_width, target_height = target_size
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
            if target_height > target_width:
                top_bias = 0.3
            else:
                top_bias = 0.38 if source_height > source_width else 0.5
            top = max(0, min(source_height - crop_height, int((source_height - crop_height) * top_bias)))
            left = 0
            right = source_width
            bottom = top + crop_height

        cropped = source_image.crop((left, top, right, bottom))
        return cropped.resize((target_width, target_height), image_module.Resampling.LANCZOS)

    @staticmethod
    def _is_portrait_size(size: tuple[int, int]) -> bool:
        width, height = size
        return height > width

    def _wrap_thumbnail_text(
        self,
        draw: Any,
        text: str,
        font: Any,
        max_width: int,
        stroke_width: int,
    ) -> list[str]:
        words = text.strip().split()
        if not words:
            return ["WATCH THIS"]

        lines: list[str] = []
        current = words[0]

        for word in words[1:]:
            candidate = f"{current} {word}"
            bbox = draw.textbbox((0, 0), candidate, font=font, stroke_width=stroke_width)
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

    def _target_publish_size(self, metadata: VideoMetadata) -> tuple[int, int]:
        fallback_width, fallback_height = self.SHORTS_COVER_SIZE
        width = metadata.width if metadata.width and metadata.width > 1 else fallback_width
        height = metadata.height if metadata.height and metadata.height > 1 else fallback_height
        return self._normalize_even_dimension(width, fallback_width), self._normalize_even_dimension(height, fallback_height)

    @staticmethod
    def _target_publish_fps(raw_fps: Optional[float]) -> str:
        fps = raw_fps if raw_fps and raw_fps > 0 else 30.0
        fps = max(24.0, min(60.0, round(float(fps), 2)))
        return f"{fps:g}"

    @staticmethod
    def _normalize_even_dimension(value: int, fallback: int) -> int:
        normalized = int(value) if value and value > 1 else fallback
        if normalized % 2 != 0:
            normalized -= 1
        return max(2, normalized)

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
        ffprobe_path = VideoProcessingService._find_ffprobe()
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
    def _find_ffmpeg() -> Optional[str]:
        return VideoProcessingService._find_binary(
            "ffmpeg",
            [
                "/opt/homebrew/bin/ffmpeg",
                "/usr/local/bin/ffmpeg",
                "/usr/bin/ffmpeg",
            ],
        )

    @staticmethod
    def _find_ffprobe() -> Optional[str]:
        return VideoProcessingService._find_binary(
            "ffprobe",
            [
                "/opt/homebrew/bin/ffprobe",
                "/usr/local/bin/ffprobe",
                "/usr/bin/ffprobe",
            ],
        )

    @staticmethod
    def _find_binary(binary_name: str, fallbacks: list[str]) -> Optional[str]:
        resolved = shutil.which(binary_name)
        if resolved:
            return resolved

        for candidate in fallbacks:
            if Path(candidate).exists():
                return candidate

        return None

    @staticmethod
    def _import_cv2() -> Optional[Any]:
        try:
            import cv2  # type: ignore
        except ImportError:
            return None
        return cv2

    @staticmethod
    def _import_pillow_modules() -> tuple[Any, Any, Any, Any, Any]:
        try:
            from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont  # type: ignore
        except ImportError as error:
            raise VideoProcessingServiceError(
                "Pillow is required for custom thumbnail generation. Install it with `pip install -r requirements.txt`."
            ) from error
        return Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

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
