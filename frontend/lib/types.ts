export type HookTitleCandidate = {
  text: string;
  score: number;
};

export type DescriptionCandidate = {
  text: string;
  angle: string;
};

export type FrameInsight = {
  timestamp_seconds: number;
  summary: string;
  tags: string[];
};

export type DetectedObject = {
  label: string;
  count: number;
};

export type VideoMetadata = {
  filename: string;
  mime_type?: string | null;
  size_bytes?: number | null;
  duration_seconds?: number | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
};

export type GenerationResponse = {
  category: string;
  visual_basis: string;
  hook_titles: HookTitleCandidate[];
  descriptions: DescriptionCandidate[];
  hashtags: string[];
  first_comment_text: string;
  detected_objects: DetectedObject[];
  frame_insights: FrameInsight[];
  upload_session_id: string;
  upload_expires_at: string;
  metadata: VideoMetadata;
  processing_notes: string[];
};

export type YouTubeAuthStatus = {
  connected: boolean;
  channel_title?: string | null;
  channel_id?: string | null;
};

export type GenerationJobStartResponse = {
  job_id: string;
  state: "queued" | "running" | "succeeded" | "failed";
};

export type GenerationJobStatusResponse = {
  job_id: string;
  state: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  detail?: string | null;
  progress_percent?: number | null;
  elapsed_ms: number;
  result?: GenerationResponse | null;
  error?: string | null;
};

export type YouTubePublishResponse = {
  video_id: string;
  video_url: string;
  studio_url: string;
  privacy_status: "private" | "unlisted" | "public";
  publish_at?: string | null;
  first_comment_posted: boolean;
  first_comment_id?: string | null;
  deleted_local_upload: boolean;
  applied_enhancements?: string[];
  publish_notes: string[];
};

export type YouTubePublishJobStartResponse = {
  job_id: string;
  state: "queued" | "running" | "succeeded" | "failed";
};

export type YouTubePublishJobStatusResponse = {
  job_id: string;
  state: "queued" | "running" | "succeeded" | "failed";
  stage: string;
  detail?: string | null;
  progress_percent?: number | null;
  uploaded_bytes?: number | null;
  total_bytes?: number | null;
  remaining_seconds?: number | null;
  elapsed_ms: number;
  result?: YouTubePublishResponse | null;
  error?: string | null;
};
