from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


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


class VideoEnhancementOptions(BaseModel):
    visual_pop: bool = False
    audio_cleanup: bool = False


class YouTubePublishRequest(BaseModel):
    upload_session_id: str
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    tags: list[str] = Field(default_factory=list)
    privacy_status: Literal["private", "unlisted", "public"] = "private"
    enhancements: VideoEnhancementOptions = Field(default_factory=VideoEnhancementOptions)


class YouTubePublishResponse(BaseModel):
    video_id: str
    video_url: str
    studio_url: str
    privacy_status: Literal["private", "unlisted", "public"]
    deleted_local_upload: bool
    applied_enhancements: list[str] = Field(default_factory=list)
