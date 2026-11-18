import {
  AlertTriangle, Bot, CheckCircle2, ChevronLeft, ChevronRight, Cpu, Gauge, HardDrive,
  Maximize2, Minimize2, Play, Radio, Square, Wifi, WifiOff, XCircle,
} from "lucide-react";
import { useMemo } from "react";
import { api, media } from "@/lib/api";
import { ago, bytes, cn, CLIP_STATE, METHOD_LABEL } from "@/lib/format";
import { type Page, useStore } from "@/lib/store";
import { Badge, Button, Meter, Tip } from "./ui";
import { useAction } from "./StreamPanel";

export function Logo() {
  return (
    <div className="flex shrink-0 items-center gap-3 sm:border-r sm:border-ink-600 sm:pr-5">
      <div className="grid h-9 w-9 place-items-center bg-violet-500 sm:h-10 sm:w-10">
        <Play className="h-3.5 w-3.5 fill-white text-white" />
      </div>
      <div className="font-display text-[24px] font-black leading-none tracking-tight xl:text-[33px]">LiveClip AI</div>
    </div>
  );
}

const TABS: { id: Page; label: string }[] = [
  { id: "live", label: "Live Monitor" },
  { id: "clips", label: "Clips" },
  { id: "review", label: "Review Queue" },
  { id: "settings", label: "Settings" },
];

export function TopBar() {
  const page = useStore((s) => s.page);
  const setPage = useStore((s) => s.setPage);
  const active = useStore((s) => s.streams.filter((stream) => stream.slot >= 0 && stream.slot < 4 && s.live[stream.id]?.state === "live").length);
  const res = useStore((s) => s.resources);
  const sys = useStore((s) => s.system);
  const ws = useStore((s) => s.wsConnected);
  const clips = useStore((s) => s.clips);
  const fullscreen = useStore((s) => s.fullscreen);
  const setS = useStore((s) => s.set);
  const review = clips.filter((c) => c.status === "ready_for_review").length;
  const aiStatus = sys?.whisper.status ?? "unknown";
  const disk = res?.disk;
  const bw = res?.bandwidth;
  return (
    <header className={cn("flex shrink-0 flex-wrap items-center gap-x-6 border-b border-ink-600 bg-ink-900 px-3 sm:px-6", page === "clips" && "2xl:min-h-[84px]")}>
      <Logo />
      <nav className="order-last flex min-w-0 basis-full items-center gap-1 overflow-x-auto sm:order-none sm:basis-auto sm:gap-3" aria-label="Main">
        {TABS.map((t) => (
          <button key={t.id} onClick={() => setPage(t.id)} aria-current={page === t.id ? "page" : undefined}
            className={cn("focus-ring flex h-10 shrink-0 items-center gap-1.5 border-b-4 px-2 pt-1 font-display text-[13px] font-extrabold transition-colors sm:h-[55px] sm:px-3 xl:text-[17px]",
              page === t.id ? "border-violet-500 text-ink-100" : "border-transparent text-ink-300 hover:text-ink-100")}>
            {t.label}
            {t.id === "review" && review > 0 && <span className="bg-violet-500 px-1 font-mono text-[10px] text-white">{review}</span>}
          </button>
        ))}
      </nav>
      <div className="flex-1" />
      <div className="flex h-10 items-center gap-2 font-mono text-[11px] sm:gap-3">
        <Tip label={`${active} of 4 inputs live`}>
          <span className="flex items-center gap-1.5 text-ink-300"><Radio className={cn("h-3.5 w-3.5", active ? "text-live" : "text-ink-500")} />
            <span className="tabular whitespace-nowrap">{String(active).padStart(2, "0")} / 04 LIVE</span></span>
        </Tip>
        <Tip label={`Whisper: ${sys?.whisper.detail || aiStatus} · LLM: ${sys?.semantic.detail || sys?.semantic.status}`}>
          <span className={cn("hidden items-center gap-1.5 xl:flex", aiStatus === "ready" ? "text-ok" : aiStatus === "unavailable" || aiStatus === "error" ? "text-warn" : "text-ink-300")}>
            <Bot className="h-3.5 w-3.5" /> AI {aiStatus === "not_loaded" ? "idle" : aiStatus}
          </span>
        </Tip>
        <Tip label={bw ? `Capture ≈ ${bw.used_mbps} Mbps of ${bw.budget_mbps} Mbps budget · interface ${res?.net_rx_mbps ?? "—"} Mbps` : "Network"}>
          <span className={cn("flex items-center gap-1.5", bw?.over_budget ? "text-warn" : "text-ink-300")}>
            {ws ? <Wifi className="h-3.5 w-3.5" /> : <WifiOff className="h-3.5 w-3.5 text-live" />}
            <span className="hidden font-mono xl:inline">{ws ? "LOCAL (127.0.0.1)" : "OFFLINE"}</span>
            {ws && <span className="h-2 w-2 rounded-full bg-ok" aria-label="Connected" />}
          </span>
        </Tip>
        <Tip label={disk ? `${bytes(disk.free)} free · buffer ${bytes(disk.temp_bytes)} / ${bytes(disk.temp_budget)} · clips ${bytes(disk.clip_bytes)}` : "Disk"}>
          <span className={cn("hidden items-center gap-1.5 xl:flex", disk?.level === "critical" ? "text-live" : disk?.level === "warn" ? "text-warn" : "text-ink-300")}>
            <HardDrive className="h-3.5 w-3.5" /><span className="tabular font-mono">{disk ? bytes(disk.free) : "—"}</span>
          </span>
        </Tip>
        {page === "live" && (
          <Tip label={fullscreen ? "Exit fullscreen monitor (F)" : "Fullscreen monitor (F)"}>
            <button className="focus-ring rounded p-1 text-ink-300 hover:text-ink-100" onClick={() => toggleFullscreen(setS, fullscreen)} aria-label="Fullscreen">
              {fullscreen ? <Minimize2 className="h-4 w-4" /> : <Maximize2 className="h-4 w-4" />}
            </button>
          </Tip>
        )}
      </div>
    </header>
  );
}

export function toggleFullscreen(setS: (p: any) => void, current: boolean) {
  if (!current) document.documentElement.requestFullscreen?.().catch(() => undefined);
  else if (document.fullscreenElement) document.exitFullscreen().catch(() => undefined);
  setS({ fullscreen: !current, sidebarCollapsed: !current });
}

function Section({ title, right, children }: { title: string; right?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section className="border-b border-ink-600 py-3.5 last:border-b-0">
      <div className="mb-2.5 flex items-center justify-between"><h3 className="font-display text-xl font-black leading-none xl:text-[23px]">{title}</h3>{right}</div>
      {children}
    </section>
  );
}

export function Sidebar() {
  const collapsed = useStore((s) => s.sidebarCollapsed);
  const setS = useStore((s) => s.set);
  const clips = useStore((s) => s.clips);
  const events = useStore((s) => s.events);
  const jobs = useStore((s) => s.jobs);
  const warnings = useStore((s) => s.warnings);
  const res = useStore((s) => s.resources);
  const streams = useStore((s) => s.streams);
  const { busy, run } = useAction();
  const byId = useMemo(() => Object.fromEntries(streams.map((s) => [s.id, s])), [streams]);
  const recentClips = clips.filter((c) => c.status !== "discarded").slice(0, 3);
  const aiEvents = events.filter((e) => e.kind === "auto").slice(0, 8);
  const jobList = Object.values(jobs).filter((j) => j.state === "queued" || j.state === "running");
  const warns = Object.values(warnings);

  if (collapsed)
    return (
      <aside className="flex shrink-0 items-center border-t border-ink-750 bg-ink-900/60 p-2 lg:w-10 lg:flex-col lg:border-l lg:border-t-0">
        <button aria-label="Expand sidebar" className="focus-ring rounded p-1 text-ink-400 hover:text-ink-100" onClick={() => setS({ sidebarCollapsed: false })}>
          <ChevronLeft className="h-4 w-4" />
        </button>
        {warns.length > 0 && <AlertTriangle className="mt-3 h-4 w-4 text-warn" />}
      </aside>
    );

  return (
    <aside className="shrink-0 border-t border-ink-600 px-4 pb-4 lg:w-[27%] lg:overflow-y-auto lg:border-l lg:border-t-0 xl:px-[18px]" aria-label="Activity">
      <div className="flex min-h-[86px] items-center justify-between border-b border-ink-600">
        <h2 className="font-display text-[40px] font-black leading-none tracking-tight xl:text-5xl">Activity</h2>
        <button aria-label="Collapse sidebar" className="focus-ring rounded p-1 text-ink-400 hover:text-ink-100" onClick={() => setS({ sidebarCollapsed: true })}>
          <ChevronRight className="h-4 w-4" />
        </button>
      </div>
      {warns.length > 0 && (
        <Section title={`Warnings · ${warns.length}`}>
          <ul className="space-y-1.5">
            {warns.map((w) => (
              <li key={w.key} className={cn("flex gap-2 rounded-md border px-2 py-1.5 text-[11px] leading-snug",
                w.level === "error" ? "border-live/30 bg-live/10 text-live" : "border-warn/30 bg-warn/10 text-warn")}>
                {w.level === "error" ? <XCircle className="mt-px h-3.5 w-3.5 shrink-0 text-live" /> : <AlertTriangle className="mt-px h-3.5 w-3.5 shrink-0 text-warn" />}
                {w.message}
              </li>
            ))}
          </ul>
        </Section>
      )}
      <Section title="Quick actions">
        <div className="grid grid-cols-2 gap-1.5">
          <Button size="lg" className="font-mono" variant="primary" loading={busy === "sa"} onClick={() => run("sa", () => api.startAll(), "Starting all streams")}><Play className="h-3.5 w-3.5 fill-current" />Start all</Button>
          <Button size="lg" className="font-mono" variant="secondary" loading={busy === "xa"} onClick={() => run("xa", () => api.stopAll(), "All streams stopped")}><Square className="h-3.5 w-3.5" />Stop all</Button>
          {streams.filter((s) => s.live?.state === "live").map((s) => (
            <Button key={s.id} size="xs" variant="ghost" className="justify-start border border-ink-700" onClick={() => run("s" + s.id, () => api.save(s.id, 60, useStore.getState().latency[s.id] ?? 0), `Saved ${s.name}`)}>
              <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />Save 60s · {s.name}
            </Button>
          ))}
        </div>
      </Section>
      <Section title={`Recent clips (${recentClips.length})`} right={<button className="focus-ring font-mono text-[11px] text-violet-400 hover:underline" onClick={() => setS({ page: "clips" })}>All</button>}>
        {recentClips.length === 0 ? <Empty text="No clips yet. Save a moment or let AI detect one." /> : (
          <ul className="space-y-1.5">
            {recentClips.map((c) => (
              <li key={c.id}>
                <button onClick={() => setS({ page: "clips", focusClip: c.id })} className="focus-ring flex w-full items-center gap-2.5 border-b border-ink-750 py-1.5 text-left hover:bg-ink-800">
                  <div className="media-well relative aspect-video w-[36%] shrink-0 overflow-hidden">
                    {c.files.thumb_path && <img src={media.clip(c.id, "thumb", c.updated_at)} alt="" className="h-full w-full object-cover" />}
                    <span className="absolute bottom-0 left-0 h-[2px] w-full" style={{ background: c.streamer_color }} />
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="line-clamp-2 font-display text-sm font-extrabold leading-tight xl:text-base">{c.title || `${c.streamer_name} · ${METHOD_LABEL[c.detection_method]}`}</div>
                    <div className="mt-0.5 flex flex-wrap items-center gap-1.5 font-mono text-[10px] text-ink-400">
                      <Badge className={CLIP_STATE[c.status]?.tone}>{CLIP_STATE[c.status]?.label}</Badge>
                      {c.ai_score != null && <span className="tabular font-mono text-violet-400">{Math.round(c.ai_score * 100)}</span>}
                      <span>{ago(c.created_at)}</span>
                    </div>
                  </div>
                </button>
              </li>
            ))}
          </ul>
        )}
      </Section>
      <Section title={`AI detections (${aiEvents.length})`}>
        {aiEvents.length === 0 ? <Empty text="Watching for hype. Detection needs ~45s of baseline per stream." /> : (
          <ul className="space-y-1">
            {aiEvents.map((e) => (
              <li key={e.id} className="flex items-center gap-2 border-b border-ink-750 py-1.5 text-[11px] last:border-b-0">
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: byId[e.streamer_id]?.color ?? "#666" }} />
                <span className="truncate text-ink-200">{byId[e.streamer_id]?.name ?? "—"}</span>
                <span className="text-ink-500">{e.state}{e.merged_count && e.merged_count > 1 ? ` ·×${e.merged_count}` : ""}</span>
                <span className="ml-auto tabular font-mono text-violet-400">{Math.round(((e.final_score ?? e.peak_score) || 0) * 100)}</span>
                <span className="w-12 text-right font-mono text-ink-500">{ago(e.created_at)}</span>
              </li>
            ))}
          </ul>
        )}
      </Section>
      {jobList.length > 0 && (
      <Section title={`Processing queue · ${jobList.length}`}>
          <ul className="space-y-1">
            {jobList.slice(0, 8).map((j) => (
              <li key={j.id} className="flex items-center gap-2 text-[11px]">
                <span className={cn("h-1.5 w-1.5 rounded-full", j.state === "running" ? "animate-pulseDot bg-volt-400" : "bg-ink-500")} />
                <span className="font-mono text-ink-200">{j.kind}</span>
                <span className="truncate text-ink-500">{byId[j.streamer_id ?? ""]?.name}</span>
                <span className="ml-auto text-ink-400">{j.state}</span>
              </li>
            ))}
          </ul>
      </Section>
      )}
      <Section title="Resources">
        {!res ? <Empty text="Collecting…" /> : (
          <div className="space-y-2 text-[11px]">
            <ResRow icon={<Cpu className="h-3 w-3" />} label="CPU" value={`${res.cpu.toFixed(0)}%`} frac={res.cpu / 100} />
            <ResRow icon={<Gauge className="h-3 w-3" />} label="RAM" value={`${bytes(res.ram_used)} / ${bytes(res.ram_total)}`} frac={res.ram_percent / 100} />
            {res.gpu ? (
              <>
                <ResRow icon={<Cpu className="h-3 w-3" />} label={`GPU ${res.gpu.temp_c}°C`} value={`${res.gpu.util.toFixed(0)}%${res.gpu.enc != null ? ` · enc ${res.gpu.enc}%` : ""}`} frac={res.gpu.util / 100} />
                <ResRow icon={<Cpu className="h-3 w-3" />} label="VRAM" value={`${(res.gpu.vram_used_mb / 1024).toFixed(1)} / ${(res.gpu.vram_total_mb / 1024).toFixed(1)} GB`} frac={res.gpu.vram_used_mb / res.gpu.vram_total_mb} />
              </>
            ) : <div className="text-ink-500">GPU metrics unavailable (nvidia-smi not found)</div>}
            <ResRow icon={<Wifi className="h-3 w-3" />} label="Download" value={`${res.net_rx_mbps?.toFixed(1) ?? "—"} Mbps`} frac={(res.net_rx_mbps ?? 0) / Math.max(1, res.bandwidth.budget_mbps / 0.8)} />
            <ResRow icon={<HardDrive className="h-3 w-3" />} label="Buffer" value={`${bytes(res.disk.temp_bytes)} / ${bytes(res.disk.temp_budget)}`} frac={res.disk.temp_bytes / res.disk.temp_budget} />
            <ResRow icon={<CheckCircle2 className="h-3 w-3" />} label="Clips + exports" value={bytes(res.disk.clip_bytes + res.disk.export_bytes)} frac={null} />
          </div>
        )}
      </Section>
    </aside>
  );
}

function ResRow({ icon, label, value, frac }: { icon: React.ReactNode; label: string; value: string; frac: number | null }) {
  return (
    <div>
      <div className="mb-0.5 flex items-center gap-1.5 text-ink-300">{icon}<span>{label}</span><span className="tabular ml-auto font-mono text-ink-200">{value}</span></div>
      {frac != null && <Meter value={frac} color={frac > 0.85 ? "violet" : "volt"} />}
    </div>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="py-1 text-[11px] text-ink-500">{text}</div>;
}
