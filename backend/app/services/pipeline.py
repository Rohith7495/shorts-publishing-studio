from __future__ import annotations

from fastapi import UploadFile

from app.schemas import GenerationResponse
from app.services.video import VideoProcessingService
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
        metadata, metadata_notes = self.video_service.build_metadata(
            video_path=stored_upload.video_path,
            original_name=stored_upload.original_filename,
            mime_type=stored_upload.mime_type,
        )
        timestamps = self.video_service.sample_timestamps(metadata)
        frame_samples, frame_notes = self.video_service.extract_frames(stored_upload.video_path, timestamps)
        analysis, analysis_notes = self.vision_service.analyze_frames(
            frame_samples=frame_samples,
            max_titles=self.max_title_count,
            max_hashtags=self.max_hashtag_count,
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
