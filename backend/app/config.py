from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Optional, Union

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Shorts Publishing Studio API"
    app_env: str = "development"
    video_upload_dir: Path = Path("storage/uploads")
    oauth_session_dir: Path = Path("storage/oauth_sessions")
    pending_comment_dir: Path = Path("storage/pending_comments")
    frame_sample_seconds: int = Field(default=3, ge=1, le=30)
    upload_session_ttl_seconds: int = Field(default=3600, ge=300, le=86400)
    oauth_session_ttl_seconds: int = Field(default=2592000, ge=3600, le=7776000)
    pending_comment_poll_seconds: int = Field(default=30, ge=10, le=3600)
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    max_title_count: int = Field(default=2, ge=1, le=10)
    max_hashtag_count: int = Field(default=15, ge=3, le=30)
    frontend_base_url: str = "http://localhost:3000"
    browser_session_cookie_name: str = "shorts_studio_session"
    browser_session_cookie_samesite: str = "lax"
    browser_session_cookie_secure: bool = False
    gemini_api_key: Optional[str] = None
    gemini_vision_model: str = "gemini-2.5-flash-lite"
    gemini_fallback_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    google_client_id: Optional[str] = None
    google_client_secret: Optional[str] = None
    google_redirect_uri: str = "http://localhost:8000/api/auth/youtube/callback"
    youtube_category_id: str = "22"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: Union[str, list[str]]) -> list[str]:
        parsed = cls._parse_string_list(value)
        if not parsed:
            return ["http://localhost:3000"]
        return parsed

    @field_validator("gemini_vision_model")
    @classmethod
    def validate_gemini_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("GEMINI_VISION_MODEL must not be empty")
        return normalized

    @field_validator("gemini_fallback_models", mode="before")
    @classmethod
    def parse_gemini_fallback_models(cls, value: Union[str, list[str], None]) -> list[str]:
        normalized: list[str] = []
        for item in cls._parse_string_list(value):
            if item not in normalized:
                normalized.append(item)
        return normalized

    @field_validator("browser_session_cookie_samesite")
    @classmethod
    def validate_cookie_samesite(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"lax", "strict", "none"}:
            raise ValueError("BROWSER_SESSION_COOKIE_SAMESITE must be one of: lax, strict, none")
        return normalized

    @staticmethod
    def _parse_string_list(value: Union[str, list[str], None]) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]

        normalized = value.strip()
        if not normalized:
            return []

        if normalized.startswith("["):
            try:
                parsed_json = json.loads(normalized)
            except json.JSONDecodeError:
                parsed_json = None

            if isinstance(parsed_json, list):
                return [str(item).strip() for item in parsed_json if str(item).strip()]

        return [item.strip() for item in normalized.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.video_upload_dir.mkdir(parents=True, exist_ok=True)
    settings.oauth_session_dir.mkdir(parents=True, exist_ok=True)
    settings.pending_comment_dir.mkdir(parents=True, exist_ok=True)
    return settings
