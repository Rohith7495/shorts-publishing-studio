from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse


YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_FORCE_SSL_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"


class YouTubeServiceError(RuntimeError):
    """Raised when Google OAuth or YouTube upload operations fail."""


class YouTubeOAuthService:
    def __init__(
        self,
        session_dir: Path,
        client_id: Optional[str],
        client_secret: Optional[str],
        redirect_uri: str,
        frontend_base_url: str,
        session_ttl_seconds: int,
    ) -> None:
        self.session_dir = session_dir
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.frontend_base_url = frontend_base_url
        self.session_ttl_seconds = session_ttl_seconds
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def build_authorization_url(self, browser_session_id: str, return_to: Optional[str]) -> str:
        flow = self._build_flow()
        auth_url, state = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
        )
        session_payload = self._read_session(browser_session_id)
        session_payload["oauth_state"] = state
        session_payload["code_verifier"] = getattr(flow, "code_verifier", None)
        session_payload["return_to"] = self._sanitize_return_to(return_to)
        session_payload["updated_at"] = self._now_iso()
        self._write_session(browser_session_id, session_payload)
        return auth_url

    def complete_authorization(self, browser_session_id: str, state: str, code: str) -> str:
        session_payload = self._read_session(browser_session_id)
        expected_state = session_payload.get("oauth_state")
        if not expected_state or expected_state != state:
            raise YouTubeServiceError("Google OAuth state did not match this browser session.")
        code_verifier = session_payload.get("code_verifier")
        if not code_verifier:
            raise YouTubeServiceError(
                "Google OAuth session expired or lost its PKCE verifier. Start the YouTube connection again."
            )

        flow = self._build_flow(state=state, code_verifier=code_verifier)
        try:
            flow.fetch_token(code=code)
        except Exception as error:
            raise YouTubeServiceError(f"Google OAuth token exchange failed: {error}") from error

        session_payload["oauth_state"] = None
        session_payload["code_verifier"] = None
        session_payload["credentials"] = json.loads(flow.credentials.to_json())
        session_payload["updated_at"] = self._now_iso()
        self._write_session(browser_session_id, session_payload)
        return str(session_payload.get("return_to") or self.frontend_base_url)

    def get_auth_status(self, browser_session_id: Optional[str]) -> dict[str, Optional[str] | bool]:
        if not browser_session_id:
            return {"connected": False, "channel_title": None, "channel_id": None}

        credentials = self.get_credentials(browser_session_id)
        if credentials is None:
            return {"connected": False, "channel_title": None, "channel_id": None}

        build = self._import_google_api_build()
        try:
            youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
            payload = youtube.channels().list(part="snippet", mine=True).execute()
            items = payload.get("items") or []
            if not items:
                return {"connected": True, "channel_title": None, "channel_id": None}

            first_item = items[0]
            snippet = first_item.get("snippet") or {}
            return {
                "connected": True,
                "channel_title": snippet.get("title"),
                "channel_id": first_item.get("id"),
            }
        except Exception:
            return {"connected": True, "channel_title": None, "channel_id": None}

    def get_credentials(self, browser_session_id: str) -> Optional[Any]:
        session_payload = self._read_session(browser_session_id)
        credentials_payload = session_payload.get("credentials")
        if not credentials_payload:
            return None

        credentials_module, google_request = self._import_google_credentials_modules()
        try:
            credentials = credentials_module.from_authorized_user_info(
                credentials_payload,
                scopes=[YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE, YOUTUBE_FORCE_SSL_SCOPE],
            )
        except Exception as error:
            raise YouTubeServiceError(f"Stored Google credentials could not be restored: {error}") from error

        if credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(google_request())
            except Exception as error:
                raise YouTubeServiceError(f"Google access token refresh failed: {error}") from error
            session_payload["credentials"] = json.loads(credentials.to_json())
            session_payload["updated_at"] = self._now_iso()
            self._write_session(browser_session_id, session_payload)

        return credentials

    def disconnect(self, browser_session_id: Optional[str]) -> bool:
        if not browser_session_id:
            return False
        session_path = self._session_path(browser_session_id)
        if not session_path.exists():
            return False
        session_path.unlink(missing_ok=True)
        return True

    def cleanup_stale_sessions(self) -> int:
        removed = 0
        now = datetime.now(timezone.utc)

        for session_path in self.session_dir.glob("*.json"):
            try:
                payload = json.loads(session_path.read_text(encoding="utf-8"))
                updated_at = datetime.fromisoformat(payload["updated_at"])
            except (KeyError, ValueError, TypeError, json.JSONDecodeError):
                session_path.unlink(missing_ok=True)
                removed += 1
                continue

            age_seconds = (now - updated_at).total_seconds()
            if age_seconds > self.session_ttl_seconds:
                session_path.unlink(missing_ok=True)
                removed += 1

        return removed

    def _build_flow(self, state: Optional[str] = None, code_verifier: Optional[str] = None) -> Any:
        if not self.client_id or not self.client_secret:
            raise YouTubeServiceError(
                "Google OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in the backend environment."
            )

        flow_module = self._import_google_flow()
        flow = flow_module.from_client_config(
            {
                "web": {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            scopes=[YOUTUBE_UPLOAD_SCOPE, YOUTUBE_READONLY_SCOPE, YOUTUBE_FORCE_SSL_SCOPE],
            state=state,
            code_verifier=code_verifier,
        )
        flow.redirect_uri = self.redirect_uri
        return flow

    def _sanitize_return_to(self, return_to: Optional[str]) -> str:
        if not return_to:
            return self.frontend_base_url

        frontend = urlparse(self.frontend_base_url)
        candidate = urlparse(return_to)
        if candidate.scheme != frontend.scheme or candidate.netloc != frontend.netloc:
            return self.frontend_base_url

        return return_to

    def _read_session(self, browser_session_id: str) -> dict[str, Any]:
        session_path = self._session_path(browser_session_id)
        if not session_path.exists():
            return {"browser_session_id": browser_session_id, "updated_at": self._now_iso()}

        try:
            return json.loads(session_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"browser_session_id": browser_session_id, "updated_at": self._now_iso()}

    def _write_session(self, browser_session_id: str, payload: dict[str, Any]) -> None:
        payload["browser_session_id"] = browser_session_id
        payload["updated_at"] = self._now_iso()
        self._session_path(browser_session_id).write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

    def _session_path(self, browser_session_id: str) -> Path:
        safe_name = browser_session_id.replace("/", "").replace("\\", "")
        return self.session_dir / f"{safe_name}.json"

    @staticmethod
    def new_browser_session_id() -> str:
        return uuid.uuid4().hex

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _import_google_flow() -> Any:
        try:
            from google_auth_oauthlib.flow import Flow  # type: ignore
        except ImportError as error:
            raise YouTubeServiceError(
                "Google OAuth dependencies are missing. Run `pip install -r requirements.txt` in the backend virtual environment."
            ) from error
        return Flow

    @staticmethod
    def _import_google_credentials_modules() -> tuple[Any, Any]:
        try:
            from google.auth.transport.requests import Request as GoogleRequest  # type: ignore
            from google.oauth2.credentials import Credentials  # type: ignore
        except ImportError as error:
            raise YouTubeServiceError(
                "Google authentication dependencies are missing. Run `pip install -r requirements.txt` in the backend virtual environment."
            ) from error
        return Credentials, GoogleRequest

    @staticmethod
    def _import_google_api_build() -> Any:
        try:
            from googleapiclient.discovery import build  # type: ignore
        except ImportError as error:
            raise YouTubeServiceError(
                "The YouTube API client is missing. Run `pip install -r requirements.txt` in the backend virtual environment."
            ) from error
        return build


class YouTubeUploadService:
    def __init__(self, category_id: str) -> None:
        self.category_id = category_id

    def upload_video(
        self,
        credentials: Any,
        video_path: Path,
        title: str,
        description: str,
        tags: list[str],
        privacy_status: str,
        publish_at: Optional[datetime] = None,
    ) -> dict[str, str]:
        build, media_file_upload = self._import_youtube_client_modules()
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        normalized_tags = self._normalize_tags(tags)

        body = {
            "snippet": {
                "title": title.strip(),
                "description": description.strip(),
                "tags": normalized_tags or None,
                "categoryId": self.category_id,
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        if publish_at is not None:
            scheduled_publish_at = publish_at.astimezone(timezone.utc).replace(microsecond=0)
            body["status"]["publishAt"] = scheduled_publish_at.isoformat().replace("+00:00", "Z")

        insert_request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media_file_upload(str(video_path), chunksize=-1, resumable=True),
        )

        response = None
        try:
            while response is None:
                _, response = insert_request.next_chunk()
        except Exception as error:
            raise YouTubeServiceError(f"YouTube upload failed: {error}") from error

        video_id = response.get("id")
        if not video_id:
            raise YouTubeServiceError("YouTube did not return a video ID after upload.")

        return {
            "video_id": str(video_id),
            "video_url": f"https://www.youtube.com/watch?v={video_id}",
            "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
        }

    def upload_thumbnail(
        self,
        credentials: Any,
        video_id: str,
        thumbnail_path: Path,
    ) -> None:
        build, media_file_upload = self._import_youtube_client_modules()
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)

        try:
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=media_file_upload(str(thumbnail_path), mimetype="image/jpeg"),
            ).execute()
        except Exception as error:
            raise YouTubeServiceError(f"YouTube thumbnail upload failed: {error}") from error

    def post_first_comment(
        self,
        credentials: Any,
        video_id: str,
        text: str,
    ) -> str:
        build, _ = self._import_youtube_client_modules()
        youtube = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        channel_id = self._get_authenticated_channel_id(youtube)

        body = {
            "snippet": {
                "channelId": channel_id,
                "videoId": video_id,
                "topLevelComment": {
                    "snippet": {
                        "textOriginal": text.strip(),
                    }
                },
            }
        }

        try:
            response = youtube.commentThreads().insert(part="snippet", body=body).execute()
        except Exception as error:
            raise YouTubeServiceError(f"YouTube first-comment post failed: {error}") from error

        comment_id = (((response.get("snippet") or {}).get("topLevelComment") or {}).get("id"))
        if not comment_id:
            raise YouTubeServiceError("YouTube did not return a comment ID after posting the first comment.")
        return str(comment_id)

    @staticmethod
    def _normalize_tags(tags: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for tag in tags:
            cleaned = tag.strip()
            if not cleaned:
                continue
            cleaned = cleaned.lstrip("#").replace(" ", "")
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            normalized.append(cleaned)

        return normalized[:15]

    @staticmethod
    def _get_authenticated_channel_id(youtube: Any) -> str:
        try:
            payload = youtube.channels().list(part="id", mine=True).execute()
            items = payload.get("items") or []
            if not items or not items[0].get("id"):
                raise ValueError("No authenticated YouTube channel was returned.")
            return str(items[0]["id"])
        except Exception as error:
            raise YouTubeServiceError(f"Unable to determine the authenticated YouTube channel ID: {error}") from error

    @staticmethod
    def _import_youtube_client_modules() -> tuple[Any, Any]:
        try:
            from googleapiclient.discovery import build  # type: ignore
            from googleapiclient.http import MediaFileUpload  # type: ignore
        except ImportError as error:
            raise YouTubeServiceError(
                "The YouTube API client is missing. Run `pip install -r requirements.txt` in the backend virtual environment."
            ) from error
        return build, MediaFileUpload
