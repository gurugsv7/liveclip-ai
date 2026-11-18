import {
  Check, Copy, Download, ExternalLink, FolderOpen, Loader2, RefreshCcw, ScanFace, Sparkles, Trash2, Upload, Wand2, X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, media } from "@/lib/api";
import { bytes, cn, CLIP_STATE, dur, METHOD_LABEL } from "@/lib/format";
import { useStore } from "@/lib/store";
import type { Clip, Layout, Rect, Word } from "@/lib/types";
import { useAction } from "@/components/StreamPanel";
import { Badge, Button, Confirm, Field, inputCls, Slider, Switch, Tip } from "@/components/ui";

const LAYOUTS: { id: Layout; label: string }[] = [
  { id: "smart_crop", label: "Smart crop · 9:16" },
  { id: "facecam", label: "Facecam + gameplay" },
  { id: "letterbox", label: "Original · Letterbox" },
];
const STYLES = [
  { id: "bold_pop", label: "Bold pop", hint: "Uppercase, yellow active word" },
  { id: "box", label: "Highlight box", hint: "Violet box behind the active word" },
  { id: "neon", label: "Neon", hint: "Electric-blue glow" },
  { id: "clean", label: "Clean phrases", hint: "Sentence case, phrase-level" },
];

export function ClipEditor({ clip }: { clip: Clip }) {
  const cfg = useStore((s) => s.config);
  const progress = useStore((s) => s.renderProgress[clip.id]);
  const jobs = useStore((s) => s.jobs);
  const { busy, run } = useAction();
  const vref = useRef<HTMLVideoElement>(null);
  const [t, setT] = useState(0);
  const [title, setTitle] = useState(clip.title ?? "");
  const [confirmDel, setConfirmDel] = useState(false);
  const [guides, setGuides] = useState(true);
  const [drawFc, setDrawFc] = useState(false);
  const [words, setWords] = useState<Word[]>([]);
  const [wordsDirty, setWordsDirty] = useState(false);
  const [pub, setPub] = useState<any>(null);

  const D = clip.duration ?? 0;
  const lp = clip.layout_params ?? {};
  const cs = { style: cfg?.render?.caption_style ?? "bold_pop", size: cfg?.render?.caption_size ?? 80, y: cfg?.render?.caption_y ?? 0.66,
    uppercase: cfg?.render?.caption_uppercase ?? true, hook: cfg?.render?.hook_text_enabled ?? true, enabled: true, fps: cfg?.render?.fps ?? 60,
    ...(clip.caption_style ?? {}) };
  const trim: [number, number] = [clip.trim_start ?? 0, clip.trim_end ?? D];
  const facecam: Rect | null = (lp.facecam as Rect) ?? null;
  const rendering = Object.values(jobs).some((j) => j.ref_id === clip.id && j.kind.startsWith("render") && (j.state === "running" || j.state === "queued"));

  useEffect(() => {
    setTitle(clip.title ?? "");
  }, [clip.id, clip.title]);
  useEffect(() => {
    setWords((clip.caption_words ?? clip.transcript?.words ?? []).map((w) => ({ ...w })));
    setWordsDirty(false);
  }, [clip.id, clip.caption_words, clip.transcript]);
  useEffect(() => {
    api.publishing().then(setPub).catch(() => undefined);
  }, []);

  const patch = (b: Partial<Clip>, ok?: string) => run("patch", async () => useStore.getState().upsertClip(await api.patchClip(clip.id, b)), ok);
  const setStyle = (k: string, v: unknown) => patch({ caption_style: { ...cs, [k]: v } as any });
  const setLp = (k: string, v: unknown) => patch({ layout_params: { [k]: v } as any });
  const activeWord = useMemo(() => words.findIndex((w) => t >= w.start && t <= w.end), [words, t]);
  const transcriptRows = useMemo(() => {
    const rows: number[][] = [];
    words.forEach((word, i) => {
      const row = rows.at(-1);
      if (!row || row.length === 12 || word.start - words[i - 1].end > 0.65) rows.push([i]);
      else row.push(i);
    });
    return rows;
  }, [words]);

  return (
    <div className="grid min-h-0 min-w-0 flex-1 content-start lg:grid-cols-[minmax(0,1fr)_minmax(265px,36.5%)] lg:content-stretch lg:overflow-hidden">
      <section className="min-w-0 space-y-3 px-4 pb-5 pt-4 lg:overflow-y-auto xl:px-5" aria-label="Clip footage and transcript">
        <div>
          <input value={title} onChange={(e) => setTitle(e.target.value)} onBlur={() => title !== (clip.title ?? "") && patch({ title })}
            placeholder="Untitled clip" aria-label="Clip title"
            className="focus-ring w-full bg-transparent py-1 font-display text-[29px] font-black leading-tight tracking-tight placeholder:text-ink-500 hover:bg-ink-850 xl:text-[40px] 2xl:text-[46px]" />
          <div className="mt-1 flex flex-wrap items-center gap-2 font-mono text-[11px] text-ink-400">
            <span className="h-2 w-2 rounded-full" style={{ background: clip.streamer_color }} />
            <span className="font-semibold text-ink-200">{clip.streamer_name}</span>
            <span>{METHOD_LABEL[clip.detection_method]}</span>
            <Badge className={CLIP_STATE[clip.status]?.tone}>{CLIP_STATE[clip.status]?.label}</Badge>
            {clip.ai_score != null && (
              <Tip label={clip.ai_reason || "AI score"}>
                <span className="font-semibold text-violet-400">AI {Math.round(clip.ai_score * 100)}</span>
              </Tip>
            )}
          </div>
          {!!clip.suggested_titles?.length && (
            <div className="mt-1 flex flex-wrap gap-1">
              {clip.suggested_titles.map((s) => (
                <button key={s} onClick={() => { setTitle(s); patch({ title: s }); }}
                  className="focus-ring max-w-full truncate rounded-full border border-ink-600 px-2 py-0.5 text-[11px] text-ink-300 hover:border-violet-500 hover:text-ink-100">
                  <Sparkles className="mr-1 inline h-3 w-3 text-violet-400" />{s}
                </button>
              ))}
            </div>
          )}
        </div>
        {clip.error && <div className="border border-live/30 bg-live/10 px-3 py-2 text-xs text-live">{clip.error}</div>}
        <div className="media-well relative overflow-hidden border border-ink-500">
          {clip.files.file_path ? (
            <video ref={vref} src={media.clip(clip.id, "source")} poster={clip.files.thumb_path ? media.clip(clip.id, "thumb", clip.updated_at) : undefined} controls
              className={cn("aspect-video w-full", clip.layout !== "facecam" && "2xl:aspect-[1.57]")}
              onTimeUpdate={(e) => setT(e.currentTarget.currentTime)} />
          ) : (
            <div className="grid aspect-video place-items-center text-sm text-stone-200">
              {["protected", "processing", "detected"].includes(clip.status) ? <span className="flex items-center gap-2"><Loader2 className="h-4 w-4 animate-spin" />Extracting protected footage…</span> : "Source file not available"}
            </div>
          )}
          {clip.layout === "facecam" && clip.files.file_path && (
            <FacecamOverlay rect={facecam} drawing={drawFc} onDone={(r) => { setDrawFc(false); setLp("facecam", r); }} />
          )}
        </div>
        {D > 0 && (
          <section className="space-y-2 pb-2">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <h2 className="font-display text-[28px] font-black leading-tight">Trim</h2>
              <span className="font-mono text-[11px] text-ink-400">{(trim[1] - trim[0]).toFixed(1)}s
                {(trim[1] - trim[0]) > (cfg?.render?.max_reel_s ?? 30) && <span className="ml-2 text-violet-400">Export auto-cuts to ≤{cfg?.render?.max_reel_s ?? 30}s</span>}</span>
            </div>
            <div className="flex items-center gap-3 font-mono text-xs">
              <span className="border border-ink-500 px-2.5 py-1">{dur(trim[0])}</span>
              <Slider value={trim} min={0} max={D} step={0.1} onChange={(v) => useStore.getState().upsertClip({ ...clip, trim_start: v[0], trim_end: v[1] })} label="Trim range" />
              <span className="border border-ink-500 px-2.5 py-1">{dur(trim[1])}</span>
            </div>
            <div className="mt-2 flex flex-wrap gap-1.5">
              <Button size="xs" onClick={() => patch({ trim_start: Math.min(t, trim[1] - 1) })}>Set in @ {dur(t)}</Button>
              <Button size="xs" onClick={() => patch({ trim_end: Math.max(t, trim[0] + 1) })}>Set out @ {dur(t)}</Button>
              <Button size="xs" variant="ghost" onClick={() => patch({ trim_start: trim[0], trim_end: trim[1] }, "Trim saved")}>Save trim</Button>
              <Button size="xs" variant="ghost" onClick={() => patch({ trim_start: 0, trim_end: D })}>Reset</Button>
              {lp.peak != null && <Button size="xs" variant="ghost" onClick={() => vref.current && (vref.current.currentTime = lp.peak!)}>Jump to peak ({dur(lp.peak)})</Button>}
            </div>
          </section>
        )}
        {/* captions */}
        <section className="border-t border-ink-500 pt-3">
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <h2 className="font-display text-[28px] font-black leading-tight">Transcript</h2>
            <span className="text-[11px] text-ink-400">{clip.transcript ? `${clip.transcript.model} · ${words.length} words` : "No transcript yet"}</span>
            <div className="flex-1" />
            {wordsDirty && <Button size="xs" variant="primary" onClick={() => { patch({ caption_words: words }, "Caption corrections saved"); setWordsDirty(false); }}>Save corrections</Button>}
            <Button size="xs" variant="ghost" loading={busy === "rt"} onClick={() => run("rt", () => api.retranscribe(clip.id), "Re-transcribing")}><RefreshCcw className="h-3 w-3" />Regenerate</Button>
          </div>
          {words.length === 0 ? (
            <div className="text-xs text-ink-500">Whisper transcribes after extraction. Unclear speech is left out, never invented.</div>
          ) : (
            <div className="space-y-1.5 font-mono text-xs" aria-label="Caption words">
              {transcriptRows.map((row) => (
                <div key={row[0]} className="grid grid-cols-[56px_minmax(0,1fr)] gap-2">
                  <button className="focus-ring self-start text-left text-violet-400" onClick={() => vref.current && (vref.current.currentTime = words[row[0]].start)} aria-label={`Seek to ${dur(words[row[0]].start)}`}>{dur(words[row[0]].start)}</button>
                  <div className="flex flex-wrap">
                    {row.map((i) => {
                      const w = words[i];
                      return <input key={i} value={w.text} title={`${w.start.toFixed(2)}–${w.end.toFixed(2)}s${w.prob != null ? ` · conf ${(w.prob * 100).toFixed(0)}%` : ""}`}
                        aria-label={`Caption word ${i + 1}`} onFocus={() => vref.current && (vref.current.currentTime = w.start)}
                        onChange={(e) => { const n = words.slice(); n[i] = { ...w, text: e.target.value }; setWords(n); setWordsDirty(true); }}
                        style={{ width: `${Math.max(2, w.text.length + 1)}ch` }}
                        className={cn("focus-ring max-w-full bg-transparent hover:bg-ink-800",
                          i === activeWord ? "bg-violet-500/10 text-violet-400" : "text-ink-200", (w.prob ?? 1) < 0.4 && "underline decoration-dotted")} />;
                    })}
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      </section>

      <aside className="editor-inspector min-w-0 space-y-4 border-t border-ink-600 px-4 pb-5 pt-5 lg:overflow-y-auto lg:border-l lg:border-t-0 xl:px-5" aria-label="Edit and export">
        <h2 className="font-display text-[32px] font-black leading-none tracking-tight 2xl:text-[45px]">Edit &amp; export</h2>
        <div className="space-y-4">
          <Field label="Layout">
            <select className={cn(inputCls, "h-10 font-mono")} value={clip.layout} onChange={(e) => patch({ layout: e.target.value as Layout })}>
              {LAYOUTS.map((l) => <option key={l.id} value={l.id}>{l.label}</option>)}
            </select>
          </Field>
          {clip.layout === "facecam" && (
            <div className="flex flex-wrap gap-1.5">
              <Button size="xs" variant={drawFc ? "primary" : "secondary"} onClick={() => setDrawFc(!drawFc)}>{drawFc ? "Drag on the video…" : facecam ? "Redraw facecam" : "Draw facecam"}</Button>
              <Button size="xs" variant="ghost" loading={busy === "fc"} onClick={() => run("fc", async () => {
                const r = await api.detectFacecam(clip.id);
                if (r.facecam) setLp("facecam", r.facecam);
                else throw new Error(r.detail ?? "No facecam found");
              }, "Facecam detected")}><ScanFace className="h-3 w-3" />Auto-detect</Button>
              {!facecam && <span className="text-[11px] text-warn">Uses the streamer default, or smart crop if none is set.</span>}
            </div>
          )}
          <Field label="Caption style">
            <select className={cn(inputCls, "h-10 font-mono")} value={cs.style} onChange={(e) => setStyle("style", e.target.value)}>
              {STYLES.map((s) => <option key={s.id} value={s.id}>{s.label} — {s.hint}</option>)}
            </select>
          </Field>
          <div className="grid grid-cols-2 gap-x-2 gap-y-3 border-y border-ink-750 py-3 text-[11px] text-ink-300">
            <Toggle label="Cold-open hook" tip="Flash the peak moment first, then play from the setup" checked={!!lp.cold_open} onChange={(v) => setLp("cold_open", v)} />
            <Toggle label="Punch-in at peak" tip="Subtle 7% zoom on the reaction (smart crop)" checked={lp.punch_zoom !== false} onChange={(v) => setLp("punch_zoom", v)} />
            <Toggle label="Hook title" tip="Title on screen from frame 1" checked={cs.hook} onChange={(v) => setStyle("hook", v)} />
            <Toggle label="Captions" checked={cs.enabled !== false} onChange={(v) => setStyle("enabled", v)} />
            <Toggle label="Uppercase" checked={cs.uppercase} onChange={(v) => setStyle("uppercase", v)} />
            <Toggle label="Loudness −14 LUFS" tip="Optional normalization; original audio otherwise untouched" checked={!!lp.normalize_audio} onChange={(v) => setLp("normalize_audio", v)} />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <Field label={`Caption size · ${cs.size}px`}><Slider value={[cs.size]} min={48} max={120} onChange={(v) => setStyle("size", v[0])} label="Caption size" /></Field>
            <Field label={`Caption position · ${Math.round(cs.y * 100)}% down`} hint="Keep within 55–72% to stay clear of platform UI"><Slider value={[cs.y]} min={0.3} max={0.8} step={0.01} onChange={(v) => setStyle("y", v[0])} label="Caption position" /></Field>
          </div>
          <Field label="Frame rate">
            <div className="flex gap-1">{[30, 60].map((f) => (
              <button key={f} onClick={() => setStyle("fps", f)} className={cn("focus-ring flex-1 border py-1 text-xs", cs.fps === f ? "border-violet-500 bg-violet-500/15" : "border-ink-600 text-ink-300")}>{f} fps</button>
            ))}</div>
          </Field>
          {clip.fps != null && cs.fps > Math.round(clip.fps) && <div className="text-[11px] text-ink-400">Source is {Math.round(clip.fps)} fps; output keeps {Math.round(clip.fps)} fps (no invented frames).</div>}
        </div>

        <div className="grid gap-3 border-t border-ink-500 pt-4 2xl:gap-4 2xl:pt-5">
          <h3 className="font-display text-[32px] font-black leading-none 2xl:text-[40px]">Output</h3>
          <Button size="lg" className="h-12 font-display text-xl font-black 2xl:h-16 2xl:text-[27px]" variant="secondary" disabled={!clip.files.file_path || rendering} loading={busy === "pv"} onClick={() => run("pv", () => api.renderPreview(clip.id), "Rendering preview")}>
            <Wand2 className="h-4 w-4" />Preview
          </Button>
          <Button size="lg" className="h-12 font-display text-xl font-black 2xl:h-16 2xl:text-[27px]" variant="primary" disabled={!clip.files.file_path || rendering} loading={busy === "ex"} onClick={() => run("ex", () => api.exportClip(clip.id), "Export queued")}>
            {rendering ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}Export 1080×1920
          </Button>
        </div>
        {progress != null && <progress className="h-1.5 w-full accent-violet-500" value={progress} max={1} aria-label="Render progress" />}
        {clip.files.preview_path && (
          <details open className="space-y-2 border-y border-ink-750 py-2">
            <summary className="focus-ring cursor-pointer font-display text-base font-bold">9:16 preview</summary>
            <div className="media-well relative mx-auto aspect-[9/16] w-full max-w-[240px] overflow-hidden border border-ink-600">
              <video key={clip.updated_at} src={media.clip(clip.id, "preview", clip.updated_at)} controls loop className="h-full w-full" />
              {guides && (
                <div className="pointer-events-none absolute inset-0">
                  <div className="absolute inset-x-0 top-0 h-[6.25%] bg-live/10" />
                  <div className="absolute inset-x-0 bottom-0 h-[16.7%] bg-live/10" />
                  <div className="absolute bottom-0 right-0 top-0 w-[11%] bg-live/10" />
                  <div className="absolute border border-dashed border-lime-300/60" style={{ left: "8.3%", right: "8.3%", top: "13.5%", bottom: "13.5%" }} />
                </div>
              )}
            </div>
            <label className="flex items-center gap-2 text-[11px] text-ink-400"><Switch checked={guides} onChange={setGuides} label="Safe-area guides" />Platform UI safe-area guides</label>
          </details>
        )}
        {clip.files.export_path && (
          <div className="panel space-y-1.5 p-3 text-xs">
            <div className="flex items-center gap-2 text-ok"><Check className="h-3.5 w-3.5" />Exported</div>
            <div className="break-all font-mono text-[10px] text-ink-400">{clip.export_path}</div>
            <div className="flex flex-wrap gap-1.5">
              <a className="inline-flex h-6 items-center gap-1 rounded-md border border-ink-600 px-2 text-[11px] hover:bg-ink-700" href={media.clip(clip.id, "export")} download><Download className="h-3 w-3" />MP4</a>
              <a className="inline-flex h-6 items-center gap-1 rounded-md border border-ink-600 px-2 text-[11px] hover:bg-ink-700" href={media.clip(clip.id, "srt")} download>SRT</a>
              <Button size="xs" onClick={() => run("of", () => api.openFolder(clip.id))}><FolderOpen className="h-3 w-3" />Show in folder</Button>
              {pub?.youtube?.authorized ? (
                <Button size="xs" variant="volt" loading={busy === "yt"} onClick={() => run("yt", async () => {
                  const r = await api.publish(clip.id, "youtube");
                  useStore.getState().toast("ok", `Uploaded: ${r.url}`);
                })}><Upload className="h-3 w-3" />YouTube</Button>
              ) : (
                <Tip label={pub?.youtube?.detail ?? "YouTube publishing not configured"}><span className="text-[10px] text-ink-500">Upload manually (publishing not configured)</span></Tip>
              )}
            </div>
            {clip.publish_status && <div className="text-[11px] text-ink-300">Publish: {clip.publish_status}</div>}
          </div>
        )}
        {clip.files.export_path && (clip.files.cover_path || clip.post_caption) && <PostKit clip={clip} />}

        <div className="space-y-1 text-[11px] text-ink-400">
          <Row k="Captured" v={new Date(clip.created_at * 1000).toLocaleString()} />
          <Row k="Source" v={clip.width ? `${clip.width}×${clip.height} @ ${clip.fps?.toFixed(0)} fps` : "—"} />
          <Row k="Duration" v={dur(D)} />
          <Row k="File" v={bytes(clip.file_size)} />
          <Row k="Stream time" v={`${dur(clip.t_start)} → ${dur(clip.t_end)}`} />
          <a href={clip.source_url} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-violet-400 hover:underline"><ExternalLink className="h-3 w-3" />Original stream</a>
        </div>
        {clip.ai_reason && <div className="text-xs text-ink-300"><span className="label mr-2">Why</span>{clip.ai_reason}</div>}
        <div className="flex flex-wrap gap-1.5 border-t border-ink-750 pt-3">
          <Button variant="secondary" size="xs" onClick={() => run("rej", () => api.reject(clip.id).then(useStore.getState().upsertClip))} disabled={clip.status === "rejected"}><X className="h-3.5 w-3.5" />Reject</Button>
          <Button variant="secondary" size="xs" className="border-ok/40 text-ok" onClick={() => run("app", () => api.approve(clip.id).then(useStore.getState().upsertClip))} disabled={clip.status === "approved" || clip.status === "exported"}><Check className="h-3.5 w-3.5" />Approve</Button>
          <Button size="xs" variant="ghost" onClick={() => run("ofs", () => api.openFolder(clip.id, "source"))} disabled={!clip.files.file_path}><FolderOpen className="h-3 w-3" />Source file</Button>
          <Button size="xs" variant="danger" onClick={() => setConfirmDel(true)}><Trash2 className="h-3 w-3" />Delete clip</Button>
        </div>
      </aside>
      <Confirm open={confirmDel} onOpenChange={setConfirmDel} title="Delete this clip permanently?" confirmLabel="Delete"
        body="The source clip, preview, export, subtitles and thumbnail files are removed from disk. This can't be undone."
        onConfirm={() => run("del", async () => { await api.deleteClip(clip.id); useStore.getState().removeClip(clip.id); }, "Clip deleted")} />
    </div>
  );
}

function Toggle({ label, checked, onChange, tip }: { label: string; checked: boolean; onChange: (v: boolean) => void; tip?: string }) {
  const el = <label className="flex cursor-pointer items-center gap-2"><Switch checked={checked} onChange={onChange} label={label} />{label}</label>;
  return tip ? <Tip label={tip}>{el}</Tip> : el;
}

function Row({ k, v }: { k: string; v: string }) {
  return <div className="flex justify-between"><span>{k}</span><span className="tabular font-mono text-ink-300">{v}</span></div>;
}

function FacecamOverlay({ rect, drawing, onDone }: { rect: Rect | null; drawing: boolean; onDone: (r: Rect) => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [drag, setDrag] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const rel = (e: React.PointerEvent) => {
    const b = ref.current!.getBoundingClientRect();
    return { x: Math.max(0, Math.min(1, (e.clientX - b.left) / b.width)), y: Math.max(0, Math.min(1, (e.clientY - b.top) / b.height)) };
  };
  const show = drag ? { x: Math.min(drag.x0, drag.x1), y: Math.min(drag.y0, drag.y1), w: Math.abs(drag.x1 - drag.x0), h: Math.abs(drag.y1 - drag.y0) } : rect;
  return (
    <div ref={ref} className={cn("absolute inset-0", drawing ? "cursor-crosshair bg-black/20" : "pointer-events-none")}
      onPointerDown={(e) => { if (!drawing) return; const p = rel(e); setDrag({ x0: p.x, y0: p.y, x1: p.x, y1: p.y }); (e.target as HTMLElement).setPointerCapture(e.pointerId); }}
      onPointerMove={(e) => { if (drag) { const p = rel(e); setDrag({ ...drag, x1: p.x, y1: p.y }); } }}
      onPointerUp={() => { if (drag && show && show.w > 0.03 && show.h > 0.03) onDone({ x: +show.x.toFixed(4), y: +show.y.toFixed(4), w: +show.w.toFixed(4), h: +show.h.toFixed(4) }); setDrag(null); }}>
      {show && (
        <div className="absolute border-2 border-volt-400 bg-volt-400/10" style={{ left: `${show.x * 100}%`, top: `${show.y * 100}%`, width: `${show.w * 100}%`, height: `${show.h * 100}%` }}>
          <span className="absolute -top-5 left-0 rounded bg-volt-500 px-1 text-[10px] text-white">facecam</span>
        </div>
      )}
    </div>
  );
}

/** Cover image + ready-to-paste caption, made with every export. */
function PostKit({ clip }: { clip: Clip }) {
  const toast = useStore((s) => s.toast);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(clip.post_caption ?? "");
      toast("ok", "Caption copied");
    } catch {
      toast("err", "Couldn't copy: select the text and press Ctrl+C");
    }
  };
  return (
    <section className="panel space-y-3 p-3 text-xs" aria-label="Post kit">
      <h3 className="font-display text-lg font-black leading-none">Post kit</h3>
      {clip.files.cover_path && (
        <div className="flex items-start gap-3">
          <img src={media.clip(clip.id, "cover", clip.updated_at)} alt="Reel cover: best frame with the hook line"
            className="aspect-[9/16] w-24 shrink-0 border border-ink-600 object-cover" />
          <div className="space-y-1.5">
            <div className="font-semibold">Cover</div>
            <p className="text-[11px] text-ink-400">Best frame of the reel with the hook line. Upload it as the Reel/Short cover.</p>
            <a className="inline-flex h-6 items-center gap-1 rounded-md border border-ink-600 px-2 text-[11px] hover:bg-ink-700"
              href={media.clip(clip.id, "cover", clip.updated_at)} download><Download className="h-3 w-3" />Cover JPG</a>
          </div>
        </div>
      )}
      {clip.post_caption && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <span className="font-semibold">Caption</span>
            <Button size="xs" onClick={copy}><Copy className="h-3 w-3" />Copy</Button>
          </div>
          <textarea readOnly value={clip.post_caption} rows={Math.min(10, clip.post_caption.split("\n").length + 1)}
            className={cn(inputCls, "w-full resize-y font-sans text-[12px] leading-snug")} aria-label="Post caption"
            onFocus={(e) => e.currentTarget.select()} />
        </div>
      )}
    </section>
  );
}
