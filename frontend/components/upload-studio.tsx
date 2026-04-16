"use client";

import { FormEvent, useEffect, useState, useTransition } from "react";

import { API_BASE_URL } from "@/lib/api";
import type {
  GenerationJobStartResponse,
  GenerationJobStatusResponse,
  GenerationResponse,
  YouTubeAuthStatus,
  YouTubePublishJobStartResponse,
  YouTubePublishJobStatusResponse,
  YouTubePublishResponse,
} from "@/lib/types";

type CopyState = {
  key: string;
  copied: boolean;
};

type PublishPrivacyStatus = "private" | "unlisted" | "public";
type PublishMode = PublishPrivacyStatus | "scheduled";

const DEFAULT_AUTH_STATUS: YouTubeAuthStatus = {
  connected: false,
  channel_title: null,
  channel_id: null,
};

const GENERATION_STAGES = [
  {
    label: "Uploading video",
    detail: "Sending the video from your browser to the live backend.",
  },
  {
    label: "Processing video",
    detail: "Reading the uploaded file and preparing the analysis workspace.",
  },
  {
    label: "Extracting frames",
    detail: "Sampling key frames and gathering video metadata.",
  },
  {
    label: "Asking Gemini",
    detail: "Generating titles, descriptions, hashtags, and first comment ideas.",
  },
  {
    label: "Finalizing package",
    detail: "Preparing the final package response for review.",
  },
] as const;

function getGenerationStageIndex(stage?: string | null) {
  if (!stage || stage === "Queued") {
    return 0;
  }

  const stageIndex = GENERATION_STAGES.findIndex((item) => item.label === stage);
  if (stageIndex >= 0) {
    return stageIndex;
  }

  if (stage === "Complete") {
    return GENERATION_STAGES.length - 1;
  }

  return 0;
}

function formatDateTimeLocalInput(date: Date) {
  const year = date.getFullYear();
  const month = `${date.getMonth() + 1}`.padStart(2, "0");
  const day = `${date.getDate()}`.padStart(2, "0");
  const hours = `${date.getHours()}`.padStart(2, "0");
  const minutes = `${date.getMinutes()}`.padStart(2, "0");
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function defaultScheduledAtValue() {
  return formatDateTimeLocalInput(new Date(Date.now() + 60 * 60 * 1000));
}

function getAuthStatusLabel(authStatus: YouTubeAuthStatus, isCheckingAuth: boolean) {
  if (isCheckingAuth) {
    return "Checking connection...";
  }
  if (!authStatus.connected) {
    return "Not connected";
  }
  return authStatus.channel_title ?? "Connected (channel name unavailable)";
}

function getAuthStatusDescription(authStatus: YouTubeAuthStatus) {
  if (!authStatus.connected) {
    return "Sign in with Google to allow this app to upload to your YouTube channel.";
  }
  if (authStatus.channel_title) {
    return `Connected in this browser session as ${authStatus.channel_title}.`;
  }
  return "Connected in this browser session, but the channel name could not be loaded yet.";
}

export function UploadStudio() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [results, setResults] = useState<GenerationResponse | null>(null);
  const [generationJobId, setGenerationJobId] = useState<string | null>(null);
  const [generationJobStatus, setGenerationJobStatus] = useState<GenerationJobStatusResponse | null>(null);
  const [authStatus, setAuthStatus] = useState<YouTubeAuthStatus>(DEFAULT_AUTH_STATUS);
  const [publishResult, setPublishResult] = useState<YouTubePublishResponse | null>(null);
  const [publishJobId, setPublishJobId] = useState<string | null>(null);
  const [publishJobStatus, setPublishJobStatus] = useState<YouTubePublishJobStatusResponse | null>(null);
  const [publishElapsedMs, setPublishElapsedMs] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [copyState, setCopyState] = useState<CopyState | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [isPublishing, setIsPublishing] = useState(false);
  const [isCheckingAuth, setIsCheckingAuth] = useState(true);
  const [selectedTitleIndex, setSelectedTitleIndex] = useState(0);
  const [selectedDescriptionIndex, setSelectedDescriptionIndex] = useState(0);
  const [titleDraft, setTitleDraft] = useState("");
  const [descriptionDraft, setDescriptionDraft] = useState("");
  const [tagsDraft, setTagsDraft] = useState("");
  const [firstCommentDraft, setFirstCommentDraft] = useState("");
  const [postFirstComment, setPostFirstComment] = useState(true);
  const [publishMode, setPublishMode] = useState<PublishMode>("private");
  const [scheduleAtDraft, setScheduleAtDraft] = useState("");
  const [isPending, startTransition] = useTransition();
  const [showGenerationProgress, setShowGenerationProgress] = useState(false);
  const [generationStageIndex, setGenerationStageIndex] = useState(0);
  const [generationStartedAt, setGenerationStartedAt] = useState<number | null>(null);
  const [generationElapsedMs, setGenerationElapsedMs] = useState<number | null>(null);
  const [generationDisplayElapsedMs, setGenerationDisplayElapsedMs] = useState<number | null>(null);

  useEffect(() => {
    void fetchAuthStatus();
    handleOAuthReturn();
  }, []);

  useEffect(() => {
    if (generationStartedAt === null || (!isSubmitting && generationJobId === null)) {
      return;
    }

    const updateElapsed = () => {
      setGenerationDisplayElapsedMs(Date.now() - generationStartedAt);
    };

    updateElapsed();
    const interval = window.setInterval(updateElapsed, 250);
    return () => window.clearInterval(interval);
  }, [generationJobId, generationStartedAt, isSubmitting]);

  useEffect(() => {
    if (!generationJobId) {
      return;
    }

    let cancelled = false;
    let timeoutId: number | null = null;

    const pollGenerationJob = async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/api/generate/jobs/${generationJobId}`, {
          credentials: "include",
        });

        if (!response.ok) {
          let backendMessage = `Generation status failed with ${response.status}.`;
          try {
            const errorPayload = (await response.json()) as { detail?: string };
            if (errorPayload.detail) {
              backendMessage = errorPayload.detail;
            }
          } catch {
            // Keep generic message if the backend did not return JSON.
          }
          throw new Error(backendMessage);
        }

        const payload = (await response.json()) as GenerationJobStatusResponse;
        if (cancelled) {
          return;
        }

        setGenerationJobStatus(payload);
        setGenerationStageIndex(getGenerationStageIndex(payload.stage));
        setGenerationDisplayElapsedMs(payload.elapsed_ms);

        if (payload.state === "succeeded" && payload.result) {
          startTransition(() => {
            setResults(payload.result ?? null);
            if (payload.result) {
              applySuggestedMetadata(payload.result);
            }
          });
          setGenerationElapsedMs(payload.elapsed_ms);
          setGenerationDisplayElapsedMs(payload.elapsed_ms);
          setGenerationJobId(null);
          window.setTimeout(() => setShowGenerationProgress(false), 350);
          return;
        }

        if (payload.state === "failed") {
          setGenerationElapsedMs(payload.elapsed_ms);
          setGenerationDisplayElapsedMs(payload.elapsed_ms);
          setGenerationJobId(null);
          setShowGenerationProgress(false);
          setError(payload.error ?? "Something went wrong while generating the YouTube package.");
          return;
        }

        timeoutId = window.setTimeout(() => {
          void pollGenerationJob();
        }, 1000);
      } catch (pollError) {
        if (cancelled) {
          return;
        }

        setGenerationJobId(null);
        setShowGenerationProgress(false);
        setError(
          pollError instanceof Error
            ? pollError.message
            : "Something went wrong while checking generation status.",
        );
      }
    };

    void pollGenerationJob();

    return () => {
      cancelled = true;
      if (timeoutId != null) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [generationJobId, startTransition]);

  useEffect(() => {
    if (!publishJobId || !isPublishing) {
      return;
    }

    let cancelled = false;
    let timeoutId: number | null = null;

    const pollPublishJob = async () => {
      try {
        const response = await fetch(`${API_BASE_URL}/api/youtube/publish/jobs/${publishJobId}`, {
          credentials: "include",
        });

        if (!response.ok) {
          let backendMessage = `Publish status failed with ${response.status}.`;
          try {
            const errorPayload = (await response.json()) as { detail?: string };
            if (errorPayload.detail) {
              backendMessage = errorPayload.detail;
            }
          } catch {
            // Keep generic error if the backend did not return JSON.
          }
          throw new Error(backendMessage);
        }

        const payload = (await response.json()) as YouTubePublishJobStatusResponse;
        if (cancelled) {
          return;
        }

        setPublishJobStatus(payload);

        if (payload.state === "succeeded" && payload.result) {
          setPublishElapsedMs(payload.elapsed_ms);
          setPublishResult(payload.result);
          setNotice(
            payload.result.publish_at
              ? payload.result.deleted_local_upload
                ? "Scheduled on YouTube. The temporary local upload was deleted from the server."
                : "Scheduled on YouTube."
              : payload.result.deleted_local_upload
                ? "Published to YouTube. The temporary local upload was deleted from the server."
                : "Published to YouTube.",
          );
          setIsPublishing(false);
          setPublishJobId(null);
          return;
        }

        if (payload.state === "failed") {
          setPublishElapsedMs(payload.elapsed_ms);
          setError(payload.error ?? "Something went wrong while publishing to YouTube.");
          setIsPublishing(false);
          setPublishJobId(null);
          return;
        }

        timeoutId = window.setTimeout(() => {
          void pollPublishJob();
        }, 1000);
      } catch (pollError) {
        if (cancelled) {
          return;
        }
        setIsPublishing(false);
        setPublishJobId(null);
        setError(
          pollError instanceof Error
            ? pollError.message
            : "Something went wrong while checking YouTube publish status.",
        );
      }
    };

    void pollPublishJob();

    return () => {
      cancelled = true;
      if (timeoutId != null) {
        window.clearTimeout(timeoutId);
      }
    };
  }, [isPublishing, publishJobId]);

  async function fetchAuthStatus() {
    setIsCheckingAuth(true);

    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/youtube/status`, {
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error(`YouTube auth status failed with ${response.status}.`);
      }

      const payload = (await response.json()) as YouTubeAuthStatus;
      setAuthStatus(payload);
    } catch {
      setAuthStatus(DEFAULT_AUTH_STATUS);
    } finally {
      setIsCheckingAuth(false);
    }
  }

  function handleOAuthReturn() {
    if (typeof window === "undefined") {
      return;
    }

    const currentUrl = new URL(window.location.href);
    const youtubeStatus = currentUrl.searchParams.get("youtube");
    const message = currentUrl.searchParams.get("message");

    if (!youtubeStatus && !message) {
      return;
    }

    if (youtubeStatus === "connected") {
      setNotice("YouTube account connected. You can now publish the generated package.");
      void fetchAuthStatus();
    } else if (youtubeStatus === "error") {
      setError(message ?? "YouTube sign-in did not complete.");
    }

    currentUrl.searchParams.delete("youtube");
    currentUrl.searchParams.delete("message");
    window.history.replaceState({}, "", currentUrl.toString());
  }

  async function discardUploadSession(uploadSessionId?: string) {
    if (!uploadSessionId) {
      return;
    }

    try {
      await fetch(`${API_BASE_URL}/api/uploads/${uploadSessionId}`, {
        method: "DELETE",
        credentials: "include",
      });
    } catch {
      // The session will still expire automatically on the backend.
    }
  }

  async function runGeneration() {
    if (!selectedFile) {
      setError("Choose a video file before generating a YouTube package.");
      return;
    }

    setIsSubmitting(true);
    setError(null);
    setNotice(null);
    setPublishResult(null);
    setShowGenerationProgress(true);
    setGenerationStageIndex(0);
    setGenerationJobId(null);
    setGenerationJobStatus(null);
    const startedAt = Date.now();
    setGenerationStartedAt(startedAt);
    setGenerationElapsedMs(null);
    setGenerationDisplayElapsedMs(0);

    try {
      await discardUploadSession(results?.upload_session_id);

      const formData = new FormData();
      formData.append("file", selectedFile);

      const response = await fetch(`${API_BASE_URL}/api/generate/start`, {
        method: "POST",
        body: formData,
        credentials: "include",
      });

      if (!response.ok) {
        let backendMessage = `Backend request failed with status ${response.status}.`;
        try {
          const errorPayload = (await response.json()) as { detail?: string };
          if (errorPayload.detail) {
            backendMessage = errorPayload.detail;
          }
        } catch {
          // Keep the generic message if the backend did not return JSON.
        }
        throw new Error(backendMessage);
      }

      const payload = (await response.json()) as GenerationJobStartResponse;
      setGenerationJobId(payload.job_id);
      setGenerationJobStatus({
        job_id: payload.job_id,
        state: payload.state,
        stage: "Queued",
        detail: "The upload finished and frame analysis is about to start.",
        progress_percent: 5.0,
        elapsed_ms: Date.now() - startedAt,
        result: null,
        error: null,
      });
      setPublishJobId(null);
      setPublishJobStatus(null);
      setPublishElapsedMs(null);
    } catch (submissionError) {
      setShowGenerationProgress(false);
      setGenerationStartedAt(null);
      setGenerationElapsedMs(null);
      setGenerationDisplayElapsedMs(null);
      setError(
        submissionError instanceof Error
          ? submissionError.message
          : "Something went wrong while generating the YouTube package.",
      );
    } finally {
      setIsSubmitting(false);
    }
  }

  function applySuggestedMetadata(payload: GenerationResponse) {
    setSelectedTitleIndex(0);
    setSelectedDescriptionIndex(0);
    setTitleDraft(payload.hook_titles[0]?.text ?? "");
    setDescriptionDraft(payload.descriptions[0]?.text ?? "");
    setTagsDraft(payload.hashtags.join(" "));
    setFirstCommentDraft(payload.first_comment_text ?? "");
    setPostFirstComment(Boolean(payload.first_comment_text));
    setPublishMode("private");
    setScheduleAtDraft("");
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void runGeneration();
  }

  function handleFileChange(file: File | null) {
    if (results?.upload_session_id) {
      void discardUploadSession(results.upload_session_id);
    }

    setSelectedFile(file);
    setResults(null);
    setGenerationJobId(null);
    setGenerationJobStatus(null);
    setPublishResult(null);
    setPublishJobId(null);
    setPublishJobStatus(null);
    setPublishElapsedMs(null);
    setError(null);
    setNotice(null);
    setTitleDraft("");
    setDescriptionDraft("");
    setTagsDraft("");
    setFirstCommentDraft("");
    setPostFirstComment(true);
    setSelectedTitleIndex(0);
    setSelectedDescriptionIndex(0);
    setPublishMode("private");
    setScheduleAtDraft("");
  }

  function selectTitle(index: number) {
    if (!results?.hook_titles[index]) {
      return;
    }

    setSelectedTitleIndex(index);
    setTitleDraft(results.hook_titles[index].text);
  }

  function selectDescription(index: number) {
    if (!results?.descriptions[index]) {
      return;
    }

    setSelectedDescriptionIndex(index);
    setDescriptionDraft(results.descriptions[index].text);
  }

  function connectYouTube() {
    if (typeof window === "undefined") {
      return;
    }

    const returnTo = `${window.location.origin}${window.location.pathname}`;
    window.location.href = `${API_BASE_URL}/api/auth/youtube/start?return_to=${encodeURIComponent(returnTo)}`;
  }

  function handlePublishModeChange(nextMode: PublishMode) {
    setPublishMode(nextMode);
    if (nextMode === "scheduled") {
      setScheduleAtDraft((current) => current || defaultScheduledAtValue());
      return;
    }
    setScheduleAtDraft("");
  }

  async function disconnectYouTube() {
    setError(null);
    setNotice(null);

    try {
      const response = await fetch(`${API_BASE_URL}/api/auth/youtube/disconnect`, {
        method: "POST",
        credentials: "include",
      });
      if (!response.ok) {
        throw new Error(`Disconnect failed with ${response.status}.`);
      }

      setAuthStatus(DEFAULT_AUTH_STATUS);
      setNotice("YouTube account disconnected from this browser session.");
    } catch (disconnectError) {
      setError(
        disconnectError instanceof Error
          ? disconnectError.message
          : "Something went wrong while disconnecting YouTube.",
      );
    }
  }

  async function publishToYouTube() {
    if (!results?.upload_session_id) {
      setError("Generate the package again before publishing.");
      return;
    }

    if (!authStatus.connected) {
      setError("Connect your YouTube account before publishing.");
      return;
    }

    if (!titleDraft.trim() || !descriptionDraft.trim()) {
      setError("The title and description are required before publishing.");
      return;
    }

    if (postFirstComment && !firstCommentDraft.trim()) {
      setError("Enter the first comment text or turn off first-comment posting.");
      return;
    }

    let publishAt: string | null = null;
    let effectivePrivacyStatus: PublishPrivacyStatus = publishMode === "scheduled" ? "private" : publishMode;

    if (publishMode === "scheduled") {
      if (!scheduleAtDraft.trim()) {
        setError("Choose the date and time when YouTube should publish this video.");
        return;
      }

      const scheduledDate = new Date(scheduleAtDraft);
      if (Number.isNaN(scheduledDate.getTime())) {
        setError("Choose a valid schedule date and time.");
        return;
      }
      if (scheduledDate.getTime() <= Date.now()) {
        setError("The scheduled publish time must be in the future.");
        return;
      }

      publishAt = scheduledDate.toISOString();
      effectivePrivacyStatus = "private";
    }

    setIsPublishing(true);
    setPublishJobId(null);
    setPublishJobStatus(null);
    setPublishElapsedMs(null);
    setError(null);
    setNotice(null);
    setPublishResult(null);

    try {
      const response = await fetch(`${API_BASE_URL}/api/youtube/publish/start`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        credentials: "include",
        body: JSON.stringify({
          upload_session_id: results.upload_session_id,
          title: titleDraft.trim(),
          description: descriptionDraft.trim(),
          tags: parseTags(tagsDraft),
          privacy_status: effectivePrivacyStatus,
          publish_at: publishAt,
          post_first_comment: postFirstComment,
          first_comment_text: postFirstComment ? firstCommentDraft.trim() : null,
        }),
      });

      if (!response.ok) {
        let backendMessage = `Publish failed with status ${response.status}.`;
        try {
          const errorPayload = (await response.json()) as { detail?: string };
          if (errorPayload.detail) {
            backendMessage = errorPayload.detail;
          }
        } catch {
          // Keep generic error.
        }
        throw new Error(backendMessage);
      }

      const payload = (await response.json()) as YouTubePublishJobStartResponse;
      setPublishJobId(payload.job_id);
      setPublishJobStatus({
        job_id: payload.job_id,
        state: payload.state,
        stage: "Queued",
        detail: "The YouTube publish job is starting on the backend.",
        progress_percent: null,
        uploaded_bytes: null,
        total_bytes: null,
        remaining_seconds: null,
        elapsed_ms: 0,
        result: null,
        error: null,
      });
    } catch (publishError) {
      setError(
        publishError instanceof Error
          ? publishError.message
          : "Something went wrong while publishing to YouTube.",
      );
      setIsPublishing(false);
    }
  }

  async function copyText(key: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopyState({ key, copied: true });
      window.setTimeout(() => setCopyState(null), 1800);
    } catch {
      setError("Clipboard access was blocked in this browser session.");
    }
  }

  function parseTags(value: string) {
    const matches = value.match(/#[A-Za-z0-9_]+|[A-Za-z0-9_]+/g) ?? [];
    const cleaned: string[] = [];
    const seen = new Set<string>();

    for (const entry of matches) {
      const normalized = entry.trim();
      if (!normalized) {
        continue;
      }

      const key = normalized.toLowerCase();
      if (seen.has(key)) {
        continue;
      }
      seen.add(key);
      cleaned.push(normalized);
    }

    return cleaned;
  }

  function formatBytes(value?: number | null) {
    if (value == null) {
      return "Unknown";
    }
    const units = ["B", "KB", "MB", "GB"];
    let current = value;
    let unitIndex = 0;
    while (current >= 1024 && unitIndex < units.length - 1) {
      current /= 1024;
      unitIndex += 1;
    }
    return `${current.toFixed(1)} ${units[unitIndex]}`;
  }

  function formatRemainingTime(value?: number | null) {
    if (value == null || !Number.isFinite(value)) {
      return "Estimating...";
    }

    const totalSeconds = Math.max(0, value);
    if (totalSeconds < 60) {
      return `${Math.round(totalSeconds)}s`;
    }

    const minutes = Math.floor(totalSeconds / 60);
    const seconds = Math.round(totalSeconds % 60);
    return `${minutes}m ${seconds}s`;
  }

  function formatLabel(value: string) {
    return value
      .split("_")
      .join(" ")
      .replace(/\b\w/g, (character) => character.toUpperCase());
  }

  function formatExpiry(value?: string) {
    if (!value) {
      return "Unknown";
    }

    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return "Unknown";
    }

    return date.toLocaleString();
  }

  function formatScheduleSummary(value: string) {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return "Choose a valid local date and time to schedule this upload.";
    }
    return `YouTube will keep this upload private until ${date.toLocaleString()}.`;
  }

  function formatElapsedTime(value?: number | null) {
    if (value == null) {
      return "Unknown";
    }

    const totalSeconds = Math.max(0, value / 1000);
    if (totalSeconds < 60) {
      return `${totalSeconds.toFixed(1)}s`;
    }

    const minutes = Math.floor(totalSeconds / 60);
    const seconds = totalSeconds % 60;
    return `${minutes}m ${seconds.toFixed(1)}s`;
  }

  const canPublish =
    Boolean(results?.upload_session_id) &&
    authStatus.connected &&
    !publishResult &&
    !isPublishing &&
    !isSubmitting &&
    titleDraft.trim().length > 0 &&
    descriptionDraft.trim().length > 0 &&
    (publishMode !== "scheduled" || scheduleAtDraft.trim().length > 0) &&
    (!postFirstComment || firstCommentDraft.trim().length > 0);
  const isGenerating = isSubmitting || generationJobId !== null;
  const authStatusLabel = getAuthStatusLabel(authStatus, isCheckingAuth);
  const authStatusDescription = getAuthStatusDescription(authStatus);

  return (
    <main className="page-shell">
      <section className="hero">
        <div className="hero-copy">
          <p className="eyebrow">Shorts Publishing Studio</p>
          <h1>Generate a YouTube Shorts package and publish it from one workflow.</h1>
          <p className="hero-text">
            Upload a video, let Gemini generate YouTube-ready metadata from sampled frames, review
            the package, then publish the short through the YouTube API. The backend keeps the
            uploaded video only in temporary storage and deletes it after a successful upload.
          </p>
          <div className="hero-pills">
            <span>Connect YouTube</span>
            <span>Generate metadata</span>
            <span>Review before publish</span>
            <span>Delete temp upload after publish</span>
          </div>
        </div>
        <div className="hero-panel">
          <div className="stat-card">
            <span className="stat-label">YouTube status</span>
            <strong>{authStatusLabel}</strong>
          </div>
          <div className="stat-card">
            <span className="stat-label">Current file</span>
            <strong>{selectedFile?.name ?? "No file selected yet"}</strong>
          </div>
          <div className="stat-card">
            <span className="stat-label">Publish mode</span>
            <strong>{publishMode}</strong>
          </div>
        </div>
      </section>

      <section className="studio-grid">
        <form className="studio-panel form-panel" onSubmit={handleSubmit}>
          <div className="panel-header">
            <h2>Upload Flow</h2>
            <p>Connect your YouTube channel, upload a video for temporary analysis, and publish with reviewed metadata.</p>
          </div>

          <div className="info-card">
            <h3>YouTube Connection</h3>
            <p>{authStatusDescription}</p>
            <div className="action-row">
              <button
                type="button"
                className="secondary-button"
                onClick={connectYouTube}
                disabled={isCheckingAuth}
              >
                {authStatus.connected ? "Reconnect YouTube" : "Connect YouTube"}
              </button>
              {authStatus.connected ? (
                <button type="button" className="secondary-button" onClick={() => void disconnectYouTube()}>
                  Disconnect
                </button>
              ) : null}
            </div>
          </div>

          <label className="upload-zone">
            <input
              type="file"
              accept="video/*"
              onChange={(event) => handleFileChange(event.target.files?.[0] ?? null)}
            />
            <span className="upload-title">
              {selectedFile ? selectedFile.name : "Drop a video here or browse"}
            </span>
            <span className="upload-subtitle">
              The backend stores the upload only temporarily until publish or expiry
            </span>
          </label>

          <div className="info-card">
            <h3>What happens</h3>
            <ul>
              <li>The backend samples frames and generates titles, descriptions, hashtags, and a first comment suggestion.</li>
              <li>You choose or edit the metadata before publish.</li>
              <li>The video uploads to YouTube through the official API.</li>
              <li>The temporary local upload is deleted after a successful YouTube upload.</li>
            </ul>
          </div>

          {notice ? <p className="success-banner">{notice}</p> : null}
          {error ? <p className="error-banner">{error}</p> : null}

          {showGenerationProgress ? (
            <div className="info-card progress-card">
              <h3>Generation Progress</h3>
              <div className="progress-list">
                {GENERATION_STAGES.map((stage, index) => {
                  const state =
                    index < generationStageIndex
                      ? "complete"
                      : index === generationStageIndex
                        ? "active"
                        : "pending";

                  return (
                    <div key={stage.label} className={`progress-step progress-step-${state}`}>
                      <div className="progress-dot" aria-hidden="true" />
                      <div>
                        <strong>{stage.label}</strong>
                        <p>{stage.detail}</p>
                      </div>
                    </div>
                  );
                })}
              </div>
              <p className="section-caption">
                {isSubmitting
                  ? "Uploading the video to the backend before analysis starts."
                  : generationJobStatus?.detail
                    ? generationJobStatus.detail
                    : "Finalizing the package response."}
              </p>
              {generationJobStatus?.progress_percent != null && !isSubmitting ? (
                <p className="section-caption">
                  Progress: {generationJobStatus.progress_percent.toFixed(0)}%
                </p>
              ) : null}
              <p className="section-caption">
                Elapsed time: {formatElapsedTime(generationDisplayElapsedMs)}
              </p>
            </div>
          ) : null}

          <button className="primary-button" type="submit" disabled={isGenerating || isPending}>
            {isGenerating || isPending ? "Generating package..." : "Generate YouTube Package"}
          </button>
        </form>

        <section className="studio-panel results-panel">
          <div className="panel-header">
            <h2>Review And Publish</h2>
            <p>Review the generated package, edit any field you want, and publish directly to YouTube.</p>
          </div>

          {!results ? (
            <div className="empty-state">
              <p>The first run will generate the top 2 titles, 2 descriptions, hashtags, frame insights, and a temporary upload session for publishing.</p>
            </div>
          ) : (
            <div className="results-stack">
              <div className="results-topline">
                <div>
                  <span className="meta-label">Detected category</span>
                  <strong>{results.category}</strong>
                </div>
                <div>
                  <span className="meta-label">Temp upload expires</span>
                  <strong>{formatExpiry(results.upload_expires_at)}</strong>
                </div>
                <div>
                  <span className="meta-label">Generation time</span>
                  <strong>{formatElapsedTime(generationElapsedMs)}</strong>
                </div>
                <button
                  type="button"
                  className="secondary-button"
                  disabled={isGenerating || isPending || !selectedFile}
                  onClick={() => void runGeneration()}
                >
                  Regenerate
                </button>
              </div>

              <div className="results-section">
                <h3>Visual Basis</h3>
                <article className="info-card">
                  <p>{results.visual_basis}</p>
                </article>
              </div>

              <div className="results-section">
                <h3>Top YouTube Shorts Titles</h3>
                <p className="section-caption">Pick one of the top 2 curiosity titles or edit it before publish.</p>
                <div className="result-card-grid">
                  {results.hook_titles.map((title, index) => (
                    <article
                      key={`hook-title-${index}`}
                      className={`result-card ${selectedTitleIndex === index ? "active-card" : ""}`}
                    >
                      <div className="result-card-top">
                        <span>Shorts Title {index + 1}</span>
                        <span>{title.score}/10 · {title.text.length}/100 chars</span>
                      </div>
                      <p>{title.text}</p>
                      <div className="action-row">
                        <button
                          type="button"
                          className="secondary-button"
                          onClick={() => selectTitle(index)}
                        >
                          {selectedTitleIndex === index ? "Using This Title" : "Use Title"}
                        </button>
                        <button
                          type="button"
                          className="copy-button"
                          onClick={() => copyText(`title-${index}`, title.text)}
                        >
                          {copyState?.key === `title-${index}` && copyState.copied ? "Copied" : "Copy"}
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              </div>

              <div className="results-section">
                <h3>YouTube Shorts Descriptions</h3>
                <p className="section-caption">Descriptions are formatted as hook, context, CTA, and hashtags.</p>
                {results.descriptions.map((description, index) => (
                  <article
                    key={`${description.angle}-${index}`}
                    className={`result-row ${selectedDescriptionIndex === index ? "active-card" : ""}`}
                  >
                    <div className="result-main">
                      <span className="meta-label">{description.angle}</span>
                      <p className="multiline-copy">{description.text}</p>
                    </div>
                    <div className="action-column">
                      <button
                        type="button"
                        className="secondary-button"
                        onClick={() => selectDescription(index)}
                      >
                        {selectedDescriptionIndex === index ? "Using This Description" : "Use Description"}
                      </button>
                      <button
                        type="button"
                        className="copy-button"
                        onClick={() => copyText(`description-${index}`, description.text)}
                      >
                        {copyState?.key === `description-${index}` && copyState.copied ? "Copied" : "Copy"}
                      </button>
                    </div>
                  </article>
                ))}
              </div>

              <div className="results-section">
                <article className="info-card">
                  <h3>First Comment</h3>
                  <p className="section-caption">
                    Auto-post the first comment right after upload. You can pin it manually later in YouTube Studio.
                  </p>
                  {publishMode === "scheduled" ? (
                    <p className="section-caption">
                      Scheduled uploads will queue the first comment and post it automatically after the video becomes visible.
                    </p>
                  ) : publishMode === "private" ? (
                    <p className="section-caption">
                      Private uploads will queue the first comment and keep retrying until you later change the video to public or unlisted.
                    </p>
                  ) : null}
                  <label className="checkbox-row">
                    <input
                      type="checkbox"
                      checked={postFirstComment}
                      onChange={(event) => setPostFirstComment(event.target.checked)}
                    />
                    <span>Post the first comment automatically after upload</span>
                  </label>
                  <label className="field-group">
                    <span>First Comment Text</span>
                    <textarea
                      rows={5}
                      value={firstCommentDraft}
                      onChange={(event) => setFirstCommentDraft(event.target.value)}
                      placeholder="Ask a question, tease the next short, or add your CTA here"
                    />
                  </label>
                </article>
              </div>

              <div className="results-section">
                <h3>Publish To YouTube</h3>
                <div className="info-card publish-panel">
                  <div className="field-group">
                    <span>Title</span>
                    <input
                      value={titleDraft}
                      maxLength={100}
                      onChange={(event) => setTitleDraft(event.target.value)}
                      placeholder="Final YouTube Shorts title"
                    />
                  </div>

                  <div className="field-group">
                    <span>Description</span>
                    <textarea
                      rows={6}
                      value={descriptionDraft}
                      onChange={(event) => setDescriptionDraft(event.target.value)}
                      placeholder="Final YouTube Shorts description"
                    />
                  </div>

                  <div className="field-row two-field">
                    <label className="field-group">
                      <span>Tags</span>
                      <input
                        value={tagsDraft}
                        onChange={(event) => setTagsDraft(event.target.value)}
                        placeholder="#shorts #viralclip #topic"
                      />
                    </label>
                    <label className="field-group">
                      <span>Publish Mode</span>
                      <select
                        value={publishMode}
                        onChange={(event) => handlePublishModeChange(event.target.value as PublishMode)}
                      >
                        <option value="private">Private</option>
                        <option value="unlisted">Unlisted</option>
                        <option value="public">Public</option>
                        <option value="scheduled">Scheduled</option>
                      </select>
                    </label>
                  </div>

                  {publishMode === "scheduled" ? (
                    <div className="field-group">
                      <span>Publish At</span>
                      <input
                        type="datetime-local"
                        value={scheduleAtDraft}
                        min={formatDateTimeLocalInput(new Date())}
                        onChange={(event) => setScheduleAtDraft(event.target.value)}
                      />
                      <p className="section-caption">{formatScheduleSummary(scheduleAtDraft)}</p>
                    </div>
                  ) : null}

                  <div className="publish-meta">
                    <span className="meta-label">
                      {publishMode === "scheduled"
                        ? "Scheduled uploads are sent to YouTube as private until the publish time."
                        : authStatus.connected
                        ? `Publishing to ${authStatus.channel_title ?? "your connected YouTube channel"}`
                        : "Connect YouTube to enable publishing"}
                    </span>
                    <span className="meta-label">{titleDraft.length}/100 characters</span>
                  </div>

                  <button
                    type="button"
                    className="primary-button publish-button"
                    disabled={!canPublish}
                    onClick={() => void publishToYouTube()}
                  >
                    {isPublishing
                      ? publishJobStatus?.stage
                        ? `${publishJobStatus.stage}...`
                        : publishMode === "scheduled"
                          ? "Scheduling On YouTube..."
                          : "Uploading To YouTube..."
                      : publishMode === "scheduled"
                        ? "Schedule On YouTube"
                        : "Publish To YouTube"}
                  </button>
                </div>

                {isPublishing && publishJobStatus ? (
                  <article className="info-card">
                    <h3>Live Publish Status</h3>
                    <p className="section-caption">
                      {publishJobStatus.detail ?? "The backend is still working through the YouTube publish flow."}
                    </p>
                    <div className="publish-meta">
                      <span className="meta-label">Stage: {publishJobStatus.stage}</span>
                      <span className="meta-label">Elapsed: {formatElapsedTime(publishJobStatus.elapsed_ms)}</span>
                    </div>
                    {publishJobStatus.progress_percent != null ? (
                      <div className="publish-meta">
                        <span className="meta-label">
                          Upload progress: {publishJobStatus.progress_percent.toFixed(1)}%
                        </span>
                        <span className="meta-label">
                          ETA: {formatRemainingTime(publishJobStatus.remaining_seconds)}
                        </span>
                      </div>
                    ) : null}
                    {publishJobStatus.uploaded_bytes != null && publishJobStatus.total_bytes != null ? (
                      <p className="section-caption">
                        {formatBytes(publishJobStatus.uploaded_bytes)} / {formatBytes(publishJobStatus.total_bytes)}
                      </p>
                    ) : (
                      <p className="section-caption">
                        YouTube upload progress becomes more accurate once the backend starts sending the video file.
                      </p>
                    )}
                  </article>
                ) : null}

                {publishResult ? (
                  <article className="info-card success-card">
                    <h3>{publishResult.publish_at ? "Upload Scheduled" : "Upload Complete"}</h3>
                    <p>
                      {publishResult.publish_at
                        ? `Your video was uploaded to YouTube and scheduled for ${formatExpiry(publishResult.publish_at)}.`
                        : "Your video was uploaded to YouTube and the temporary local upload was cleaned up."}
                    </p>
                    {publishResult.publish_at ? (
                      <p>The video stays private on YouTube until the scheduled publish time arrives.</p>
                    ) : null}
                    {publishElapsedMs != null ? (
                      <p>Total publish time: {formatElapsedTime(publishElapsedMs)}</p>
                    ) : null}
                    <p>
                      First comment:{" "}
                      {publishResult.first_comment_posted
                        ? "posted"
                        : publishResult.first_comment_queued
                          ? "queued for automatic posting"
                          : "not posted"}
                      .
                    </p>
                    {publishResult.publish_notes.length > 0 ? (
                      <ul className="notes-list">
                        {publishResult.publish_notes.map((note) => (
                          <li key={note}>{note}</li>
                        ))}
                      </ul>
                    ) : null}
                    <div className="link-row">
                      <a href={publishResult.video_url} target="_blank" rel="noreferrer">
                        View on YouTube
                      </a>
                      <a href={publishResult.studio_url} target="_blank" rel="noreferrer">
                        Open in YouTube Studio
                      </a>
                    </div>
                  </article>
                ) : null}
              </div>

              <div className="results-section">
                <h3>Hashtags</h3>
                <div className="tag-row">
                  {results.hashtags.map((hashtag) => (
                    <span key={hashtag} className="tag-chip">
                      {hashtag}
                    </span>
                  ))}
                </div>
              </div>

              <div className="results-section two-column">
                <article className="info-card">
                  <h3>Detected Objects</h3>
                  <ul>
                    {results.detected_objects.length > 0 ? (
                      results.detected_objects.map((object) => (
                        <li key={object.label}>
                          {formatLabel(object.label)}: visible in {object.count} sampled frame{object.count === 1 ? "" : "s"}
                        </li>
                      ))
                    ) : (
                      <li>No reliable objects were detected.</li>
                    )}
                  </ul>
                </article>
                <article className="info-card">
                  <h3>Upload Metadata</h3>
                  <ul>
                    <li>File: {results.metadata.filename}</li>
                    <li>Size: {formatBytes(results.metadata.size_bytes)}</li>
                    <li>Duration: {results.metadata.duration_seconds ?? "Unknown"}s</li>
                    <li>Resolution: {results.metadata.width ?? "?"} x {results.metadata.height ?? "?"}</li>
                    <li>FPS: {results.metadata.fps ?? "Unknown"}</li>
                  </ul>
                </article>
              </div>

              <div className="results-section">
                <h3>Frame Insights</h3>
                <div className="result-card-grid">
                  {results.frame_insights.map((insight) => (
                    <article key={`${insight.timestamp_seconds}-${insight.summary}`} className="result-card">
                      <div className="result-card-top">
                        <span>{insight.timestamp_seconds}s</span>
                        <span>{insight.tags.length} tags</span>
                      </div>
                      <p>{insight.summary}</p>
                    </article>
                  ))}
                </div>
              </div>

              <div className="results-section">
                <h3>Processing Notes</h3>
                <ul className="notes-list">
                  {results.processing_notes.map((note) => (
                    <li key={note}>{note}</li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </section>
      </section>
    </main>
  );
}
