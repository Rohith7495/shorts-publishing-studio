from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


class VideoMetadata(BaseModel):
    filename: str
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    duration_seconds: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    fps: Optional[float] = None


class FrameSample(BaseModel):
    timestamp_seconds: float
    image_path: str


class FrameInsight(BaseModel):
    timestamp_seconds: float
    summary: str
    tags: list[str] = Field(default_factory=list)


class DetectedObject(BaseModel):
    label: str
    count: int


class HookTitleCandidate(BaseModel):
    text: str
    score: float


class DescriptionCandidate(BaseModel):
    text: str
    angle: str


class VisionModelOutput(BaseModel):
    category: str
    visual_basis: str
    hook_titles: list[HookTitleCandidate]
    descriptions: list[DescriptionCandidate]
    hashtags: list[str]
    first_comment_text: str
    detected_objects: list[DetectedObject]
    frame_insights: list[FrameInsight]


class GenerationResponse(VisionModelOutput):
    upload_session_id: str
    upload_expires_at: str
    metadata: VideoMetadata
    processing_notes: list[str]


class YouTubeAuthStatus(BaseModel):
    connected: bool
    channel_title: Optional[str] = None
    channel_id: Optional[str] = None


class GenerationJobStartResponse(BaseModel):
    job_id: str
    state: Literal["queued", "running", "succeeded", "failed"]


class GenerationJobStatusResponse(BaseModel):
    job_id: str
    state: Literal["queued", "running", "succeeded", "failed"]
    stage: str
    detail: Optional[str] = None
    progress_percent: Optional[float] = None
    elapsed_ms: int
    result: Optional[GenerationResponse] = None
    error: Optional[str] = None


class VideoEnhancementOptions(BaseModel):
    visual_pop: bool = False
    audio_cleanup: bool = False


class YouTubePublishRequest(BaseModel):
    upload_session_id: str
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(default_factory=list)
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    publish_at: Optional[datetime] = None
    post_first_comment: bool = False
    first_comment_text: Optional[str] = Field(default=None, max_length=1000)
    enhancements: VideoEnhancementOptions = Field(default_factory=VideoEnhancementOptions)

    @field_validator("publish_at")
    @classmethod
    def normalize_publish_at(cls, value: Optional[datetime]) -> Optional[datetime]:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("publish_at must include a timezone.")
        return value.astimezone(timezone.utc)

    @model_validator(mode="after")
    def validate_schedule_rules(self) -> "YouTubePublishRequest":
        if self.publish_at is None:
            return self
        if self.privacy_status != "private":
            raise ValueError("Scheduled YouTube uploads must stay private until the publish time.")
        if self.publish_at <= datetime.now(timezone.utc):
            raise ValueError("publish_at must be in the future.")
        return self

    @model_validator(mode="after")
    def validate_optional_publish_features(self) -> "YouTubePublishRequest":
        if self.first_comment_text is not None:
            cleaned_comment = self.first_comment_text.strip()
            self.first_comment_text = cleaned_comment or None

        if self.post_first_comment and not self.first_comment_text:
            raise ValueError("Enter a first comment before enabling automatic first-comment posting.")

        return self


class YouTubePublishResponse(BaseModel):
    video_id: str
    video_url: str
    studio_url: str
    privacy_status: Literal["private", "unlisted", "public"]
    publish_at: Optional[datetime] = None
    first_comment_posted: bool = False
    first_comment_queued: bool = False
    first_comment_id: Optional[str] = None
    deleted_local_upload: bool
    applied_enhancements: list[str] = Field(default_factory=list)
    publish_notes: list[str] = Field(default_factory=list)


class YouTubePublishJobStartResponse(BaseModel):
    job_id: str
    state: Literal["queued", "running", "succeeded", "failed"]


class YouTubePublishJobStatusResponse(BaseModel):
    job_id: str
    state: Literal["queued", "running", "succeeded", "failed"]
    stage: str
    detail: Optional[str] = None
    progress_percent: Optional[float] = None
    uploaded_bytes: Optional[int] = None
    total_bytes: Optional[int] = None
    remaining_seconds: Optional[float] = None
    elapsed_ms: int
    result: Optional[YouTubePublishResponse] = None
    error: Optional[str] = None
