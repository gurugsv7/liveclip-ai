import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

export const cn = (...c: ClassValue[]) => twMerge(clsx(c));

export function bytes(n: number | null | undefined): string {
  if (n == null) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

export function dur(s: number | null | undefined): string {
  if (s == null || !isFinite(s)) return "—";
  s = Math.max(0, s);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = Math.floor(s % 60);
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}

export function ago(ts: number): string {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return `${Math.max(0, Math.floor(d))}s ago`;
  if (d < 3600) return `${Math.floor(d / 60)}m ago`;
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

export function qualityLabel(_w: number | null, h: number | null, fps: number | null): string {
  if (!h) return "—";
  return `${h}p${fps && fps > 31 ? Math.round(fps) : ""}`;
}

export const STATE_LABEL: Record<string, string> = {
  starting: "Starting", connecting: "Connecting", live: "Live", reconnecting: "Reconnecting",
  offline: "Offline", failed: "Failed", stopped: "Stopped", error: "Error",
};

export const CLIP_STATE: Record<string, { label: string; tone: string }> = {
  detected: { label: "Detected", tone: "text-volt-400 bg-volt-500/10 border-volt-500/30" },
  protected: { label: "Protected", tone: "text-violet-400 bg-violet-500/10 border-violet-500/30" },
  processing: { label: "Processing", tone: "text-volt-400 bg-volt-500/10 border-volt-500/30" },
  ready_for_review: { label: "Ready for review", tone: "text-warn bg-warn/10 border-warn/30" },
  maybe: { label: "Maybe", tone: "text-warn bg-warn/10 border-warn/30" },
  approved: { label: "Approved", tone: "text-ok bg-ok/10 border-ok/30" },
  rejected: { label: "Rejected", tone: "text-ink-400 bg-ink-700/40 border-ink-600" },
  exported: { label: "Exported", tone: "text-ok bg-ok/10 border-ok/30" },
  failed: { label: "Failed", tone: "text-live bg-live/10 border-live/30" },
  discarded: { label: "Discarded", tone: "text-ink-400 bg-ink-700/40 border-ink-600" },
};

export const METHOD_LABEL: Record<string, string> = { auto: "AI detected", manual_save: "Manual save", mark: "Marked", viewer_clip: "Viewer clip" };
