export type Platform = "twitch" | "youtube" | "kick" | "local";
export type Layout = "smart_crop" | "facecam" | "letterbox";
export type Rect = { x: number; y: number; w: number; h: number; source?: string };

export interface BufferStatus {
  segments: number;
  bytes: number;
  t_first: number | null;
  t_last: number | null;
  seconds: number;
  protected: { id: string; t_start: number; t_end: number; reason: string }[];
  protected_segments: number;
}

export interface LiveStatus {
  streamer_id: string;
  session_id: string;
  state: "starting" | "connecting" | "live" | "reconnecting" | "offline" | "failed" | "stopped" | "error";
  detail: string;
  platform: Platform;
  url: string;
  width: number | null;
  height: number | null;
  fps: number | null;
  video_codec: string | null;
  audio_codec: string | null;
  audio_rate: number | null;
  measured_kbps: number | null;
  quality_requested: string[];
  quality_selected: string | null;
  reconnects: number;
  recording_s: number;
  media_now: number;
  buffer: BufferStatus;
  ai_enabled: boolean;
  chat: { platform: string; status: string; detail: string; messages: number };
  chat_messages: number;
}

export interface Meters {
  audio: number;
  chat: number | null;
  chat_rate: number;
  speech?: number;
  speech_text?: string | null;
  reaction?: number;
  sound_top?: string | null;
  game_noise?: number | null;
  clip_calls?: number;
  score: number;
  level_db: number;
  open: boolean;
  suppressed: number;
}

export interface Streamer {
  id: string;
  slug: string;
  name: string;
  platform: Platform;
  url: string;
  slot: number;
  priority: number;
  color: string;
  ai_enabled: boolean;
  facecam: Rect | null;
  default_layout: Layout;
  usage_notes: string;
  live: LiveStatus | null;
  meters: Meters | null;
}

export type ClipStatus =
  | "detected" | "protected" | "processing" | "ready_for_review" | "maybe" | "approved" | "rejected" | "exported" | "failed" | "discarded";

export interface Word { text: string; start: number; end: number; prob?: number }

export interface Clip {
  id: string;
  streamer_id: string;
  streamer_name: string;
  streamer_slug: string;
  streamer_color: string;
  session_id: string;
  hype_event_id: string | null;
  platform: Platform;
  source_url: string;
  t_start: number;
  t_end: number;
  wall_start: number | null;
  detection_method: "auto" | "manual_save" | "mark" | "viewer_clip";
  ai_score: number | null;
  ai_reason: string | null;
  duration: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  file_path: string | null;
  file_size: number | null;
  thumb_path: string | null;
  status: ClipStatus;
  export_status: "none" | "queued" | "rendering" | "done" | "failed";
  title: string | null;
  suggested_titles: string[] | null;
  caption_text: string | null;
  tags: string[] | null;
  layout: Layout;
  layout_params: { peak?: number | null; facecam?: Rect; cold_open?: boolean; punch_zoom?: boolean; normalize_audio?: boolean } | null;
  caption_words: Word[] | null;
  caption_style: { style?: string; size?: number; y?: number; uppercase?: boolean; hook?: boolean; enabled?: boolean; fps?: number } | null;
  trim_start: number;
  trim_end: number | null;
  preview_path: string | null;
  export_path: string | null;
  publish_status: string | null;
  error: string | null;
  created_at: number;
  updated_at: number;
  transcript: { text: string; words: Word[]; model: string; language: string } | null;
  files: { file_path: boolean; thumb_path: boolean; preview_path: boolean; export_path: boolean };
}

export interface HypeEvent {
  id: string;
  session_id: string;
  streamer_id: string;
  kind: "auto" | "manual_save" | "mark";
  t_event: number;
  peak_score: number;
  audio_score: number | null;
  chat_score: number | null;
  final_score: number | null;
  state: string;
  reason: string | null;
  merged_count?: number;
  clip_id: string | null;
  created_at: number;
}

export interface Warning { key: string; level: "warn" | "error"; message: string; ts: number }

export interface Resources {
  cpu: number;
  ram_used: number;
  ram_total: number;
  ram_percent: number;
  app_rss: number;
  net_rx_mbps: number | null;
  gpu: { name: string; util: number; vram_used_mb: number; vram_total_mb: number; temp_c: number; enc: number | null; dec: number | null } | null;
  disk: { total: number; free: number; temp_bytes: number; clip_bytes: number; export_bytes: number; temp_budget: number; level: string };
  bandwidth: { used_mbps: number; budget_mbps: number; over_budget: boolean };
  jobs_running: Record<string, number>;
}

export interface Job {
  id: string;
  kind: string;
  ref_id: string;
  streamer_id: string | null;
  state: string;
  priority?: number;
  attempts?: number;
  error?: string | null;
}

export interface SystemStatus {
  tools: { ffmpeg: string | null; ffprobe: string | null; streamlink: string | null };
  resources: Resources | Record<string, never>;
  whisper: { status: string; detail: string; device: string | null; model: string | null; jobs_done: number; last_ms: number | null };
  semantic: { status: string; detail: string };
  warnings: Warning[];
  bandwidth: { used_mbps: number; budget_mbps: number; over_budget: boolean };
  jobs: Job[];
  active_streams: number;
  data_dir: string;
  allow_local_sources: boolean;
}

export type AppConfig = Record<string, any>;
