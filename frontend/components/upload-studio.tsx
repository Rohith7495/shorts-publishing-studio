"use client";

import { FormEvent, useEffect, useState, useTransition } from "react";

import { API_BASE_URL } from "@/lib/api";
import type {
  GenerationResponse,
  VideoEnhancementOptions,
  YouTubeAuthStatus,
  YouTubePublishResponse,
} from "@/lib/types";

type CopyState = {
  key: string;
  copied: boolean;
};

type PrivacyStatus = "private" | "unlisted" | "public";

const DEFAULT_AUTH_STATUS: YouTubeAuthStatus = {
  connected: false,
  channel_title: null,
  channel_id: null,
};

const DEFAULT_ENHANCEMENTS: VideoEnhancementOptions = {
  visual_pop: false,
  audio_cleanup: false,
};

export function UploadStudio() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [results, setResults] = useState<GenerationResponse | null>(null);
  const [authStatus, setAuthStatus] = useState<YouTubeAuthStatus>(DEFAULT_AUTH_STATUS);
  const [publishResult, setPublishResult] = useState<YouTubePublishResponse | null>(null);
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
  const [privacyStatus, setPrivacyStatus] = useState<PrivacyStatus>("private");
  const [enhancements, setEnhancements] = useState<VideoEnhancementOptions>(DEFAULT_ENHANCEMENTS);
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    void fetchAuthStatus();
    handleOAuthReturn();
  }, []);

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

    try {
      await discardUploadSession(results?.upload_session_id);

      const formData = new FormData();
      formData.append("file", selectedFile);

      const response = await fetch(`${API_BASE_URL}/api/generate`, {
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

      const payload = (await response.json()) as GenerationResponse;
      startTransition(() => {
        setResults(payload);
        applySuggestedMetadata(payload);
      });
    } catch (submissionError) {
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
    setPrivacyStatus("private");
    setEnhancements(DEFAULT_ENHANCEMENTS);
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
    setPublishResult(null);
    setError(null);
    setNotice(null);
    setTitleDraft("");
    setDescriptionDraft("");
    setTagsDraft("");
    setSelectedTitleIndex(0);
    setSelectedDescriptionIndex(0);
    setEnhancements(DEFAULT_ENHANCEMENTS);
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

    setIsPublishing(true);
    setError(null);
    setNotice(null);

    try {
      const response = await fetch(`${API_BASE_URL}/api/youtube/publish`, {
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
          privacy_status: privacyStatus,
          enhancements,
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

      const payload = (await response.json()) as YouTubePublishResponse;
      setPublishResult(payload);
      setNotice(
        payload.deleted_local_upload
          ? "Published to YouTube. The temporary local upload was deleted from the server."
          : "Published to YouTube.",
      );
    } catch (publishError) {
      setError(
        publishError instanceof Error
          ? publishError.message
          : "Something went wrong while publishing to YouTube.",
      );
    } finally {
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
    if (!value) {
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

  const canPublish =
    Boolean(results?.upload_session_id) &&
    authStatus.connected &&
    !publishResult &&
    !isPublishing &&
    !isSubmitting &&
    titleDraft.trim().length > 0 &&
    descriptionDraft.trim().length > 0;

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
            <strong>
              {isCheckingAuth
                ? "Checking connection..."
                : authStatus.connected
                  ? authStatus.channel_title ?? "Connected"
                  : "Not connected"}
            </strong>
          </div>
          <div className="stat-card">
            <span className="stat-label">Current file</span>
            <strong>{selectedFile?.name ?? "No file selected yet"}</strong>
          </div>
          <div className="stat-card">
            <span className="stat-label">Publish mode</span>
            <strong>{privacyStatus}</strong>
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
            <p>
              {authStatus.connected
                ? `Connected in this browser session${authStatus.channel_title ? ` as ${authStatus.channel_title}` : ""}.`
                : "Sign in with Google to allow this app to upload to your YouTube channel."}
            </p>
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
              <li>The backend samples frames and generates titles, descriptions, and hashtags.</li>
              <li>You choose or edit the metadata before publish.</li>
              <li>The video uploads to YouTube through the official API.</li>
              <li>The temporary local upload is deleted after a successful YouTube upload.</li>
            </ul>
          </div>

          {notice ? <p className="success-banner">{notice}</p> : null}
          {error ? <p className="error-banner">{error}</p> : null}

          <button className="primary-button" type="submit" disabled={isSubmitting || isPending}>
            {isSubmitting || isPending ? "Generating package..." : "Generate YouTube Package"}
          </button>
        </form>

        <section className="studio-panel results-panel">
          <div className="panel-header">
            <h2>Review And Publish</h2>
            <p>Review the AI-generated package, edit any field you want, and publish directly to YouTube.</p>
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
                <button
                  type="button"
                  className="secondary-button"
                  disabled={isSubmitting || isPending || !selectedFile}
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
                      <span>Privacy</span>
                      <select
                        value={privacyStatus}
                        onChange={(event) => setPrivacyStatus(event.target.value as PrivacyStatus)}
                      >
                        <option value="private">Private</option>
                        <option value="unlisted">Unlisted</option>
                        <option value="public">Public</option>
                      </select>
                    </label>
                  </div>

                  <div className="field-group">
                    <span>Enhance Video Before Upload</span>
                    <div className="option-grid">
                      <label className="toggle-card">
                        <input
                          type="checkbox"
                          checked={enhancements.visual_pop}
                          onChange={(event) =>
                            setEnhancements((current) => ({
                              ...current,
                              visual_pop: event.target.checked,
                            }))
                          }
                        />
                        <div>
                          <strong>Pop Look</strong>
                          <p>Boost contrast, saturation, and sharpness before the YouTube upload.</p>
                        </div>
                      </label>
                      <label className="toggle-card">
                        <input
                          type="checkbox"
                          checked={enhancements.audio_cleanup}
                          onChange={(event) =>
                            setEnhancements((current) => ({
                              ...current,
                              audio_cleanup: event.target.checked,
                            }))
                          }
                        />
                        <div>
                          <strong>Audio Cleanup</strong>
                          <p>Reduce rumble and noise, then normalize loudness before upload.</p>
                        </div>
                      </label>
                    </div>
                  </div>

                  <div className="publish-meta">
                    <span className="meta-label">
                      {authStatus.connected
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
                    {isPublishing ? "Uploading To YouTube..." : "Publish To YouTube"}
                  </button>
                </div>

                {publishResult ? (
                  <article className="info-card success-card">
                    <h3>Upload Complete</h3>
                    <p>Your video was uploaded to YouTube and the temporary local upload was cleaned up.</p>
                    {publishResult.applied_enhancements.length > 0 ? (
                      <p>
                        Applied enhancements: {publishResult.applied_enhancements.join(", ")}
                      </p>
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
