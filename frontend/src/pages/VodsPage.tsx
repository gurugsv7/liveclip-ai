import { Clapperboard, FileVideo, History, Link2, X } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { ago, cn, dur } from "@/lib/format";
import { useStore } from "@/lib/store";
import type { VodJob, VodState } from "@/lib/types";
import { Badge, Button, Field, inputCls, PlatformIcon, Switch } from "@/components/ui";

const STATE: Record<VodState, { label: string; tone: string }> = {
  queued: { label: "Queued", tone: "text-ink-300" },
  waiting_vod: { label: "Waiting for VOD", tone: "text-amber-400" },
  downloading: { label: "Downloading audio", tone: "text-violet-400" },
  analyzing: { label: "Finding moments", tone: "text-violet-400" },
  cutting: { label: "Cutting clips", tone: "text-violet-400" },
  done: { label: "Done", tone: "text-ok" },
  failed: { label: "Failed", tone: "text-live" },
  cancelled: { label: "Cancelled", tone: "text-ink-400" },
};
const RUNNING: VodState[] = ["downloading", "analyzing", "cutting"];

/** Past broadcasts: paste a VOD link (Twitch / YouTube / Kick) or a video file path; the same
 *  detectors as live find the reel-worthy moments and only those stretches are downloaded. */
export function VodsPage() {
  const vods = useStore((s) => s.vods);
  const streams = useStore((s) => s.streams);
  const toast = useStore((s) => s.toast);
  const [src, setSrc] = useState("");
  const [streamer, setStreamer] = useState("");
  const [busy, setBusy] = useState(false);
  const [ytdlp, setYtdlp] = useState(true);

  useEffect(() => {
    api.vods().then((r) => {
      useStore.getState().set({ vods: r.jobs });
      setYtdlp(r.ytdlp);
    }).catch(() => undefined);
  }, []);

  const submit = async () => {
    const v = src.trim();
    if (!v) return;
    setBusy(true);
    try {
      const isUrl = /^https?:\/\//i.test(v);
      const job = await api.addVod({ ...(isUrl ? { url: v } : { path: v }), ...(streamer ? { streamer_id: streamer } : {}) });
      useStore.getState().upsertVod(job);
      setSrc("");
      toast("ok", "VOD queued — clips will appear in Review as they're found");
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const sorted = [...vods].sort((a, b) => b.created_at - a.created_at);

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="mx-auto w-full max-w-4xl space-y-6 px-4 py-6 sm:px-6">
        <section className="space-y-3 border border-ink-600 bg-ink-900 p-4">
          <h2 className="flex items-center gap-2 font-display text-xl font-extrabold"><History className="h-5 w-5 text-violet-400" /> Clip a past broadcast</h2>
          <p className="text-sm text-ink-300">
            Paste a VOD link or the full path of a video on this computer. Only the audio is downloaded to find the
            moments, then only those stretches of video — so a 9-hour stream costs about 1 GB, not 30. Live capture always
            keeps its bandwidth.
          </p>
          {!ytdlp && <p className="text-sm text-amber-400">yt-dlp isn't installed, so only local files work. Run the setup script again.</p>}
          <div className="grid gap-3 sm:grid-cols-[1fr_200px_auto] sm:items-end">
            <Field label="VOD link or file path">
              <input className={inputCls} value={src} onChange={(e) => setSrc(e.target.value)} onKeyDown={(e) => e.key === "Enter" && submit()}
                placeholder="https://www.twitch.tv/videos/…  or  C:\Videos\stream.mp4" aria-label="VOD link or file path" />
            </Field>
            <Field label="Streamer">
              <select className={inputCls} value={streamer} onChange={(e) => setStreamer(e.target.value)} aria-label="Streamer">
                <option value="">Match by channel</option>
                {streams.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </Field>
            <Button variant="primary" size="md" loading={busy} onClick={submit} disabled={!src.trim()}>Find moments</Button>
          </div>
          <p className="text-[11px] text-ink-400">
            The streamer's settings (language, facecam, captions) are used for the clips. Twitch and Kick VODs have no chat
            replay, so detection uses voice, sound and speech; YouTube replays include chat.
          </p>
        </section>

        <section className="space-y-2 border border-ink-600 bg-ink-900 p-4">
          <h3 className="font-display text-base font-extrabold">After each stream</h3>
          <p className="text-xs text-ink-400">When a monitored stream ends, analyse its whole past broadcast for moments missed live. Moments already clipped live are skipped.</p>
          <div className="divide-y divide-ink-750">
            {streams.length === 0 && <div className="py-2 text-xs text-ink-400">No streams added yet.</div>}
            {streams.map((s) => (
              <div key={s.id} className="flex items-center justify-between gap-3 py-2">
                <span className="flex items-center gap-2 text-sm"><PlatformIcon platform={s.platform} /> {s.name}</span>
                <Switch checked={!!s.vod_after_stream} label={`Analyse ${s.name}'s past broadcast after the stream`}
                  onChange={async (v) => {
                    try {
                      const up = await api.patchStream(s.id, { vod_after_stream: v });
                      useStore.getState().setStreams(useStore.getState().streams.map((x) => (x.id === s.id ? { ...x, ...up } : x)));
                    } catch (e) {
                      toast("err", (e as Error).message);
                    }
                  }} />
              </div>
            ))}
          </div>
        </section>

        <section className="space-y-2">
          <h3 className="label">Jobs</h3>
          {sorted.length === 0 && <div className="border border-dashed border-ink-600 p-6 text-center text-sm text-ink-400">No VODs yet.</div>}
          {sorted.map((j) => <VodRow key={j.id} j={j} />)}
        </section>
      </div>
    </div>
  );
}

function VodRow({ j }: { j: VodJob }) {
  const toast = useStore((s) => s.toast);
  const setS = useStore((s) => s.set);
  const st = STATE[j.state];
  const running = RUNNING.includes(j.state);
  const open = running || j.state === "queued" || j.state === "waiting_vod";
  return (
    <div className="border border-ink-600 bg-ink-900 p-3">
      <div className="flex items-start gap-3">
        <div className="mt-0.5">{j.platform === "local" ? <FileVideo className="h-4 w-4 text-ink-300" /> : <PlatformIcon platform={j.platform} className="h-4 w-4" />}</div>
        <div className="min-w-0 flex-1">
          <div className="line-clamp-2 text-sm font-semibold">{j.title || j.url || j.path}</div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[10px] text-ink-400">
            {j.streamer_name && <span style={{ color: j.streamer_color ?? undefined }}>{j.streamer_name}</span>}
            <Badge className={st.tone}>{st.label}</Badge>
            {j.duration ? <span>{dur(j.duration)}</span> : null}
            {j.source === "after_stream" && <span>after stream</span>}
            <span>{ago(j.created_at)}</span>
            {j.url && <a className="inline-flex items-center gap-1 text-violet-400 hover:underline" href={j.url} target="_blank" rel="noreferrer"><Link2 className="h-3 w-3" />source</a>}
          </div>
        </div>
        <div className="flex shrink-0 gap-1.5">
          {j.clips > 0 && <Button size="xs" onClick={() => setS({ page: "review" })}><Clapperboard className="h-3 w-3" /> {j.clips} clip{j.clips === 1 ? "" : "s"}</Button>}
          {open ? (
            <Button size="xs" variant="ghost" onClick={() => api.cancelVod(j.id).catch((e) => toast("err", e.message))}>Cancel</Button>
          ) : (
            <Button size="xs" variant="ghost" aria-label="Remove from list" onClick={() => api.removeVod(j.id).then(() => useStore.getState().removeVod(j.id)).catch((e) => toast("err", e.message))}><X className="h-3 w-3" /></Button>
          )}
        </div>
      </div>
      {(running || j.state === "done") && (
        <div className="mt-2 h-1.5 w-full bg-ink-750" role="progressbar" aria-valuenow={Math.round(j.progress * 100)} aria-valuemin={0} aria-valuemax={100}>
          <div className={cn("h-full", j.state === "done" ? "bg-ok" : "bg-violet-500")} style={{ width: `${Math.round(j.progress * 100)}%` }} />
        </div>
      )}
      {(j.detail || j.error) && (
        <div className={cn("mt-1.5 text-xs", j.error ? "text-live" : "text-ink-300")}>
          {j.error || j.detail}{j.state !== "done" && j.moments > 0 ? ` · ${j.moments} moments` : ""}
        </div>
      )}
    </div>
  );
}
