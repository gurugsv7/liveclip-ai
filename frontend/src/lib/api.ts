import type { AppConfig, Clip, HypeEvent, Job, Streamer, SystemStatus, VodJob } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function req<T>(method: string, url: string, body?: unknown): Promise<T> {
  const r = await fetch(url, {
    method,
    headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    let msg = r.statusText;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* not json */
    }
    throw new ApiError(r.status, msg);
  }
  return (await r.json()) as T;
}

export const api = {
  status: () => req<SystemStatus>("GET", "/api/system/status"),
  config: () => req<AppConfig>("GET", "/api/config"),
  putConfig: (patch: AppConfig) => req<AppConfig>("PUT", "/api/config", patch),
  aiCheck: () => req<any>("POST", "/api/ai/check"),
  aiWarmup: () => req<any>("POST", "/api/ai/warmup"),

  streams: () => req<Streamer[]>("GET", "/api/streams"),
  addStream: (b: { name: string; url: string; slot?: number; priority?: number; start?: boolean }) =>
    req<Streamer>("POST", "/api/streams", b),
  patchStream: (id: string, b: Partial<Streamer> & { clear_facecam?: boolean }) => req<Streamer>("PATCH", `/api/streams/${id}`, b),
  moveStream: (id: string, slot: number) => req<Streamer[]>("POST", `/api/streams/${id}/move`, { slot }),
  removeStream: (id: string) => req<{ ok: boolean }>("DELETE", `/api/streams/${id}`),
  start: (id: string) => req<any>("POST", `/api/streams/${id}/start`),
  stop: (id: string) => req<any>("POST", `/api/streams/${id}/stop`),
  startAll: () => req<any>("POST", "/api/streams/start-all"),
  stopAll: () => req<any>("POST", "/api/streams/stop-all"),
  replace: (id: string, url: string, name?: string) => req<any>("POST", `/api/streams/${id}/replace`, { url, name }),
  save: (id: string, seconds: number, latency_s = 0) => req<{ clip_id: string }>("POST", `/api/streams/${id}/save`, { seconds, latency_s }),
  mark: (id: string, latency_s = 0) => req<{ clip_id: string }>("POST", `/api/streams/${id}/mark`, { latency_s, analyze: true }),
  deleteBuffer: (id: string) =>
    req<{ deleted: number; protected_kept: number }>("POST", `/api/streams/${id}/delete-buffer`, { confirm: true }),
  setAI: (id: string, enabled: boolean) => req<any>("POST", `/api/streams/${id}/ai`, { enabled }),

  vods: () => req<{ jobs: VodJob[]; ytdlp: boolean }>("GET", "/api/vods"),
  addVod: (b: { url?: string; path?: string; streamer_id?: string }) => req<VodJob>("POST", "/api/vods", b),
  cancelVod: (id: string) => req<VodJob>("POST", `/api/vods/${id}/cancel`),
  removeVod: (id: string) => req<{ ok: boolean }>("DELETE", `/api/vods/${id}`),

  hype: (streamer_id?: string) => req<HypeEvent[]>("GET", `/api/hype${streamer_id ? `?streamer_id=${streamer_id}` : ""}`),
  timeline: () => req<Record<string, any>>("GET", "/api/timeline"),

  clips: (p: { streamer_id?: string; status?: string; sort?: "date" | "score" } = {}) => {
    const q = new URLSearchParams(Object.entries(p).filter(([, v]) => v) as [string, string][]);
    return req<Clip[]>("GET", `/api/clips?${q}`);
  },
  clip: (id: string) => req<Clip>("GET", `/api/clips/${id}`),
  patchClip: (id: string, b: Partial<Clip>) => req<Clip>("PATCH", `/api/clips/${id}`, b),
  approve: (id: string) => req<Clip>("POST", `/api/clips/${id}/approve`),
  reject: (id: string) => req<Clip>("POST", `/api/clips/${id}/reject`),
  deleteClip: (id: string) => req<{ ok: boolean }>("DELETE", `/api/clips/${id}?confirm=true`),
  renderPreview: (id: string) => req<{ job_id: string }>("POST", `/api/clips/${id}/render-preview`),
  exportClip: (id: string) => req<{ job_id: string }>("POST", `/api/clips/${id}/export`),
  retranscribe: (id: string) => req<{ job_id: string }>("POST", `/api/clips/${id}/retranscribe`),
  detectFacecam: (id: string) => req<{ facecam: any; detail?: string }>("POST", `/api/clips/${id}/detect-facecam`),
  openFolder: (id: string, which: "export" | "source" = "export") => req<any>("POST", `/api/clips/${id}/open-folder?which=${which}`),
  publish: (id: string, target: "youtube" | "instagram", description = "") =>
    req<any>("POST", `/api/clips/${id}/publish`, { target, description }),
  publishing: () => req<any>("GET", "/api/publishing"),
  ytAuthorize: () => req<any>("POST", "/api/publishing/youtube/authorize"),

  jobs: () => req<Job[]>("GET", "/api/jobs"),
  storage: () => req<any>("GET", "/api/storage"),
  cleanup: () => req<any>("POST", "/api/storage/cleanup", { confirm: true }),
};

export const media = {
  live: (sessionId: string) => `/media/live/${sessionId}/index.m3u8`,
  clip: (id: string, kind: "source" | "preview" | "export" | "thumb" | "srt" | "cover", bust?: number) =>
    `/media/clips/${id}/${kind}${bust ? `?v=${bust}` : ""}`,
};
