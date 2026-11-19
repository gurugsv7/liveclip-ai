import { create } from "zustand";
import type { AppConfig, Clip, HypeEvent, Job, LiveStatus, Meters, Resources, Streamer, SystemStatus, VodJob, Warning } from "./types";

export type Page = "live" | "clips" | "review" | "vods" | "settings";

export interface Toast { id: number; kind: "ok" | "err" | "info"; text: string }

interface State {
  page: Page;
  streams: Streamer[];
  live: Record<string, LiveStatus>;
  meters: Record<string, Meters>;
  clips: Clip[];
  events: HypeEvent[];
  jobs: Record<string, Job>;
  warnings: Record<string, Warning>;
  resources: Resources | null;
  system: SystemStatus | null;
  config: AppConfig | null;
  wsConnected: boolean;
  selected: number;              // slot 0..3
  audible: string[];             // streamer ids audible in the dashboard
  volumes: Record<string, number>; // per-stream dashboard volume 0..1 (this browser only)
  expanded: string | null;
  sidebarCollapsed: boolean;
  timelineOpen: boolean | null;
  fullscreen: boolean;
  pausedPreview: Record<string, boolean>;
  latency: Record<string, number>;   // seconds behind newest captured footage
  renderProgress: Record<string, number>;
  focusClip: string | null;
  toasts: Toast[];
  vods: VodJob[];

  setPage: (p: Page) => void;
  setStreams: (s: Streamer[]) => void;
  setLive: (m: Record<string, LiveStatus>) => void;
  setMeters: (m: Record<string, Meters>) => void;
  upsertClip: (c: Clip) => void;
  removeClip: (id: string) => void;
  upsertVod: (v: VodJob) => void;
  removeVod: (id: string) => void;
  setClips: (c: Clip[]) => void;
  addEvent: (e: HypeEvent) => void;
  setEvents: (e: HypeEvent[]) => void;
  setJob: (j: Job) => void;
  setJobs: (j: Job[]) => void;
  setWarning: (w: Warning) => void;
  clearWarning: (k: string) => void;
  set: (p: Partial<State>) => void;
  toggleAudio: (id: string, allowMulti: boolean) => void;
  setVolume: (id: string, v: number, allowMulti: boolean) => void;
  toast: (kind: Toast["kind"], text: string) => void;
  dismissToast: (id: number) => void;
}

let toastId = 1;
const VOL_KEY = "liveclip.volumes";

function loadVolumes(): Record<string, number> {
  try {
    const v = JSON.parse(localStorage.getItem(VOL_KEY) || "{}");
    return v && typeof v === "object" ? v : {};
  } catch {
    return {};
  }
}

export const useStore = create<State>((set, get) => ({
  page: "live",
  streams: [],
  live: {},
  meters: {},
  clips: [],
  events: [],
  jobs: {},
  warnings: {},
  resources: null,
  system: null,
  config: null,
  wsConnected: false,
  selected: 0,
  audible: [],
  volumes: loadVolumes(),
  expanded: null,
  sidebarCollapsed: false,
  timelineOpen: null,
  fullscreen: false,
  pausedPreview: {},
  latency: {},
  renderProgress: {},
  focusClip: null,
  toasts: [],
  vods: [],

  setPage: (page) => set({ page }),
  setStreams: (streams) => set({ streams }),
  setLive: (live) => set({ live }),
  setMeters: (meters) => set({ meters }),
  upsertClip: (c) =>
    set((s) => {
      if (c.status === "discarded") return { clips: s.clips.filter((x) => x.id !== c.id) };
      const i = s.clips.findIndex((x) => x.id === c.id);
      if (i === -1) return { clips: [c, ...s.clips] };
      const next = s.clips.slice();
      next[i] = c;
      return { clips: next };
    }),
  removeClip: (id) => set((s) => ({ clips: s.clips.filter((c) => c.id !== id) })),
  upsertVod: (v) =>
    set((s) => {
      const i = s.vods.findIndex((x) => x.id === v.id);
      if (i === -1) return { vods: [v, ...s.vods] };
      const next = s.vods.slice();
      next[i] = v;
      return { vods: next };
    }),
  removeVod: (id) => set((s) => ({ vods: s.vods.filter((v) => v.id !== id) })),
  setClips: (clips) => set({ clips }),
  addEvent: (e) =>
    set((s) => {
      const rest = s.events.filter((x) => x.id !== e.id);
      return { events: [e, ...rest].slice(0, 300) };
    }),
  setEvents: (events) => set({ events }),
  setJob: (j) =>
    set((s) => {
      const jobs = { ...s.jobs, [j.id]: { ...s.jobs[j.id], ...j } };
      if (j.state === "done" || j.state === "failed") {
        setTimeout(() => useStore.setState((st) => {
          const n = { ...st.jobs };
          delete n[j.id];
          return { jobs: n };
        }), 4000);
      }
      return { jobs };
    }),
  setJobs: (list) => set({ jobs: Object.fromEntries(list.map((j) => [j.id, j])) }),
  setWarning: (w) => set((s) => ({ warnings: { ...s.warnings, [w.key]: w } })),
  clearWarning: (k) =>
    set((s) => {
      const n = { ...s.warnings };
      delete n[k];
      return { warnings: n };
    }),
  set: (p) => set(p),
  toggleAudio: (id, allowMulti) => {
    const cur = get().audible;
    if (allowMulti) {
      set({ audible: cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id] });
    } else {
      // single-stream monitoring: clicking the active speaker mutes all
      set({ audible: cur.length === 1 && cur[0] === id ? [] : [id] });
    }
  },
  setVolume: (id, v, allowMulti) => {
    const volumes = { ...get().volumes, [id]: Math.max(0, Math.min(1, v)) };
    try {
      localStorage.setItem(VOL_KEY, JSON.stringify(volumes));
    } catch {
      /* private window: keep it for this session only */
    }
    const cur = get().audible;
    // turning a muted stream's volume up means "I want to hear it"
    const audible = v > 0 && !cur.includes(id) ? (allowMulti ? [...cur, id] : [id]) : cur;
    set({ volumes, audible });
  },
  toast: (kind, text) => {
    const id = toastId++;
    set((s) => ({ toasts: [...s.toasts, { id, kind, text }].slice(-5) }));
    setTimeout(() => get().dismissToast(id), kind === "err" ? 7000 : 3500);
  },
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));
