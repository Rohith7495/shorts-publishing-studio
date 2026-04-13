from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.schemas import (
    GenerationResponse,
    YouTubeAuthStatus,
    YouTubePublishRequest,
    YouTubePublishResponse,
)
from app.services.pipeline import VideoGenerationPipeline
from app.services.video import VideoProcessingService, VideoProcessingServiceError
from app.services.vision import GeminiVisionService, GeminiVisionServiceError
from app.services.youtube import YouTubeOAuthService, YouTubeServiceError, YouTubeUploadService

settings = get_settings()

app = FastAPI(title=settings.app_name)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

video_service = VideoProcessingService(
    upload_dir=settings.video_upload_dir,
    frame_sample_seconds=settings.frame_sample_seconds,
    upload_session_ttl_seconds=settings.upload_session_ttl_seconds,
)
youtube_oauth_service = YouTubeOAuthService(
    session_dir=settings.oauth_session_dir,
    client_id=settings.google_client_id,
    client_secret=settings.google_client_secret,
    redirect_uri=settings.google_redirect_uri,
    frontend_base_url=settings.frontend_base_url,
    session_ttl_seconds=settings.oauth_session_ttl_seconds,
)
youtube_upload_service = YouTubeUploadService(category_id=settings.youtube_category_id)

pipeline = VideoGenerationPipeline(
    video_service=video_service,
    vision_service=GeminiVisionService(
        api_key=settings.gemini_api_key,
        model_name=settings.gemini_vision_model,
    ),
    max_title_count=settings.max_title_count,
    max_hashtag_count=settings.max_hashtag_count,
)


@app.on_event("startup")
def cleanup_temp_state() -> None:
    video_service.cleanup_stale_upload_sessions()
    youtube_oauth_service.cleanup_stale_sessions()


@app.get("/health")
def healthcheck() -> dict[str, object]:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "gemini_configured": bool(settings.gemini_api_key),
        "vision_model": settings.gemini_vision_model,
        "youtube_oauth_configured": bool(settings.google_client_id and settings.google_client_secret),
    }


@app.post("/api/generate", response_model=GenerationResponse)
async def generate_from_video(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
) -> GenerationResponse:
    browser_session_id = _get_or_create_browser_session_id(request)
    _set_browser_session_cookie(request, response, browser_session_id)
    try:
        return await pipeline.run(upload=file, browser_session_id=browser_session_id)
    except GeminiVisionServiceError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Unexpected backend error: {error}") from error


@app.delete("/api/uploads/{upload_session_id}")
def discard_upload(upload_session_id: str, request: Request) -> dict[str, object]:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    upload_session = video_service.load_upload_session(upload_session_id)
    if upload_session is None:
        return {"deleted": False}
    if browser_session_id != upload_session.browser_session_id:
        raise HTTPException(status_code=403, detail="This temporary upload belongs to a different browser session.")

    deleted = video_service.delete_upload_session(upload_session_id)
    return {"deleted": deleted}


@app.get("/api/auth/youtube/start")
def start_youtube_auth(request: Request, return_to: Optional[str] = None) -> RedirectResponse:
    browser_session_id = _get_or_create_browser_session_id(request)
    auth_url = youtube_oauth_service.build_authorization_url(browser_session_id, return_to)
    response = RedirectResponse(url=auth_url, status_code=302)
    _set_browser_session_cookie(request, response, browser_session_id)
    return response


@app.get("/api/auth/youtube/callback")
def youtube_auth_callback(
    request: Request,
    state: Optional[str] = None,
    code: Optional[str] = None,
    error: Optional[str] = None,
) -> RedirectResponse:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    redirect_target = settings.frontend_base_url

    if error:
        return RedirectResponse(
            url=_append_query_params(redirect_target, {"youtube": "error", "message": error}),
            status_code=302,
        )

    if not browser_session_id or not state or not code:
        return RedirectResponse(
            url=_append_query_params(
                redirect_target,
                {"youtube": "error", "message": "Google sign-in session was incomplete."},
            ),
            status_code=302,
        )

    try:
        return_to = youtube_oauth_service.complete_authorization(
            browser_session_id=browser_session_id,
            state=state,
            code=code,
        )
        return RedirectResponse(
            url=_append_query_params(return_to, {"youtube": "connected"}),
            status_code=302,
        )
    except YouTubeServiceError as error_response:
        return RedirectResponse(
            url=_append_query_params(
                redirect_target,
                {"youtube": "error", "message": str(error_response)},
            ),
            status_code=302,
        )


@app.get("/api/auth/youtube/status", response_model=YouTubeAuthStatus)
def youtube_auth_status(request: Request) -> YouTubeAuthStatus:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    try:
        status = youtube_oauth_service.get_auth_status(browser_session_id)
    except YouTubeServiceError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    return YouTubeAuthStatus(**status)


@app.post("/api/auth/youtube/disconnect")
def disconnect_youtube(request: Request) -> dict[str, object]:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    disconnected = youtube_oauth_service.disconnect(browser_session_id)
    return {"disconnected": disconnected}


@app.post("/api/youtube/publish", response_model=YouTubePublishResponse)
def publish_to_youtube(
    payload: YouTubePublishRequest,
    request: Request,
) -> YouTubePublishResponse:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    if not browser_session_id:
        raise HTTPException(status_code=401, detail="Connect your YouTube account before publishing.")

    upload_session = video_service.load_upload_session(payload.upload_session_id)
    if upload_session is None:
        raise HTTPException(
            status_code=404,
            detail="The temporary upload session was not found or has expired. Generate the package again before publishing.",
        )
    if upload_session.browser_session_id != browser_session_id:
        raise HTTPException(status_code=403, detail="This temporary upload belongs to a different browser session.")

    try:
        credentials = youtube_oauth_service.get_credentials(browser_session_id)
        if credentials is None:
            raise HTTPException(status_code=401, detail="Connect your YouTube account before publishing.")

        prepared_video_path, _enhancement_notes, applied_enhancements = video_service.prepare_publish_video(
            upload_session=upload_session,
            enhancements=payload.enhancements,
        )

        publish_result = youtube_upload_service.upload_video(
            credentials=credentials,
            video_path=prepared_video_path,
            title=payload.title,
            description=payload.description,
            tags=payload.tags,
            privacy_status=payload.privacy_status,
        )
    except YouTubeServiceError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error
    except VideoProcessingServiceError as error:
        raise HTTPException(status_code=500, detail=str(error)) from error

    deleted_local_upload = video_service.delete_upload_session(payload.upload_session_id)
    return YouTubePublishResponse(
        video_id=publish_result["video_id"],
        video_url=publish_result["video_url"],
        studio_url=publish_result["studio_url"],
        privacy_status=payload.privacy_status,
        deleted_local_upload=deleted_local_upload,
        applied_enhancements=applied_enhancements,
    )


def _get_or_create_browser_session_id(request: Request) -> str:
    existing = request.cookies.get(settings.browser_session_cookie_name)
    if existing:
        return existing
    return youtube_oauth_service.new_browser_session_id()


def _set_browser_session_cookie(request: Request, response: Response, browser_session_id: str) -> None:
    if request.cookies.get(settings.browser_session_cookie_name) == browser_session_id:
        return

    response.set_cookie(
        key=settings.browser_session_cookie_name,
        value=browser_session_id,
        httponly=True,
        samesite=settings.browser_session_cookie_samesite,
        secure=settings.browser_session_cookie_secure,
        max_age=settings.oauth_session_ttl_seconds,
        path="/",
    )


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))
