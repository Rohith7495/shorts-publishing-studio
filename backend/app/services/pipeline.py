from __future__ import annotations

from typing import Callable, Optional

from fastapi import UploadFile

from app.schemas import GenerationResponse
from app.services.video import StoredUploadSession, VideoProcessingService
from app.services.vision import GeminiVisionService


class VideoGenerationPipeline:
    def __init__(
        self,
        video_service: VideoProcessingService,
        vision_service: GeminiVisionService,
        max_title_count: int,
        max_hashtag_count: int,
    ) -> None:
        self.video_service = video_service
        self.vision_service = vision_service
        self.max_title_count = max_title_count
        self.max_hashtag_count = max_hashtag_count

    async def run(self, upload: UploadFile, browser_session_id: str) -> GenerationResponse:
        stored_upload = await self.video_service.save_upload(upload, browser_session_id)
        return self.run_stored_upload(stored_upload)

    def run_stored_upload(
        self,
        stored_upload: StoredUploadSession,
        stage_callback: Optional[Callable[[str, str, Optional[float]], None]] = None,
    ) -> GenerationResponse:
        self._emit_stage(
            stage_callback,
            stage="Processing video",
            detail="Reading the uploaded file and preparing the analysis workspace.",
            progress_percent=20.0,
        )
        metadata, metadata_notes = self.video_service.build_metadata(
            video_path=stored_upload.video_path,
            original_name=stored_upload.original_filename,
            mime_type=stored_upload.mime_type,
        )
        timestamps = self.video_service.sample_timestamps(metadata)

        self._emit_stage(
            stage_callback,
            stage="Extracting frames",
            detail="Sampling key frames and gathering metadata from the uploaded video.",
            progress_percent=45.0,
        )
        frame_samples, frame_notes = self.video_service.extract_frames(stored_upload.video_path, timestamps)

        self._emit_stage(
            stage_callback,
            stage="Asking Gemini",
            detail="Generating titles, descriptions, hashtags, and video insights from the sampled frames.",
            progress_percent=75.0,
        )
        analysis, analysis_notes = self.vision_service.analyze_frames(
            frame_samples=frame_samples,
            max_titles=self.max_title_count,
            max_hashtags=self.max_hashtag_count,
        )

        self._emit_stage(
            stage_callback,
            stage="Finalizing package",
            detail="Preparing the final YouTube package response for the app.",
            progress_percent=92.0,
        )
        return GenerationResponse(
            category=analysis.category,
            visual_basis=analysis.visual_basis,
            hook_titles=analysis.hook_titles,
            descriptions=analysis.descriptions,
            hashtags=analysis.hashtags,
            first_comment_text=analysis.first_comment_text,
            detected_objects=analysis.detected_objects,
            frame_insights=analysis.frame_insights,
            upload_session_id=stored_upload.upload_session_id,
            upload_expires_at=self.video_service.build_upload_expiry(stored_upload),
            metadata=metadata,
            processing_notes=[
                *metadata_notes,
                *frame_notes,
                *analysis_notes,
                "The uploaded video is stored only temporarily and will be deleted after a successful YouTube upload or session expiry.",
            ],
        )

    @staticmethod
    def _emit_stage(
        stage_callback: Optional[Callable[[str, str, Optional[float]], None]],
        *,
        stage: str,
        detail: str,
        progress_percent: Optional[float],
    ) -> None:
        if stage_callback is None:
            return
        stage_callback(stage, detail, progress_percent)
