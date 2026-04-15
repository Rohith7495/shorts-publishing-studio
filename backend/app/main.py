from __future__ import annotations

import logging
from threading import Thread
from time import monotonic
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.schemas import (
    GenerationJobStartResponse,
    GenerationJobStatusResponse,
    GenerationResponse,
    YouTubeAuthStatus,
    YouTubePublishJobStartResponse,
    YouTubePublishJobStatusResponse,
    YouTubePublishRequest,
    YouTubePublishResponse,
)
from app.services.pipeline import VideoGenerationPipeline
from app.services.publish_jobs import PublishJobStore
from app.services.video import VideoProcessingService, VideoProcessingServiceError
from app.services.vision import GeminiVisionService, GeminiVisionServiceError
from app.services.youtube import YouTubeOAuthService, YouTubeServiceError, YouTubeUploadService

settings = get_settings()
logger = logging.getLogger(__name__)

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
generation_job_store = PublishJobStore()
publish_job_store = PublishJobStore()

pipeline = VideoGenerationPipeline(
    video_service=video_service,
    vision_service=GeminiVisionService(
        api_key=settings.gemini_api_key,
        model_name=settings.gemini_vision_model,
        fallback_model_names=settings.gemini_fallback_models,
    ),
    max_title_count=settings.max_title_count,
    max_hashtag_count=settings.max_hashtag_count,
)


@app.on_event("startup")
def cleanup_temp_state() -> None:
    video_service.cleanup_stale_upload_sessions()
    youtube_oauth_service.cleanup_stale_sessions()
    generation_job_store.cleanup_stale_jobs()
    publish_job_store.cleanup_stale_jobs()


@app.get("/health")
def healthcheck() -> dict[str, object]:
    return {
        "status": "ok",
        "environment": settings.app_env,
        "gemini_configured": bool(settings.gemini_api_key),
        "vision_model": settings.gemini_vision_model,
        "vision_fallback_models": settings.gemini_fallback_models,
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


@app.post("/api/generate/start", response_model=GenerationJobStartResponse, status_code=202)
async def start_generate_from_video(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
) -> GenerationJobStartResponse:
    browser_session_id = _get_or_create_browser_session_id(request)
    _set_browser_session_cookie(request, response, browser_session_id)

    try:
        stored_upload = await video_service.save_upload(file, browser_session_id)
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"Unexpected backend error while saving the upload: {error}") from error

    job = generation_job_store.create_job(browser_session_id)
    generation_job_store.update_job(
        job.job_id,
        state="queued",
        stage="Queued",
        detail="The upload finished and frame analysis is about to start.",
        progress_percent=5.0,
    )
    Thread(
        target=_run_generation_job,
        args=(job.job_id, stored_upload.upload_session_id, browser_session_id),
        daemon=True,
    ).start()
    return GenerationJobStartResponse(job_id=job.job_id, state="queued")


@app.get("/api/generate/jobs/{job_id}", response_model=GenerationJobStatusResponse)
def get_generation_job_status(job_id: str, request: Request) -> GenerationJobStatusResponse:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    if not browser_session_id:
        raise HTTPException(status_code=401, detail="Start a generation job before checking its status.")

    job = generation_job_store.get_job(job_id, browser_session_id)
    if job is None:
        raise HTTPException(status_code=404, detail="The generation job was not found for this browser session.")

    serialized = generation_job_store.serialize_job(job)
    return GenerationJobStatusResponse(
        job_id=serialized["job_id"],
        state=serialized["state"],
        stage=serialized["stage"],
        detail=serialized["detail"],
        progress_percent=serialized["progress_percent"],
        elapsed_ms=serialized["elapsed_ms"],
        result=serialized["result"],
        error=serialized["error"],
    )


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

    return _run_publish_workflow(payload=payload, browser_session_id=browser_session_id)


@app.post("/api/youtube/publish/start", response_model=YouTubePublishJobStartResponse, status_code=202)
def start_publish_to_youtube(
    payload: YouTubePublishRequest,
    request: Request,
) -> YouTubePublishJobStartResponse:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    if not browser_session_id:
        raise HTTPException(status_code=401, detail="Connect your YouTube account before publishing.")

    _validate_publish_prerequisites(payload, browser_session_id)
    job = publish_job_store.create_job(browser_session_id)
    publish_job_store.update_job(
        job.job_id,
        state="queued",
        stage="Queued",
        detail="The YouTube publish job is queued and about to start.",
    )
    Thread(
        target=_run_publish_job,
        args=(job.job_id, payload, browser_session_id),
        daemon=True,
    ).start()
    return YouTubePublishJobStartResponse(job_id=job.job_id, state="queued")


@app.get("/api/youtube/publish/jobs/{job_id}", response_model=YouTubePublishJobStatusResponse)
def get_publish_job_status(job_id: str, request: Request) -> YouTubePublishJobStatusResponse:
    browser_session_id = request.cookies.get(settings.browser_session_cookie_name)
    if not browser_session_id:
        raise HTTPException(status_code=401, detail="Connect your YouTube account before checking publish status.")

    job = publish_job_store.get_job(job_id, browser_session_id)
    if job is None:
        raise HTTPException(status_code=404, detail="The publish job was not found for this browser session.")

    return YouTubePublishJobStatusResponse(**publish_job_store.serialize_job(job))


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


def _run_publish_job(job_id: str, payload: YouTubePublishRequest, browser_session_id: str) -> None:
    try:
        result = _run_publish_workflow(
            payload=payload,
            browser_session_id=browser_session_id,
            job_id=job_id,
        )
        publish_job_store.complete_job(job_id, result.model_dump(mode="json"))
    except HTTPException as error:
        message = error.detail if isinstance(error.detail, str) else "The YouTube upload failed."
        logger.exception("Background YouTube publish failed: %s", message)
        publish_job_store.fail_job(job_id, message)
    except Exception as error:
        logger.exception("Unexpected background YouTube publish failure: %s", error)
        publish_job_store.fail_job(job_id, str(error))


def _run_generation_job(job_id: str, upload_session_id: str, browser_session_id: str) -> None:
    upload_session = video_service.load_upload_session(upload_session_id)
    if upload_session is None:
        generation_job_store.fail_job(
            job_id,
            "The temporary upload session was lost before frame analysis could start. Generate the package again.",
            stage="Failed",
            detail="The uploaded file is no longer available on the backend.",
        )
        return

    try:
        generation_job_store.update_job(
            job_id,
            state="running",
            stage="Processing video",
            detail="Reading the uploaded file and preparing the analysis workspace.",
            progress_percent=20.0,
        )

        result = pipeline.run_stored_upload(
            upload_session,
            stage_callback=lambda stage, detail, progress_percent: generation_job_store.update_job(
                job_id,
                state="running",
                stage=stage,
                detail=detail,
                progress_percent=progress_percent,
            ),
        )
        generation_job_store.complete_job(
            job_id,
            result.model_dump(mode="json"),
            stage="Complete",
            detail="The YouTube package is ready to review and publish.",
        )
    except GeminiVisionServiceError as error:
        logger.exception("Background generation failed: %s", error)
        generation_job_store.fail_job(
            job_id,
            str(error),
            stage="Failed",
            detail="The AI generation step did not complete successfully.",
        )
        video_service.delete_upload_session(upload_session_id)
    except VideoProcessingServiceError as error:
        logger.exception("Background video processing failed: %s", error)
        generation_job_store.fail_job(
            job_id,
            str(error),
            stage="Failed",
            detail="The backend could not finish processing the uploaded video.",
        )
        video_service.delete_upload_session(upload_session_id)
    except Exception as error:
        logger.exception("Unexpected background generation failure: %s", error)
        generation_job_store.fail_job(
            job_id,
            f"Unexpected backend error: {error}",
            stage="Failed",
            detail="The generation workflow stopped unexpectedly.",
        )
        video_service.delete_upload_session(upload_session_id)


def _validate_publish_prerequisites(payload: YouTubePublishRequest, browser_session_id: str) -> None:
    upload_session = video_service.load_upload_session(payload.upload_session_id)
    if upload_session is None:
        raise HTTPException(
            status_code=404,
            detail="The temporary upload session was not found or has expired. Generate the package again before publishing.",
        )
    if upload_session.browser_session_id != browser_session_id:
        raise HTTPException(status_code=403, detail="This temporary upload belongs to a different browser session.")

    credentials = youtube_oauth_service.get_credentials(browser_session_id)
    if credentials is None:
        raise HTTPException(status_code=401, detail="Connect your YouTube account before publishing.")


def _run_publish_workflow(
    payload: YouTubePublishRequest,
    browser_session_id: str,
    job_id: Optional[str] = None,
) -> YouTubePublishResponse:
    upload_session = video_service.load_upload_session(payload.upload_session_id)
    if upload_session is None:
        raise HTTPException(
            status_code=404,
            detail="The temporary upload session was not found or has expired. Generate the package again before publishing.",
        )
    if upload_session.browser_session_id != browser_session_id:
        raise HTTPException(status_code=403, detail="This temporary upload belongs to a different browser session.")

    credentials = youtube_oauth_service.get_credentials(browser_session_id)
    if credentials is None:
        raise HTTPException(status_code=401, detail="Connect your YouTube account before publishing.")

    try:
        if job_id is not None:
            publish_job_store.update_job(
                job_id,
                state="running",
                stage="Preparing video",
                detail="Finalizing the upload and any pre-upload processing.",
            )

        upload_metadata, _ = video_service.build_metadata(
            video_path=upload_session.video_path,
            original_name=upload_session.original_filename,
            mime_type=upload_session.mime_type,
        )

        prepared_video_path, preparation_notes, applied_enhancements = video_service.prepare_publish_video(
            upload_session=upload_session,
            enhancements=payload.enhancements,
            metadata=upload_metadata,
        )

        upload_started_at = monotonic()
        prepared_video_size = prepared_video_path.stat().st_size

        if job_id is not None:
            publish_job_store.update_job(
                job_id,
                state="running",
                stage="Uploading to YouTube",
                detail="Sending the processed video from the backend to YouTube.",
                progress_percent=0.0,
                uploaded_bytes=0,
                total_bytes=prepared_video_size,
                remaining_seconds=None,
            )

        def _handle_upload_progress(progress: dict[str, float | int]) -> None:
            if job_id is None:
                return
            uploaded_bytes = int(progress.get("uploaded_bytes", 0))
            total_bytes = int(progress.get("total_bytes", prepared_video_size))
            progress_percent = float(progress.get("progress_percent", 0.0))
            remaining_seconds: Optional[float] = None
            elapsed_seconds = max(monotonic() - upload_started_at, 0.001)
            if uploaded_bytes > 0 and total_bytes > uploaded_bytes:
                upload_rate = uploaded_bytes / elapsed_seconds
                if upload_rate > 0:
                    remaining_seconds = (total_bytes - uploaded_bytes) / upload_rate

            publish_job_store.update_job(
                job_id,
                state="running",
                stage="Uploading to YouTube",
                detail="The backend is uploading your video to YouTube. Large files can take a while on Render.",
                progress_percent=progress_percent,
                uploaded_bytes=uploaded_bytes,
                total_bytes=total_bytes,
                remaining_seconds=remaining_seconds,
            )

        publish_result = youtube_upload_service.upload_video(
            credentials=credentials,
            video_path=prepared_video_path,
            title=payload.title,
            description=payload.description,
            tags=payload.tags,
            privacy_status=payload.privacy_status,
            publish_at=payload.publish_at,
            progress_callback=_handle_upload_progress if job_id is not None else None,
        )
    except YouTubeServiceError as error:
        logger.exception("YouTube publish failed: %s", error)
        raise HTTPException(status_code=500, detail=str(error)) from error
    except VideoProcessingServiceError as error:
        logger.exception("Video preparation failed before YouTube upload: %s", error)
        raise HTTPException(status_code=500, detail=str(error)) from error

    publish_notes: list[str] = [*preparation_notes]
    first_comment_posted = False
    first_comment_id: Optional[str] = None

    first_comment_supported = payload.privacy_status in {"public", "unlisted"} and payload.publish_at is None

    if payload.post_first_comment and payload.first_comment_text and not first_comment_supported:
        publish_notes.append(
            "Skipped automatic first comment because YouTube only allows it after the video is visible "
            "publicly or as unlisted. Private and scheduled uploads cannot receive comments yet."
        )

    if payload.post_first_comment and payload.first_comment_text and first_comment_supported:
        try:
            if job_id is not None:
                publish_job_store.update_job(
                    job_id,
                    state="running",
                    stage="Posting first comment",
                    detail="Posting the first comment on the uploaded video.",
                )
            first_comment_id = youtube_upload_service.post_first_comment(
                credentials=credentials,
                video_id=publish_result["video_id"],
                text=payload.first_comment_text,
            )
            first_comment_posted = True
            publish_notes.append("Posted the first comment automatically after upload.")
        except YouTubeServiceError as error:
            publish_notes.append(str(error))

    if job_id is not None:
        publish_job_store.update_job(
            job_id,
            state="running",
            stage="Cleaning up",
            detail="Deleting the temporary upload from the backend.",
        )

    deleted_local_upload = video_service.delete_upload_session(payload.upload_session_id)
    return YouTubePublishResponse(
        video_id=publish_result["video_id"],
        video_url=publish_result["video_url"],
        studio_url=publish_result["studio_url"],
        privacy_status=payload.privacy_status,
        publish_at=payload.publish_at,
        first_comment_posted=first_comment_posted,
        first_comment_id=first_comment_id,
        deleted_local_upload=deleted_local_upload,
        applied_enhancements=applied_enhancements,
        publish_notes=publish_notes,
    )


def _append_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update(params)
    return urlunparse(parsed._replace(query=urlencode(query)))
