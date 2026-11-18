import {
  Bot, BotOff, ChevronDown, Expand, MessageSquare, MoreHorizontal, Pause, Play, Plus, Power, RefreshCw,
  Shrink, Trash2, Volume2, VolumeX,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api, media } from "@/lib/api";
import { bytes, cn, dur, qualityLabel, STATE_LABEL } from "@/lib/format";
import { useStore } from "@/lib/store";
import type { Streamer } from "@/lib/types";
import { HlsPlayer } from "./HlsPlayer";
import { Badge, Button, Confirm, Dialog, Field, IconBtn, inputCls, Meter, PlatformIcon, Tip } from "./ui";

const STATE_TONE: Record<string, string> = {
  live: "border-volt-500 bg-volt-500 text-white",
  connecting: "bg-volt-500/10 text-volt-400 border-volt-500/30",
  starting: "bg-volt-500/10 text-volt-400 border-volt-500/30",
  reconnecting: "bg-warn/10 text-warn border-warn/30",
  offline: "bg-ink-700/50 text-ink-300 border-ink-600",
  failed: "bg-live/10 text-live border-live/30",
  error: "bg-live/10 text-live border-live/30",
  stopped: "bg-ink-700/50 text-ink-300 border-ink-600",
};

export function useAction() {
  const toast = useStore((s) => s.toast);
  const [busy, setBusy] = useState<string | null>(null);
  const run = async (key: string, fn: () => Promise<unknown>, ok?: string) => {
    setBusy(key);
    try {
      await fn();
      if (ok) toast("ok", ok);
    } catch (e) {
      toast("err", (e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  return { busy, run };
}

export function EmptySlot({ slot, onAdd }: { slot: number; onAdd: (slot: number) => void }) {
  return (
    <button onClick={() => onAdd(slot)} aria-label={`Add stream to slot ${slot + 1}`}
      className="focus-ring group flex w-full flex-col border border-ink-500 bg-ink-900 text-left hover:border-violet-500">
      <div className="flex min-h-8 w-full shrink-0 items-center gap-3 px-2">
        <span className="border border-ink-500 px-1.5 font-mono text-sm font-semibold">{String(slot + 1).padStart(2, "0")}</span>
        <span className="flex-1 font-mono text-sm font-semibold">Unassigned</span>
        <Badge className={STATE_TONE.offline}>Offline</Badge>
      </div>
      <div className="media-well unassigned-well grid min-h-36 w-full flex-1 place-items-center border-t border-ink-500 text-center sm:aspect-[2.9] lg:aspect-auto lg:min-h-0">
        <div className="flex flex-col items-center gap-2 font-mono text-sm font-semibold">
          <span className="grid h-10 w-10 place-items-center border-2 border-current group-hover:bg-violet-500"><Plus className="h-6 w-6" /></span>
          <span>Add stream</span>
        </div>
      </div>
    </button>
  );
}

function BufferBar({ streamer }: { streamer: Streamer }) {
  const live = useStore((s) => s.live[streamer.id]);
  const events = useStore((s) => s.events);
  const retention = useStore((s) => s.config?.buffer?.retention_seconds ?? 120);
  if (!live) return null;
  const now = live.media_now;
  const span = Math.max(retention + 30, 150);
  const x = (t: number) => `${Math.max(0, Math.min(100, ((t - (now - span)) / span) * 100))}%`;
  const b = live.buffer;
  const evs = events.filter((e) => e.session_id === live.session_id && e.t_event > now - span);
  return (
    <Tip label={`${b.segments} segments · ${dur(b.seconds)} buffered · ${bytes(b.bytes)} · ${b.protected_segments} protected`}>
      <div className="media-well signal-strip flex items-center gap-2 border-t border-[#524f45] px-2.5 py-1.5 font-mono text-[9px] font-semibold xl:text-[10px]" aria-label="Rolling buffer timeline">
        <span className="shrink-0 uppercase">Rolling buffer</span>
        <div role="meter" aria-label="Buffered footage" aria-valuemin={0} aria-valuemax={retention} aria-valuenow={Math.min(retention, b.seconds)} className="relative h-2.5 min-w-0 flex-1 overflow-hidden">
        {b.t_first != null && b.t_last != null && (
          <div className="absolute inset-y-0" style={{ left: x(b.t_first), right: `calc(100% - ${x(b.t_last)})` }} />
        )}
        {b.protected.map((p) => (
          <span key={p.id} className="absolute inset-y-0 bg-violet-500" style={{ left: x(p.t_start), right: `calc(100% - ${x(Math.min(p.t_end, now))})` }} />
        ))}
        {evs.map((e) => (
          <span key={e.id} className={cn("absolute top-0 h-full w-0.5", e.kind === "auto" ? "bg-[#f9baaa]" : "bg-white")} style={{ left: x(e.t_event) }} />
        ))}
        <span className="absolute inset-y-0 border-l border-dashed border-white/60" style={{ left: x(now - retention) }} />
        </div>
        <span className="shrink-0">{dur(b.seconds)} / {dur(retention)}</span>
      </div>
    </Tip>
  );
}

export function StreamPanel({ streamer, selected, onSelect, onReplace, fitGrid }: {
  streamer: Streamer; selected: boolean; onSelect: () => void; onReplace: (s: Streamer) => void; fitGrid?: boolean;
}) {
  const live = useStore((s) => s.live[streamer.id]);
  const meters = useStore((s) => s.meters[streamer.id]);
  const audible = useStore((s) => s.audible.includes(streamer.id));
  const allowMulti = useStore((s) => !!s.config?.audio_monitor?.allow_multi);
  const toggleAudio = useStore((s) => s.toggleAudio);
  const expanded = useStore((s) => s.expanded === streamer.id);
  const paused = useStore((s) => !!s.pausedPreview[streamer.id]);
  const latency = useStore((s) => s.latency[streamer.id] ?? 0);
  const clips = useStore((s) => s.clips);
  const setS = useStore((s) => s.set);
  const { busy, run } = useAction();
  const [saveOpen, setSaveOpen] = useState(false);
  const [menu, setMenu] = useState(false);
  const [confirmDel, setConfirmDel] = useState(false);
  const [confirmStop, setConfirmStop] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menu && !saveOpen) return;
    const h = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenu(false);
        setSaveOpen(false);
      }
    };
    window.addEventListener("mousedown", h);
    return () => window.removeEventListener("mousedown", h);
  }, [menu, saveOpen]);

  const state = live?.state ?? "stopped";
  const isLive = state === "live";
  const pending = useMemo(
    () => clips.filter((c) => c.streamer_id === streamer.id && ["protected", "processing", "detected"].includes(c.status)).length,
    [clips, streamer.id],
  );
  const aiOn = live ? live.ai_enabled : streamer.ai_enabled;
  const src = live && (isLive || live.buffer.segments > 1) ? media.live(live.session_id) : null;
  const score = meters?.score ?? 0;
  const hot = score >= (useStore.getState().config?.detection?.trigger_threshold ?? 0.62);

  const save = (sec: number) => run("save", () => api.save(streamer.id, sec, latency), `Saving last ${sec}s of ${streamer.name}`);

  return (
    <div onMouseDown={onSelect}
      className={cn("group relative flex h-full min-w-0 flex-col border bg-ink-900",
        selected || hot ? "border-violet-500" : "border-ink-500")}
      data-testid={`panel-${streamer.slot}`}>
      {/* header */}
      <div className="flex min-h-8 shrink-0 items-center gap-2 px-2">
        <span className="border border-ink-500 px-1.5 font-mono text-sm font-semibold">{String(streamer.slot + 1).padStart(2, "0")}</span>
        <PlatformIcon platform={streamer.platform} />
        <div className="min-w-0 flex-1 truncate font-mono text-sm font-semibold tracking-tight xl:text-base">{streamer.name}</div>
        <Badge className={STATE_TONE[state]}>
          {isLive && <span className="h-1.5 w-1.5 animate-pulseDot rounded-full bg-live" />}
          {STATE_LABEL[state] ?? state}
        </Badge>
        {live?.height && (
          <Tip label={`Source: ${live.width}×${live.height} ${live.video_codec ?? ""} @ ${live.fps ?? "?"} fps · requested ${live.quality_requested?.[0] ?? "—"} · got ${live.quality_selected ?? "—"}`}>
            <Badge className={cn("border-ink-600 text-ink-200", live.height >= 1080 && "border-violet-500/40 text-violet-400")}>
              {qualityLabel(live.width, live.height, live.fps)}
            </Badge>
          </Tip>
        )}
        {live?.measured_kbps != null && (
          <span className="tabular hidden font-mono text-[10px] text-ink-400 xl:inline">{(live.measured_kbps / 1000).toFixed(1)} Mbps</span>
        )}
      </div>

      {/* video */}
      <div className={cn("media-well relative flex-1 overflow-hidden border-t border-ink-500", expanded ? "aspect-video min-h-44" : src ? "aspect-[2.16] min-h-36" : "aspect-[2.9] min-h-36", fitGrid && "lg:aspect-auto lg:min-h-0")}>
        <HlsPlayer src={src} muted={!audible} paused={paused} className="absolute inset-0"
          onLatency={(l) => useStore.setState((s) => ({ latency: { ...s.latency, [streamer.id]: l } }))} />
        {!live && (
          <div className="media-well unassigned-well absolute inset-0 grid place-items-center">
            <div className="text-center">
              <div className="text-xs text-stone-200">Not monitoring</div>
              <Button variant="primary" size="md" className="mt-2" loading={busy === "start"}
                onClick={() => run("start", () => api.start(streamer.id), `Monitoring ${streamer.name}`)}>
                <Power className="h-3.5 w-3.5" /> Start monitoring
              </Button>
            </div>
          </div>
        )}
        {live && !src && (
          <div className="absolute inset-0 grid place-items-center">
            <div className="max-w-[80%] text-center text-xs text-stone-200">
              {state === "failed" || state === "error" ? <span className="text-red-300">{live.detail}</span>
                : state === "offline" ? "Channel offline — checking periodically"
                : state === "reconnecting" ? live.detail : "Connecting to source…"}
            </div>
          </div>
        )}
        {/* overlays */}
        {live && (
          <div className="pointer-events-none absolute left-2 top-2 flex items-center gap-1.5">
            {isLive && <span className="bg-black/80 px-1.5 py-0.5 font-mono text-[10px]"><span className="text-red-400">●</span> REC {dur(live.recording_s)}</span>}
            {latency > 0 && <span className="bg-black/80 px-1.5 py-0.5 font-mono text-[10px]">−{latency.toFixed(1)}s</span>}
            {paused && <span className="bg-black/80 px-1.5 py-0.5 text-[10px] text-amber-200">Preview paused · capture running</span>}
          </div>
        )}
        {live && (
          <div className="pointer-events-none absolute right-2 top-2 flex items-center gap-1.5">
            <span className={cn("flex items-center gap-1 bg-black/80 px-1.5 py-0.5 text-[10px]", aiOn ? "text-lime-200" : "text-stone-300")}>
              {aiOn ? <Bot className="h-3 w-3" /> : <BotOff className="h-3 w-3" />} {aiOn ? (meters?.open ? "Candidate open" : "AI watching") : "AI off"}
            </span>
            {pending > 0 && <span className="rounded bg-violet-500/80 px-1.5 py-0.5 text-[10px] text-white">{pending} clip{pending > 1 ? "s" : ""} processing</span>}
          </div>
        )}
      </div>

      {live && (
        <div className="media-well signal-strip grid grid-cols-4 divide-x divide-[#524f45] border-t border-[#524f45]">
          <Signal label="Audio" value={meters?.audio ?? 0} readout={meters?.level_db == null ? "—" : `${Math.round(meters.level_db)} dB`}
            detail={`Loudness vs. ${streamer.name}'s baseline${meters?.sound_top ? ` · hears ${meters.sound_top}` : ""}${meters?.reaction ? ` · reaction ${Math.round(meters.reaction * 100)}` : ""}${(meters?.game_noise ?? 0) >= 0.35 ? " · game noise discounted" : ""}`} />
          <Signal label="Chat" value={meters?.chat ?? null} readout={meters?.chat_rate == null ? "—" : `${meters.chat_rate}/s`}
            detail={meters?.chat == null ? `Chat: ${live.chat.status}${live.chat.detail ? ` · ${live.chat.detail}` : ""}` : "Chat activity vs. baseline"} />
          <Signal label="Speech" value={meters?.speech ?? 0} readout={`${Math.round((meters?.speech ?? 0) * 100)}%`}
            detail={meters?.speech_text ? `Whisper heard "${meters.speech_text.slice(-90)}"` : "Excited speech from live transcription"} />
          <Signal label="Hype" value={score} readout={`${Math.round(score * 100)}/100`} detail="Combined hype score" peak={hot} />
        </div>
      )}
      <BufferBar streamer={streamer} />

      {/* controls */}
      <div className="stream-controls flex shrink-0 flex-wrap items-center gap-1 border-t border-ink-500 px-2 py-1.5 font-mono xl:gap-2" ref={menuRef}>
        <div className="relative flex">
          <Button size="md" variant="primary" disabled={!isLive} loading={busy === "save"} onClick={() => save(60)}>
            Save 60s
          </Button>
          <Button size="md" variant="primary" className="border-l border-violet-700 px-1.5" disabled={!isLive}
            aria-label="Choose save length" onClick={() => setSaveOpen((o) => !o)}><ChevronDown className="h-3.5 w-3.5" /></Button>
          {saveOpen && (
            <div className="absolute bottom-full left-0 z-20 mb-1 w-32 rounded-lg border border-ink-600 bg-ink-800 p-1 shadow-xl animate-slideIn">
              {[15, 30, 60, 120].map((s) => (
                <button key={s} className="focus-ring block w-full rounded px-2 py-1 text-left text-xs hover:bg-ink-700"
                  onClick={() => { setSaveOpen(false); save(s); }}>Save last {s}s</button>
              ))}
            </div>
          )}
        </div>
        <Button size="md" aria-label="Mark moment (B)" disabled={!isLive} onClick={() => run("mark", () => api.mark(streamer.id, latency), "Moment marked & protected")}>Mark</Button>
        <IconBtn className="h-8 w-auto whitespace-nowrap border-ink-600 px-2 text-[12px] font-semibold" label={aiOn ? "Disable AI detection (recording continues)" : "Enable AI detection"} active={aiOn} tone="ok"
          onClick={() => run("ai", () => api.setAI(streamer.id, !aiOn))} disabled={!live}>
          <span className={cn("h-2 w-2 rounded-full", aiOn ? "bg-ok" : "bg-ink-600")} />{aiOn ? "AI on" : "AI off"}
        </IconBtn>
        <IconBtn label={paused ? "Resume preview" : "Pause preview (capture keeps running)"} active={paused} tone="live" disabled={!src}
          onClick={() => setS({ pausedPreview: { ...useStore.getState().pausedPreview, [streamer.id]: !paused } })}>
          {paused ? <Play className="h-4 w-4" /> : <Pause className="h-4 w-4" />}
        </IconBtn>
        <div className="flex-1" />
        {live && (
          <Tip label={`Chat: ${live.chat.status}${live.chat.detail ? ` — ${live.chat.detail}` : ""}`}>
            <span className={cn("hidden items-center gap-1 font-mono text-[10px] 2xl:flex", live.chat.status === "connected" ? "text-volt-400" : "text-ink-500")}>
              <MessageSquare className="h-3 w-3" />{meters?.chat_rate != null && live.chat.status === "connected" ? `${meters.chat_rate}/s` : "—"}
            </span>
          </Tip>
        )}
        <IconBtn label={audible ? "Mute dashboard audio (M)" : allowMulti ? "Add to audible streams" : "Listen to this stream only (M)"} active={audible} tone="volt"
          onClick={(e) => { e.stopPropagation(); toggleAudio(streamer.id, allowMulti); }} disabled={!src} data-testid="audio-btn">
          {audible ? <Volume2 className="h-4 w-4" /> : <VolumeX className="h-4 w-4" />}
        </IconBtn>
        <IconBtn label={expanded ? "Back to grid (E)" : "Expand (E)"} onClick={() => setS({ expanded: expanded ? null : streamer.id })}>
          {expanded ? <Shrink className="h-4 w-4" /> : <Expand className="h-4 w-4" />}
        </IconBtn>
        <div className="relative">
          <IconBtn label="More" onClick={() => setMenu((m) => !m)}><MoreHorizontal className="h-4 w-4" /></IconBtn>
          {menu && (
            <div className="absolute bottom-full right-0 z-20 mb-1 w-48 rounded-lg border border-ink-600 bg-ink-800 p-1 text-xs shadow-xl animate-slideIn">
              <MenuItem icon={<Trash2 className="h-3.5 w-3.5" />} disabled={!live} onClick={() => { setMenu(false); setConfirmDel(true); }}>Delete buffer…</MenuItem>
              <MenuItem icon={<RefreshCw className="h-3.5 w-3.5" />} onClick={() => { setMenu(false); onReplace(streamer); }}>Replace stream…</MenuItem>
              {live ? (
                <MenuItem icon={<Power className="h-3.5 w-3.5" />} danger onClick={() => { setMenu(false); setConfirmStop(true); }}>Stop monitoring</MenuItem>
              ) : (
                <MenuItem icon={<Power className="h-3.5 w-3.5" />} onClick={() => { setMenu(false); run("start", () => api.start(streamer.id)); }}>Start monitoring</MenuItem>
              )}
            </div>
          )}
        </div>
      </div>

      <Confirm open={confirmDel} onOpenChange={setConfirmDel} title={`Delete ${streamer.name}'s buffer?`} confirmLabel="Delete unprotected footage"
        body={<>This removes all buffered, <b>unprotected</b> footage for this stream ({live ? `${live.buffer.segments} segments, ${bytes(live.buffer.bytes)}` : "—"}).
          Saved clips and footage protected by pending clips are kept. This can't be undone.</>}
        onConfirm={() => run("del", async () => {
          const r = await api.deleteBuffer(streamer.id);
          useStore.getState().toast("ok", `Deleted ${r.deleted} segments, kept ${r.protected_kept} protected`);
        })} />
      <Confirm open={confirmStop} onOpenChange={setConfirmStop} title={`Stop monitoring ${streamer.name}?`} confirmLabel="Stop"
        body="Capture stops. Any open AI candidate is finalized with the footage already captured, and queued clips finish from protected segments. Unprotected buffer is then cleaned up."
        onConfirm={() => run("stop", () => api.stop(streamer.id), "Stopped")} />
    </div>
  );
}

function Signal({ label, value, readout, detail, peak }: { label: string; value: number | null; readout: string; detail: string; peak?: boolean }) {
  return (
    <Tip label={detail}>
      <div className="min-w-0 px-2 py-2 font-mono text-[9px] xl:px-2.5 xl:text-[10px]">
        <div className="mb-1 flex justify-between gap-1 uppercase">
          <span>{label}</span><span className="truncate">{readout}</span>
        </div>
        <Meter value={value} label={label} peak={peak} />
      </div>
    </Tip>
  );
}

function MenuItem({ icon, children, onClick, danger, disabled }: { icon: React.ReactNode; children: React.ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }) {
  return (
    <button disabled={disabled} onClick={onClick}
      className={cn("focus-ring flex w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-ink-700 disabled:opacity-40", danger && "text-live")}>
      {icon}{children}
    </button>
  );
}

export function StreamDialog({ open, onOpenChange, slot, replacing }: {
  open: boolean; onOpenChange: (o: boolean) => void; slot: number; replacing?: Streamer | null;
}) {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [priority, setPriority] = useState(2);
  const [startNow, setStartNow] = useState(true);
  const { busy, run } = useAction();
  const allowLocal = useStore((s) => s.system?.allow_local_sources);
  useEffect(() => {
    if (open) {
      setName(replacing?.name ?? "");
      setUrl("");
      setPriority(replacing?.priority ?? 2);
    }
  }, [open, replacing]);
  const submit = () =>
    run("add", async () => {
      if (replacing) await api.replace(replacing.id, url, name || undefined);
      else await api.addStream({ name, url, slot, priority, start: startNow });
      useStore.getState().setStreams(await api.streams());
      onOpenChange(false);
    }, replacing ? "Stream replaced — new isolated session" : "Stream added");
  return (
    <Dialog open={open} onOpenChange={onOpenChange} title={replacing ? `Replace ${replacing.name}` : `Add stream · slot ${slot + 1}`}
      description={replacing ? "The current session is stopped safely and a fresh, isolated session starts with the new URL." : "Twitch, YouTube Live or Kick channel URL. Each stream gets its own capture, audio, chat and storage."}>
      <div className="space-y-3">
        <Field label="Display name"><input className={inputCls} value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Speed" /></Field>
        <Field label="Stream URL" hint={allowLocal ? "Test fixtures: local:<name>" : "https://www.twitch.tv/<channel> · https://www.youtube.com/@handle/live · https://kick.com/<channel>"}>
          <input className={inputCls} value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://www.twitch.tv/…" autoFocus
            onKeyDown={(e) => e.key === "Enter" && url && submit()} />
        </Field>
        {!replacing && (
          <div className="grid grid-cols-2 gap-3">
            <Field label="Capture priority" hint="1 = always full quality">
              <select className={inputCls} value={priority} onChange={(e) => setPriority(+e.target.value)}>
                {[1, 2, 3, 4].map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
            </Field>
            <label className="mt-5 flex items-center gap-2 text-sm text-ink-200">
              <input type="checkbox" checked={startNow} onChange={(e) => setStartNow(e.target.checked)} className="accent-violet-500" /> Start monitoring now
            </label>
          </div>
        )}
      </div>
      <div className="mt-5 flex justify-end gap-2">
        <Button variant="ghost" size="md" onClick={() => onOpenChange(false)}>Cancel</Button>
        <Button variant="primary" size="md" disabled={!url} loading={busy === "add"} onClick={submit}>{replacing ? "Replace" : "Add stream"}</Button>
      </div>
    </Dialog>
  );
}
