import { Check, ChevronsLeft, ChevronsRight, Crosshair, Pause, Play, Sparkles, Undo2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { dur } from "@/lib/format";
import { Button } from "./ui";

export interface ReelWin { s: number; len: number }

/**
 * The reel is one window of the clip, ≤ max seconds. Drag the box (or nudge it) to move it
 * forward/back, drag its edges or pick 15/20/30 s to change its length, play just the window,
 * then "Use this window" — export renders exactly that, never an automatic cut.
 */
export function ReelWindow({ D, max, win, onChange, saved, dirty, onSave, onAuto, peak, t, video, words }: {
  D: number; max: number; win: ReelWin; onChange: (w: ReelWin) => void; saved: boolean; dirty: boolean;
  onSave: () => void; onAuto: () => void; peak: number | null; t: number; video: HTMLVideoElement | null;
  words: { start: number; end: number }[];
}) {
  const bar = useRef<HTMLDivElement>(null);
  const drag = useRef<{ mode: "move" | "l" | "r"; x0: number; w0: ReelWin } | null>(null);
  const [playing, setPlaying] = useState(false);
  const cur = useRef(win);               // latest window, so quick repeated nudges add up
  cur.current = win;
  const minLen = Math.min(5, D);
  const clamp = (w: ReelWin): ReelWin => {
    const len = Math.max(minLen, Math.min(max, D, w.len));
    return { s: Math.max(0, Math.min(D - len, w.s)), len };
  };
  const set = (w: ReelWin) => { cur.current = clamp(w); onChange(cur.current); };
  const pct = (x: number) => `${(x / Math.max(D, 0.001)) * 100}%`;
  const seek = (x: number) => { if (video) video.currentTime = x; };

  // stop "play window" at the end of the window
  useEffect(() => {
    if (playing && video && t >= win.s + win.len - 0.05) {
      video.pause();
      setPlaying(false);
    }
  }, [t, playing, video, win]);
  useEffect(() => {
    if (!video) return;
    const stop = () => setPlaying(false);
    video.addEventListener("pause", stop);
    return () => video.removeEventListener("pause", stop);
  }, [video]);

  const onDown = (mode: "move" | "l" | "r") => (e: React.PointerEvent) => {
    e.preventDefault();
    e.stopPropagation();
    (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    drag.current = { mode, x0: e.clientX, w0: win };
  };
  const onMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d || !bar.current) return;
    const dt = ((e.clientX - d.x0) / bar.current.clientWidth) * D;
    if (d.mode === "move") set({ ...d.w0, s: d.w0.s + dt });
    else if (d.mode === "l") {
      const s = Math.max(0, Math.min(d.w0.s + d.w0.len - minLen, d.w0.s + dt));
      set({ s, len: d.w0.s + d.w0.len - s });
    } else set({ ...d.w0, len: d.w0.len + dt });
  };
  const onUp = () => {
    if (drag.current) seek(win.s);
    drag.current = null;
  };
  const nudge = (by: number) => { const w = clamp({ ...cur.current, s: cur.current.s + by }); cur.current = w; onChange(w); seek(w.s); };
  const playWindow = () => {
    if (!video) return;
    if (playing) { video.pause(); return; }
    video.currentTime = win.s;
    video.play().then(() => setPlaying(true)).catch(() => undefined);
  };
  const lengths = [15, 20, 30].filter((l) => l <= max && l <= D + 0.01);

  return (
    <section className="space-y-2 pb-2" aria-label="Reel window">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-display text-[28px] font-black leading-tight">Reel window</h2>
        <span className="font-mono text-[11px] text-ink-300">{dur(win.s)} → {dur(win.s + win.len)} · {win.len.toFixed(1)}s</span>
      </div>
      {/* timeline: whole clip, the reel window as a draggable box */}
      <div ref={bar} className="relative h-12 select-none border border-ink-500 bg-ink-850 touch-none"
        onPointerMove={onMove} onPointerUp={onUp} onPointerCancel={onUp}
        onClick={(e) => { if (!bar.current || e.target !== bar.current) return; const r = bar.current.getBoundingClientRect();
          const x = ((e.clientX - r.left) / r.width) * D; nudge(x - win.len / 2 - win.s); }}
        aria-label="Clip timeline: drag the box to move the reel window">
        {words.map((w, i) => (
          <span key={i} className="pointer-events-none absolute bottom-1 h-1.5 bg-ink-500" style={{ left: pct(w.start), width: pct(Math.max(0.08, w.end - w.start)) }} />
        ))}
        {peak != null && <span className="pointer-events-none absolute inset-y-0 w-0.5 bg-live" style={{ left: pct(peak) }} title="Hype peak" />}
        <div role="slider" tabIndex={0} aria-label="Reel window position" aria-valuemin={0} aria-valuemax={Math.max(0, D - win.len)} aria-valuenow={Math.round(win.s * 10) / 10}
          onKeyDown={(e) => { if (e.key === "ArrowLeft") { e.preventDefault(); nudge(e.shiftKey ? -5 : -1); } if (e.key === "ArrowRight") { e.preventDefault(); nudge(e.shiftKey ? 5 : 1); } }}
          onPointerDown={onDown("move")}
          className="focus-ring absolute inset-y-0 cursor-grab border-2 border-violet-500 bg-violet-500/20 active:cursor-grabbing"
          style={{ left: pct(win.s), width: pct(win.len) }}>
          <span onPointerDown={onDown("l")} className="absolute inset-y-0 -left-1.5 w-3 cursor-ew-resize bg-violet-500/80" aria-hidden />
          <span onPointerDown={onDown("r")} className="absolute inset-y-0 -right-1.5 w-3 cursor-ew-resize bg-violet-500/80" aria-hidden />
          <span className="pointer-events-none absolute left-2 top-0.5 font-mono text-[10px] font-semibold text-violet-200">{win.len.toFixed(0)}s</span>
        </div>
        <span className="pointer-events-none absolute inset-y-0 w-px bg-white/80" style={{ left: pct(Math.min(t, D)) }} />
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <Button size="xs" onClick={() => nudge(-5)} aria-label="Move window back 5 seconds"><ChevronsLeft className="h-3 w-3" />5s</Button>
        <Button size="xs" onClick={() => nudge(-1)} aria-label="Move window back 1 second">−1s</Button>
        <Button size="xs" onClick={() => nudge(1)} aria-label="Move window forward 1 second">+1s</Button>
        <Button size="xs" onClick={() => nudge(5)} aria-label="Move window forward 5 seconds">5s<ChevronsRight className="h-3 w-3" /></Button>
        <span className="mx-1 h-4 w-px bg-ink-600" />
        {lengths.map((l) => (
          <Button key={l} size="xs" variant={Math.abs(win.len - l) < 0.05 ? "primary" : "secondary"}
            onClick={() => set({ s: win.s, len: l })}>{l}s</Button>
        ))}
        <span className="mx-1 h-4 w-px bg-ink-600" />
        <Button size="xs" onClick={() => nudge(t - win.s)}><Crosshair className="h-3 w-3" />Start at playhead</Button>
        {peak != null && <Button size="xs" variant="ghost" onClick={() => nudge(peak - win.len * 0.4 - win.s)}>Center on peak</Button>}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <Button size="md" variant="secondary" onClick={playWindow} disabled={!video}>
          {playing ? <Pause className="h-4 w-4" /> : <Play className="h-4 w-4" />}{playing ? "Pause" : "Play window"}
        </Button>
        <Button size="md" variant="primary" onClick={onSave} disabled={!dirty && saved}>
          <Check className="h-4 w-4" />{!dirty && saved ? "Window saved" : `Use this window (${dur(win.s)} → ${dur(win.s + win.len)})`}
        </Button>
        <Button size="xs" variant="ghost" onClick={onAuto} aria-label="Let the AI pick the best cut instead"><Sparkles className="h-3 w-3" />Let AI cut</Button>
      </div>
      <p className="text-[11px] text-ink-400">
        {saved && !dirty
          ? <>Export uses exactly this window: facecam layout when a facecam is found, captions and hook. No automatic cut.</>
          : saved && dirty ? <span className="text-warn">Changed. Press “Use this window” (Export also saves it).</span>
          : <><Undo2 className="mr-1 inline h-3 w-3" />No window chosen: export auto-cuts the best ≤{max}s. Move the box and press “Use this window” to choose it yourself.</>}
      </p>
    </section>
  );
}
