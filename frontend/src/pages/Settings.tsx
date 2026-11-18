import { Save, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { bytes, cn } from "@/lib/format";
import { useStore } from "@/lib/store";
import type { Streamer } from "@/lib/types";
import { useAction } from "@/components/StreamPanel";
import { Button, Confirm, Field, inputCls, PlatformIcon, Slider, Switch } from "@/components/ui";

const SECTIONS = ["Streams", "Detection", "Clips & buffer", "Storage", "Network", "AI models", "Export", "Publishing", "Shortcuts"] as const;
type Sec = (typeof SECTIONS)[number];

function get(o: any, path: string) {
  return path.split(".").reduce((a, k) => a?.[k], o);
}
function setPath(o: any, path: string, v: unknown) {
  const keys = path.split(".");
  const out = structuredClone(o);
  let cur = out;
  keys.slice(0, -1).forEach((k) => (cur = cur[k]));
  cur[keys[keys.length - 1]] = v;
  return out;
}

export function SettingsPage() {
  const config = useStore((s) => s.config);
  const [sec, setSec] = useState<Sec>("Streams");
  const [draft, setDraft] = useState<any>(config);
  const { busy, run } = useAction();
  useEffect(() => setDraft(config), [config]);
  if (!draft) return null;
  const dirty = JSON.stringify(draft) !== JSON.stringify(config);
  const num = (path: string, label: string, opts: { step?: number; min?: number; max?: number; hint?: string; scale?: number; unit?: string } = {}) => {
    const sc = opts.scale ?? 1;
    return (
      <Field label={label} hint={opts.hint}>
        <div className="flex items-center gap-2">
          <input type="number" className={inputCls} step={opts.step ?? 1} min={opts.min} max={opts.max} value={+(get(draft, path) / sc).toFixed(3)}
            onChange={(e) => setDraft(setPath(draft, path, Number(e.target.value) * sc))} />
          {opts.unit && <span className="text-xs text-ink-400">{opts.unit}</span>}
        </div>
      </Field>
    );
  };
  const sw = (path: string, label: string, hint?: string) => (
    <label className="flex items-start gap-3 py-1">
      <Switch checked={!!get(draft, path)} onChange={(v) => setDraft(setPath(draft, path, v))} label={label} />
      <span><span className="text-sm text-ink-100">{label}</span>{hint && <span className="block text-[11px] text-ink-400">{hint}</span>}</span>
    </label>
  );
  const save = () => run("save", async () => useStore.getState().set({ config: await api.putConfig(draft) }), "Settings saved");

  return (
    <div className="flex min-h-0 flex-1 flex-col md:flex-row">
      <nav aria-label="Settings sections" className="flex shrink-0 flex-wrap gap-1 border-b border-ink-600 p-3 md:block md:w-52 md:space-y-0.5 md:overflow-y-auto md:border-b-0 md:border-r xl:p-5">
        <h1 className="mb-3 hidden font-display text-[32px] font-black tracking-tight md:block">Settings</h1>
        {SECTIONS.map((s) => (
          <button key={s} onClick={() => setSec(s)} aria-current={sec === s ? "true" : undefined} className={cn("focus-ring block border-l-2 px-2.5 py-1.5 text-left text-sm md:w-full",
            sec === s ? "border-violet-500 bg-ink-800 font-semibold text-ink-100" : "border-transparent text-ink-400 hover:bg-ink-800 hover:text-ink-100")}>{s}</button>
        ))}
      </nav>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl space-y-5 p-6">
          <div className="flex items-center justify-between gap-2 border-b border-ink-600 pb-3">
            <h2 className="font-display text-[33px] font-black leading-none tracking-tight xl:text-[42px]">{sec}</h2>
            {sec !== "Streams" && sec !== "Storage" && (
              <div className="flex gap-2">
                {dirty && <Button variant="ghost" onClick={() => setDraft(config)}>Discard</Button>}
                <Button variant="primary" size="md" disabled={!dirty} loading={busy === "save"} onClick={save}><Save className="h-4 w-4" />Save</Button>
              </div>
            )}
          </div>

          {sec === "Streams" && <StreamsSettings />}
          {sec === "Detection" && (
            <div className="space-y-5">
              <div className="grid grid-cols-3 gap-3">
                {num("detection.trigger_threshold", "Trigger threshold", { step: 0.01, min: 0.05, max: 1, hint: "Lightweight score that opens a candidate" })}
                {num("detection.release_threshold", "Release threshold", { step: 0.01, min: 0, max: 1, hint: "Reaction considered over below this" })}
                {num("detection.save_threshold", "Save threshold", { step: 0.01, min: 0, max: 1, hint: "Auto clips below this are discarded" })}
                {num("detection.merge_window_s", "Merge window", { unit: "s", hint: "Near-duplicate triggers merge" })}
                {num("detection.cooldown_s", "Cooldown", { unit: "s" })}
                {num("detection.min_baseline_s", "Baseline warm-up", { unit: "s", hint: "Per-streamer normalization" })}
                {num("detection.chat_reaction_delay_s", "Chat reaction delay", { step: 0.5, unit: "s" })}
                {num("detection.lightweight_audio_weight", "Layer A: audio weight", { step: 0.05 })}
                {num("detection.lightweight_chat_weight", "Layer A: chat weight", { step: 0.05 })}
                {num("detection.single_signal_factor", "Single-signal factor", { step: 0.05, min: 0, max: 1, hint: "A lone strong chat or audio spike counts at this fraction" })}
              </div>
              <div className="panel space-y-3 p-4">
                <div className="label">Layer B scoring weights</div>
                {Object.keys(draft.detection.weights).map((k) => (
                  <div key={k} className="grid grid-cols-[120px_1fr_44px] items-center gap-3 text-sm">
                    <span className="capitalize text-ink-200">{k}</span>
                    <Slider value={[draft.detection.weights[k]]} min={0} max={1} step={0.05} label={`${k} weight`}
                      onChange={(v) => setDraft(setPath(draft, `detection.weights.${k}`, v[0]))} />
                    <span className="tabular text-right font-mono text-xs text-ink-300">{draft.detection.weights[k].toFixed(2)}</span>
                  </div>
                ))}
                <div className="text-[11px] text-ink-400">Signals that are unavailable (e.g. chat without API access, LLM disabled) are excluded and the rest are re-weighted.</div>
              </div>
              <Field label="Chat excitement keywords" hint="Comma separated, case-insensitive">
                <textarea className={cn(inputCls, "h-20 py-1.5")} value={draft.detection.keywords.join(", ")}
                  onChange={(e) => setDraft(setPath(draft, "detection.keywords", e.target.value.split(",").map((x) => x.trim()).filter(Boolean)))} />
              </Field>
            </div>
          )}
          {sec === "Clips & buffer" && (
            <div className="grid grid-cols-3 gap-3">
              {num("buffer.retention_seconds", "Rolling buffer", { unit: "s", min: 30, max: 600 })}
              {num("buffer.segment_seconds", "Segment length", { unit: "s", step: 0.5, min: 2, max: 10, hint: "Applies to newly started streams" })}
              {num("clips.pre_context_s", "Pre-event context", { unit: "s" })}
              {num("clips.post_context_s", "Post-event context", { unit: "s" })}
              {num("clips.max_clip_s", "Max clip length", { unit: "s" })}
              {num("clips.max_reaction_wait_s", "Max reaction wait", { unit: "s" })}
              <div className="col-span-3">{sw("clips.keep_discarded_auto_clips", "Keep discarded AI clips", "Off: low-scoring auto candidates are deleted after scoring")}</div>
              <div className="col-span-3">{sw("audio_monitor.allow_multi", "Allow multiple audible streams", "On: hear several streams at once, each with its own volume bar. Off: unmuting one stream mutes the others")}</div>
            </div>
          )}
          {sec === "Storage" && <StorageSettings draft={draft} setDraft={setDraft} num={num} save={save} dirty={dirty} busy={busy === "save"} />}
          {sec === "Network" && (
            <div className="space-y-3">
              <div className="grid grid-cols-3 gap-3">
                {num("network.link_capacity_mbps", "Internet download", { unit: "Mbps" })}
                {num("network.safety_fraction", "Capture budget", { step: 0.05, min: 0.1, max: 1, hint: "Fraction of link capture may use" })}
                {num("network.reconnect_max_attempts", "Reconnect attempts")}
                {num("network.reconnect_base_s", "Backoff base", { unit: "s", step: 0.5 })}
                {num("network.reconnect_max_s", "Backoff max", { unit: "s" })}
              </div>
              <Field label="Preferred quality order" hint="Streamlink names, first available wins">
                <input className={inputCls} value={draft.network.preferred_quality.join(",")} onChange={(e) => setDraft(setPath(draft, "network.preferred_quality", e.target.value.split(",").map((x) => x.trim()).filter(Boolean)))} />
              </Field>
              <Field label="Reduced quality order" hint="Used for lower-priority streams when the budget is exceeded">
                <input className={inputCls} value={draft.network.reduced_quality.join(",")} onChange={(e) => setDraft(setPath(draft, "network.reduced_quality", e.target.value.split(",").map((x) => x.trim()).filter(Boolean)))} />
              </Field>
              <div className="rounded-md border border-ink-700 p-3 text-xs text-ink-300">The dashboard preview reuses the captured segments, so watching all four streams adds no internet traffic.</div>
            </div>
          )}
          {sec === "AI models" && <AISettings draft={draft} setDraft={setDraft} sw={sw} num={num} />}
          {sec === "Export" && (
            <div className="grid grid-cols-3 gap-3">
              <Field label="Default FPS"><select className={inputCls} value={draft.render.fps} onChange={(e) => setDraft(setPath(draft, "render.fps", +e.target.value))}><option value={30}>30</option><option value={60}>60</option></select></Field>
              <Field label="Encoder"><select className={inputCls} value={draft.render.encoder} onChange={(e) => setDraft(setPath(draft, "render.encoder", e.target.value))}><option value="auto">Auto (NVENC if available)</option><option value="nvenc">NVENC</option><option value="x264">x264 (CPU)</option></select></Field>
              {num("render.nvenc_cq", "NVENC CQ", { hint: "Lower = higher quality" })}
              {num("render.x264_crf", "x264 CRF")}
              {num("render.max_bitrate_mbps", "Max bitrate", { unit: "Mbps" })}
              {num("render.audio_bitrate_k", "Audio bitrate", { unit: "kbps" })}
              <Field label="Caption font" hint="Installed font family name"><input className={inputCls} value={draft.render.caption_font} onChange={(e) => setDraft(setPath(draft, "render.caption_font", e.target.value))} /></Field>
              <Field label="Extra fonts folder"><input className={inputCls} value={draft.render.caption_fonts_dir} onChange={(e) => setDraft(setPath(draft, "render.caption_fonts_dir", e.target.value))} placeholder="C:\\Fonts\\brand" /></Field>
              <Field label="Default caption style"><select className={inputCls} value={draft.render.caption_style} onChange={(e) => setDraft(setPath(draft, "render.caption_style", e.target.value))}>{["bold_pop", "box", "neon", "clean"].map((s) => <option key={s}>{s}</option>)}</select></Field>
              {num("render.caption_size", "Caption size", { unit: "px" })}
              {num("render.caption_y", "Caption position", { step: 0.01, hint: "Fraction of height" })}
              {num("render.hook_seconds", "Hook title duration", { step: 0.1, unit: "s" })}
              {num("render.max_reel_s", "Max reel length", { unit: "s", min: 10, max: 90, hint: "Longer clips are auto-cut to the best moment (house rule: 30 s)" })}
              <div className="col-span-3">{sw("render.hook_text_enabled", "Show title as hook text from frame 1")}</div>
            </div>
          )}
          {sec === "Publishing" && <PublishingSettings draft={draft} setDraft={setDraft} />}
          {sec === "Shortcuts" && (
            <div className="grid grid-cols-3 gap-3">
              {Object.entries(draft.shortcuts as Record<string, string>).map(([k, v]) => (
                <Field key={k} label={k}><input className={cn(inputCls, "font-mono")} maxLength={1} value={v} onChange={(e) => setDraft(setPath(draft, `shortcuts.${k}`, e.target.value.toLowerCase()))} /></Field>
              ))}
              <div className="col-span-3 text-[11px] text-ink-400">Shortcuts are ignored while typing in a text field. F toggles fullscreen, Esc leaves expanded view.</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function StreamsSettings() {
  const streams = useStore((s) => s.streams);
  const { run } = useAction();
  const [del, setDel] = useState<Streamer | null>(null);
  const refresh = async () => useStore.getState().setStreams(await api.streams());
  return (
    <div className="space-y-2">
      {streams.length === 0 && <div className="text-sm text-ink-400">No streams yet. Add them from the Live Monitor's empty slots.</div>}
      {streams.map((s) => (
        <div key={s.id} className="panel grid grid-cols-[auto_1fr_auto] items-start gap-3 p-3">
          <input type="color" value={s.color} aria-label="Stream colour" className="h-8 w-8 cursor-pointer rounded border border-ink-600 bg-transparent"
            onChange={(e) => run("c", () => api.patchStream(s.id, { color: e.target.value }).then(refresh))} />
          <div className="min-w-0 space-y-2">
            <div className="flex items-center gap-2"><PlatformIcon platform={s.platform} /><span className="font-medium">{s.name}</span><span className="truncate text-xs text-ink-400">{s.url}</span></div>
            <div className="grid grid-cols-3 gap-2">
              <Field label="Priority"><select className={inputCls} value={s.priority} onChange={(e) => run("p", () => api.patchStream(s.id, { priority: +e.target.value }).then(refresh))}>{[1, 2, 3, 4].map((p) => <option key={p}>{p}</option>)}</select></Field>
              <Field label="Default layout"><select className={inputCls} value={s.default_layout} onChange={(e) => run("l", () => api.patchStream(s.id, { default_layout: e.target.value as any }).then(refresh))}><option value="smart_crop">Smart crop</option><option value="facecam">Facecam + gameplay</option><option value="letterbox">Original</option></select></Field>
              <Field label="Facecam">
                <div className="flex h-8 items-center gap-2 text-xs text-ink-300">{s.facecam ? `${Math.round(s.facecam.w * 100)}×${Math.round(s.facecam.h * 100)}% at ${Math.round(s.facecam.x * 100)},${Math.round(s.facecam.y * 100)}` : "not set"}
                  {s.facecam && <button className="text-violet-400 hover:underline" onClick={() => run("fc", () => api.patchStream(s.id, { clear_facecam: true }).then(refresh))}>clear</button>}</div>
              </Field>
            </div>
            <Field label="Permissions / usage notes" hint="Track permission to clip this creator and any usage restrictions">
              <input className={inputCls} defaultValue={s.usage_notes} onBlur={(e) => e.target.value !== s.usage_notes && run("n", () => api.patchStream(s.id, { usage_notes: e.target.value }).then(refresh))} />
            </Field>
          </div>
          <Button variant="danger" size="sm" onClick={() => setDel(s)}><Trash2 className="h-3.5 w-3.5" />Remove</Button>
        </div>
      ))}
      <Confirm open={!!del} onOpenChange={(o) => !o && setDel(null)} title={`Remove ${del?.name}?`} confirmLabel="Remove stream"
        body="Monitoring stops and the slot is freed. Saved clips for this streamer are kept." onConfirm={() => del && run("rm", () => api.removeStream(del.id).then(refresh), "Stream removed")} />
    </div>
  );
}

function StorageSettings({ draft, setDraft, num, save, dirty, busy }: any) {
  const [st, setSt] = useState<any>(null);
  const [confirm, setConfirm] = useState(false);
  const { busy: b, run } = useAction();
  const load = () => api.storage().then(setSt).catch(() => undefined);
  useEffect(() => {
    load();
  }, []);
  return (
    <div className="space-y-5">
      <div className="grid grid-cols-3 gap-3">
        <Field label="Max temporary storage" hint="Rolling-buffer budget across all streams">
          <div className="flex items-center gap-2"><input type="number" className={inputCls} step={0.5} value={+(draft.buffer.max_temp_bytes / 1024 ** 3).toFixed(2)}
            onChange={(e) => setDraft(setPath(draft, "buffer.max_temp_bytes", Math.round(Number(e.target.value) * 1024 ** 3)))} /><span className="text-xs text-ink-400">GB</span></div>
        </Field>
        {num("buffer.disk_warn_free_bytes", "Warn below free", { scale: 1024 ** 3, step: 0.5, unit: "GB" })}
        {num("buffer.disk_critical_free_bytes", "Critical below free", { scale: 1024 ** 3, step: 0.5, unit: "GB", hint: "Lowest-priority stream stops" })}
      </div>
      <div className="flex justify-end"><Button variant="primary" disabled={!dirty} loading={busy} onClick={save}><Save className="h-4 w-4" />Save</Button></div>
      {st && (
        <>
          <div className="grid grid-cols-4 gap-3">
            {[["Free disk", st.disk.free], ["Rolling buffer", st.disk.temp_bytes], ["Saved clips", st.disk.clip_bytes], ["Exports", st.disk.export_bytes]].map(([k, v]) => (
              <div key={k as string} className="panel p-3"><div className="label">{k}</div><div className="tabular mt-1 font-display text-lg">{bytes(v as number)}</div></div>
            ))}
          </div>
          <div className="panel p-3">
            <div className="label mb-2">By streamer</div>
            <table className="w-full text-sm"><thead className="text-left text-[11px] text-ink-400"><tr><th className="py-1">Streamer</th><th>Buffer</th><th>Clips</th><th>Clip files</th></tr></thead>
              <tbody>{st.by_streamer.map((r: any) => <tr key={r.id} className="border-t border-ink-750"><td className="py-1.5">{r.name}</td><td className="tabular font-mono text-xs">{bytes(r.temp_bytes)}</td><td>{r.clips}</td><td className="tabular font-mono text-xs">{bytes(r.clip_bytes)}</td></tr>)}</tbody></table>
          </div>
          <div className="panel space-y-1 p-3 font-mono text-[11px] text-ink-300">
            {Object.entries(st.paths).map(([k, v]) => <div key={k}><span className="inline-block w-24 text-ink-500">{k}</span>{v as string}</div>)}
            <div><span className="inline-block w-24 text-ink-500">deleted</span>{st.segments_deleted_total} expired segments since start</div>
          </div>
          <Button variant="secondary" loading={b === "cl"} onClick={() => setConfirm(true)}><Trash2 className="h-4 w-4" />Clean up temporary files…</Button>
          <Confirm open={confirm} onOpenChange={setConfirm} title="Clean up temporary files?" confirmLabel="Clean up"
            body="Runs retention now, removes orphaned segments, preview renders and discarded AI candidates. Approved, exported and review clips are never touched."
            onConfirm={() => run("cl", async () => { const r = await api.cleanup(); useStore.getState().toast("ok", `Removed ${r.deleted + r.budget_deleted + r.orphans} segments, ${r.previews_removed} previews`); load(); })} />
        </>
      )}
    </div>
  );
}

function AISettings({ draft, setDraft, sw, num }: any) {
  const sys = useStore((s) => s.system);
  const [check, setCheck] = useState<any>(null);
  const { busy, run } = useAction();
  return (
    <div className="space-y-4">
      <div className="panel space-y-1 p-3 text-xs">
        <div><span className="label mr-2">Whisper</span>{check?.whisper?.detail || sys?.whisper.detail || sys?.whisper.status}</div>
        <div><span className="label mr-2">LLM</span>{check?.semantic?.detail || sys?.semantic.detail || sys?.semantic.status}</div>
        <div className="flex gap-2 pt-1">
          <Button size="xs" loading={busy === "w"} onClick={() => run("w", async () => setCheck(await api.aiWarmup()))}>Load Whisper now</Button>
          <Button size="xs" loading={busy === "c"} onClick={() => run("c", async () => setCheck(await api.aiCheck()))}>Check Ollama</Button>
        </div>
      </div>
      <div className="grid grid-cols-3 gap-3">
        <Field label="Whisper model" hint="small is the default for 8 GB VRAM"><select className={inputCls} value={draft.ai.whisper_model} onChange={(e) => setDraft(setPath(draft, "ai.whisper_model", e.target.value))}>{["tiny", "base", "small", "medium", "large-v3", "distil-large-v3"].map((m) => <option key={m}>{m}</option>)}</select></Field>
        <Field label="Device"><select className={inputCls} value={draft.ai.whisper_device} onChange={(e) => setDraft(setPath(draft, "ai.whisper_device", e.target.value))}><option value="auto">Auto (CUDA → CPU)</option><option value="cuda">CUDA</option><option value="cpu">CPU</option></select></Field>
        <Field label="Language"><input className={inputCls} value={draft.ai.language} onChange={(e) => setDraft(setPath(draft, "ai.language", e.target.value))} /></Field>
      </div>
      {sw("ai.ollama_enabled", "Semantic scoring with a local LLM (Ollama)", "Optional, free. Needs Ollama running locally and the model pulled.")}
      <div className="grid grid-cols-3 gap-3">
        <Field label="Ollama URL"><input className={inputCls} value={draft.ai.ollama_url} onChange={(e) => setDraft(setPath(draft, "ai.ollama_url", e.target.value))} /></Field>
        <Field label="Ollama model"><input className={inputCls} value={draft.ai.ollama_model} onChange={(e) => setDraft(setPath(draft, "ai.ollama_model", e.target.value))} /></Field>
        {num("ai.ollama_timeout_s", "Timeout", { unit: "s" })}
      </div>
      {sw("ai.vision_enabled", "Facecam auto-detection on candidate clips", "Samples a few frames per clip only (needs opencv-python-headless)")}
    </div>
  );
}

function PublishingSettings({ draft, setDraft }: any) {
  const [pub, setPub] = useState<any>(null);
  const { busy, run } = useAction();
  useEffect(() => {
    api.publishing().then(setPub).catch(() => undefined);
  }, []);
  return (
    <div className="space-y-4">
      <div className="rounded-md border border-ink-700 p-3 text-xs text-ink-300">Local export always works: files go to <span className="font-mono">{pub?.local?.dir}</span>. Upload manually if no integration is configured.</div>
      <div className="panel space-y-3 p-4">
        <div className="font-medium">YouTube Shorts</div>
        <div className="text-xs text-ink-400">{pub?.youtube?.detail}</div>
        <Field label="OAuth client JSON path" hint="Google Cloud → APIs → YouTube Data API v3 → OAuth client (Desktop app)"><input className={inputCls} value={draft.publishing.youtube_client_secrets_path} onChange={(e) => setDraft(setPath(draft, "publishing.youtube_client_secrets_path", e.target.value))} /></Field>
        <Field label="Privacy"><select className={inputCls} value={draft.publishing.youtube_privacy} onChange={(e) => setDraft(setPath(draft, "publishing.youtube_privacy", e.target.value))}><option>private</option><option>unlisted</option><option>public</option></select></Field>
        <Button size="sm" variant="volt" disabled={!pub?.youtube?.available} loading={busy === "yt"} onClick={() => run("yt", async () => setPub({ ...pub, youtube: await api.ytAuthorize() }), "YouTube connected")}>Connect YouTube</Button>
      </div>
      <div className="panel space-y-1 p-4">
        <div className="font-medium">Instagram Reels</div>
        <div className="text-xs text-ink-400">{pub?.instagram?.detail}</div>
      </div>
    </div>
  );
}
