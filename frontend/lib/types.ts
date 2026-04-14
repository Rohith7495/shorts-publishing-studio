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
  thumbnail_text: string;
  thumbnail_timestamp_seconds?: number | null;
  first_comment_text: string;
  detected_objects: DetectedObject[];
  frame_insights: FrameInsight[];
  upload_session_id: string;
  upload_expires_at: string;
  thumbnail_preview_path: string;
  metadata: VideoMetadata;
  processing_notes: string[];
};

export type YouTubeAuthStatus = {
  connected: boolean;
  channel_title?: string | null;
  channel_id?: string | null;
};

export type YouTubePublishResponse = {
  video_id: string;
  video_url: string;
  studio_url: string;
  privacy_status: "private" | "unlisted" | "public";
  publish_at?: string | null;
  thumbnail_uploaded: boolean;
  first_comment_posted: boolean;
  first_comment_id?: string | null;
  deleted_local_upload: boolean;
  publish_notes: string[];
};
