import { api } from "./api";
import { useStore } from "./store";

let sock: WebSocket | null = null;
let retry = 0;
let streamsTimer: ReturnType<typeof setTimeout> | null = null;

function refreshStreamsSoon() {
  if (streamsTimer) return;
  streamsTimer = setTimeout(async () => {
    streamsTimer = null;
    try {
      useStore.getState().setStreams(await api.streams());
    } catch {
      /* backend restarting */
    }
  }, 300);
}

export async function bootstrap() {
  const s = useStore.getState();
  try {
    const [streams, clips, events, system, config] = await Promise.all([
      api.streams(), api.clips({}), api.hype(), api.status(), api.config(),
    ]);
    // seed live state from the REST snapshot so panels don't flash "Stopped" before the first WS tick
    const live = Object.fromEntries(streams.filter((x) => x.live).map((x) => [x.id, x.live!]));
    const meters = Object.fromEntries(streams.filter((x) => x.meters).map((x) => [x.id, x.meters!]));
    s.set({ streams, clips, events, system, config, live: { ...live, ...useStore.getState().live },
      meters: { ...meters, ...useStore.getState().meters },
      resources: (system.resources as any)?.cpu !== undefined ? (system.resources as any) : null });
    s.setJobs(system.jobs);
    for (const w of system.warnings) s.setWarning(w);
  } catch (e) {
    s.toast("err", `Backend not reachable: ${(e as Error).message}`);
  }
}

export function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  sock = new WebSocket(`${proto}://${location.host}/ws`);
  sock.onopen = () => {
    retry = 0;
    useStore.getState().set({ wsConnected: true });
    bootstrap();
  };
  sock.onclose = () => {
    useStore.getState().set({ wsConnected: false });
    retry = Math.min(retry + 1, 6);
    setTimeout(connectWS, 500 * 2 ** retry);
  };
  sock.onmessage = (ev) => {
    let msg: { type: string; data: any };
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    const s = useStore.getState();
    const d = msg.data;
    switch (msg.type) {
      case "hello":
        for (const w of d.warnings ?? []) s.setWarning(w);
        break;
      case "streams":
        s.setLive(d);
        break;
      case "meters":
        s.setMeters(d);
        break;
      case "resources":
        s.set({ resources: d });
        break;
      case "clip":
        s.upsertClip(d);
        break;
      case "vod":
        if (d.deleted) s.removeVod(d.id);
        else s.upsertVod(d);
        break;
      case "clip_deleted":
        s.removeClip(d.id);
        break;
      case "hype":
        s.addEvent(d);
        if (d.kind === "auto" && d.state === "open") {
          const st = s.streams.find((x) => x.id === d.streamer_id);
          s.toast("info", `Hype detected on ${st?.name ?? "stream"} (${Math.round((d.peak_score ?? 0) * 100)})`);
        }
        break;
      case "job":
        s.setJob({ id: d.id, kind: d.kind, ref_id: d.ref_id, state: d.state, streamer_id: d.streamer_id });
        break;
      case "warning":
        s.setWarning(d);
        break;
      case "warning_cleared":
        s.clearWarning(d.key);
        break;
      case "render_progress":
        s.set({ renderProgress: { ...s.renderProgress, [d.clip_id]: d.progress } });
        break;
      case "render_done": {
        const rp = { ...s.renderProgress };
        delete rp[d.clip_id];
        s.set({ renderProgress: rp });
        s.toast("ok", d.preview ? "Preview ready" : `Export ready: ${d.width}×${d.height} @ ${d.fps}fps`);
        break;
      }
      case "render_failed":
        s.toast("err", `Render failed: ${String(d.error).slice(0, 160)}`);
        break;
      case "session_started":
      case "session_stopped":
      case "stream_state":
        refreshStreamsSoon();
        break;
    }
  };
}
