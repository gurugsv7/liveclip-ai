import { Check, Clapperboard, Film, Search, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api, media } from "@/lib/api";
import { ago, cn, CLIP_STATE, dur, METHOD_LABEL } from "@/lib/format";
import { useStore } from "@/lib/store";
import type { Clip } from "@/lib/types";
import { Badge, Button, inputCls } from "@/components/ui";
import { ClipEditor } from "./ClipEditor";
import { isTyping } from "@/lib/shortcuts";

const STATUS_FILTERS = [
  { id: "", label: "All" }, { id: "ready_for_review", label: "To review" }, { id: "maybe", label: "Maybe" }, { id: "approved", label: "Approved" },
  { id: "exported", label: "Exported" }, { id: "processing,protected,detected", label: "Processing" }, { id: "rejected", label: "Rejected" },
  { id: "failed", label: "Failed" },
];

function ClipCard({ c, active, onClick }: { c: Clip; active: boolean; onClick: () => void }) {
  return (
    <button onClick={onClick} aria-pressed={active} className={cn("focus-ring flex w-full items-start gap-3 border-b border-l-4 border-b-ink-750 px-3 py-2.5 text-left transition-colors xl:gap-4 xl:px-4 xl:py-3",
      active ? "border-l-violet-500 bg-ink-850" : "border-l-transparent hover:bg-ink-800")}>
      <div className="media-well relative grid aspect-video w-[41%] max-w-[150px] shrink-0 place-items-center overflow-hidden">
        {c.files.thumb_path ? <img src={media.clip(c.id, "thumb", c.updated_at)} alt="" className="h-full w-full object-cover" /> : <Film className="h-5 w-5 text-stone-300" />}
      </div>
      <div className="min-w-0 flex-1">
        <div className="line-clamp-2 font-display text-base font-extrabold leading-tight 2xl:text-xl">{c.title || `${c.streamer_name} · ${METHOD_LABEL[c.detection_method]}`}</div>
        <div className="mt-1 font-mono text-xs">{dur(c.duration)}</div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 font-mono text-[10px] text-ink-400">
          <span className="text-ink-300">{c.streamer_name}</span>
          <Badge className={CLIP_STATE[c.status]?.tone}>{CLIP_STATE[c.status]?.label}</Badge>
          {c.ai_score != null && <span className="tabular font-mono text-violet-400">AI {Math.round(c.ai_score * 100)}</span>}
          {c.export_status === "done" && <span className="text-ok">exported</span>}
          <span>{ago(c.created_at)}</span>
        </div>
      </div>
    </button>
  );
}

export function ClipsPage() {
  const clips = useStore((s) => s.clips);
  const streams = useStore((s) => s.streams);
  const focus = useStore((s) => s.focusClip);
  const setS = useStore((s) => s.set);
  const [streamer, setStreamer] = useState("");
  const [status, setStatus] = useState("");
  const [sort, setSort] = useState<"date" | "score">("date");
  const [q, setQ] = useState("");

  useEffect(() => {
    api.clips({ sort }).then((c) => useStore.getState().setClips(c)).catch(() => undefined);
  }, [sort]);

  const list = useMemo(() => {
    let l = clips.filter((c) => (!streamer || c.streamer_id === streamer) && (!status || status.split(",").includes(c.status)));
    if (q) l = l.filter((c) => `${c.title} ${c.caption_text} ${c.streamer_name}`.toLowerCase().includes(q.toLowerCase()));
    return [...l].sort((a, b) => (sort === "score" ? (b.ai_score ?? -1) - (a.ai_score ?? -1) : b.created_at - a.created_at));
  }, [clips, streamer, status, sort, q]);
  const current = list.find((c) => c.id === focus) ?? clips.find((c) => c.id === focus) ?? list[0];

  return (
    <div className="flex min-h-0 flex-1 flex-col md:flex-row">
      <section className="flex shrink-0 flex-col border-b border-ink-600 md:w-[31%] md:border-b-0 md:border-r xl:w-[26%]" aria-label="Clip library">
        <div className="space-y-3 border-b border-ink-600 px-4 pb-4 pt-5 xl:px-5">
          <h1 className="page-title">Clips</h1>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2" />
            <input className={cn(inputCls, "h-10 border-ink-500 pl-9 font-mono 2xl:h-12")} aria-label="Search clips" placeholder="Search clips…" value={q} onChange={(e) => setQ(e.target.value)} />
          </div>
          <div className="flex gap-1.5">
            <select className={inputCls} value={streamer} onChange={(e) => setStreamer(e.target.value)} aria-label="Filter by streamer">
              <option value="">All streamers</option>
              {streams.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
            <select className={cn(inputCls, "w-32")} value={sort} onChange={(e) => setSort(e.target.value as any)} aria-label="Sort">
              <option value="date">Newest</option><option value="score">AI score</option>
            </select>
          </div>
          <div className="flex flex-wrap gap-1">
            {STATUS_FILTERS.map((f) => (
              <button key={f.id} onClick={() => setStatus(f.id)} aria-pressed={status === f.id} className={cn("focus-ring border px-2 py-1 font-mono text-[11px] font-semibold",
                status === f.id ? "border-violet-500 bg-violet-500 text-white" : "border-ink-600 text-ink-300 hover:bg-ink-800")}>{f.label}</button>
            ))}
          </div>
        </div>
        <div className="max-h-64 min-h-0 flex-1 overflow-y-auto lg:max-h-none">
          {list.length === 0 && <div className="p-6 text-center text-xs text-ink-500">No clips match.</div>}
          {list.map((c) => <ClipCard key={c.id} c={c} active={current?.id === c.id} onClick={() => setS({ focusClip: c.id })} />)}
        </div>
      </section>
      {current ? <ClipEditor key={current.id} clip={current} /> : (
        <div className="grid flex-1 place-items-center text-center text-sm text-ink-400">
          <div><Clapperboard className="mx-auto mb-2 h-8 w-8 text-ink-600" />Saved and AI-detected clips appear here.</div>
        </div>
      )}
    </div>
  );
}

export function ReviewQueue() {
  const clips = useStore((s) => s.clips);
  const setS = useStore((s) => s.set);
  const toast = useStore((s) => s.toast);
  const queue = useMemo(() => clips.filter((c) => c.status === "ready_for_review").sort((a, b) => (b.ai_score ?? -1) - (a.ai_score ?? -1)), [clips]);
  const [idx, setIdx] = useState(0);
  const cur = queue[Math.min(idx, queue.length - 1)];

  const act = async (c: Clip, fn: (id: string) => Promise<Clip>, label: string) => {
    try {
      useStore.getState().upsertClip(await fn(c.id));
      toast("ok", label);
    } catch (e) {
      toast("err", (e as Error).message);
    }
  };

  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if (isTyping(e.target) || !cur) return;
      if (e.key === "ArrowRight" || e.key.toLowerCase() === "j") setIdx((i) => Math.min(i + 1, queue.length - 1));
      if (e.key === "ArrowLeft" || e.key.toLowerCase() === "k") setIdx((i) => Math.max(i - 1, 0));
      if (e.key.toLowerCase() === "y") act(cur, api.approve, "Approved");
      if (e.key.toLowerCase() === "n") act(cur, api.reject, "Rejected");
    };
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
  });

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto p-5">
      <div className="mb-4 flex flex-wrap items-baseline gap-3 border-b border-ink-600 pb-3">
        <h1 className="page-title">Review Queue</h1>
        {queue.length > 0 && <span className="font-mono text-xs text-ink-400">{queue.length} clips · sorted by AI score · <kbd>Y</kbd> approve · <kbd>N</kbd> reject · <kbd>←/→</kbd> navigate</span>}
      </div>
      {!queue.length && <p className="text-sm text-ink-400">New clips appear here after transcription and scoring.</p>}
      <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
        {cur && (
          <div className="panel overflow-hidden">
            <video key={cur.id} src={media.clip(cur.id, "source")} controls autoPlay className="media-well aspect-video w-full" />
            <div className="space-y-2 p-4">
              <div className="flex items-center gap-2 text-xs text-ink-400"><span className="h-2 w-2 rounded-full" style={{ background: cur.streamer_color }} />{cur.streamer_name} · {METHOD_LABEL[cur.detection_method]} · {dur(cur.duration)}</div>
              <div className="font-display text-base font-semibold">{cur.title || "Untitled"}</div>
              {cur.ai_reason && <div className="text-xs text-ink-300">{cur.ai_reason}</div>}
              {cur.caption_text && <div className="max-h-24 overflow-y-auto rounded-md bg-ink-900 p-2 text-xs italic text-ink-300">"{cur.caption_text}"</div>}
              <div className="flex gap-2 pt-1">
                <Button size="md" variant="secondary" onClick={() => act(cur, api.reject, "Rejected")}><X className="h-4 w-4" />Reject</Button>
                <Button size="md" variant="secondary" className="border-ok/40 text-ok" onClick={() => act(cur, api.approve, "Approved")}><Check className="h-4 w-4" />Approve</Button>
                <Button size="md" variant="primary" onClick={() => setS({ page: "clips", focusClip: cur.id })}>Open in editor</Button>
              </div>
            </div>
          </div>
        )}
        <div className="space-y-1">
          {queue.map((c, i) => <ClipCard key={c.id} c={c} active={c.id === cur?.id} onClick={() => setIdx(i)} />)}
        </div>
      </div>
    </div>
  );
}
