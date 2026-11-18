import { ChevronDown, ChevronUp } from "lucide-react";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/format";
import { useStore } from "@/lib/store";
import type { Streamer } from "@/lib/types";
import { DRAG_TYPE, EmptySlot, StreamDialog, StreamPanel } from "@/components/StreamPanel";
import { Tip } from "@/components/ui";

const COMPACT_MONITOR_QUERY = "(min-width: 1024px) and (max-height: 820px)";

export function LiveMonitor() {
  const streams = useStore((s) => s.streams);
  const live = useStore((s) => s.live);
  const selected = useStore((s) => s.selected);
  const expanded = useStore((s) => s.expanded);
  const setS = useStore((s) => s.set);
  const [dialog, setDialog] = useState<{ slot: number; replacing: Streamer | null } | null>(null);
  const bySlot: (Streamer | undefined)[] = [0, 1, 2, 3].map((i) => streams.find((s) => s.slot === i));
  const exp = expanded ? streams.find((s) => s.id === expanded) : null;
  const others = bySlot.flatMap((s, i) => (s && s.id !== exp?.id ? [{ s, i }] : []));
  const [dragId, setDragId] = useState<string | null>(null);
  const [dropSlot, setDropSlot] = useState<number | null>(null);
  const move = async (id: string, slot: number) => {
    const me = streams.find((s) => s.id === id);
    if (!me || me.slot === slot) return;
    // swap on screen at once; the server confirms (or we reload on error)
    setS({ streams: streams.map((s) => (s.id === id ? { ...s, slot } : s.slot === slot ? { ...s, slot: me.slot } : s)), selected: slot });
    try {
      useStore.getState().setStreams(await api.moveStream(id, slot));
    } catch (e) {
      useStore.getState().toast("err", (e as Error).message);
      useStore.getState().setStreams(await api.streams());
    }
  };
  const liveCount = bySlot.filter((s) => s && live[s.id]?.state === "live").length;

  return (
    <div className="flex flex-col px-3 pb-3 lg:min-h-0 lg:flex-1 lg:overflow-y-auto xl:px-5">
      <div className="monitor-heading flex min-h-16 shrink-0 items-center justify-between gap-2 border-b border-ink-600">
        <h1 className="page-title xl:text-[46px]">Live Monitor</h1>
        <span className="whitespace-nowrap font-mono text-xs font-semibold sm:text-base">{String(liveCount).padStart(2, "0")} / 04 LIVE</span>
      </div>
      <div className={cn("gap-3 py-3 xl:gap-x-4", exp ? "flex flex-col lg:flex-row" : "monitor-grid grid grid-cols-1 sm:grid-cols-2 lg:min-h-0 lg:flex-1 lg:grid-rows-2")}>
        {exp ? (
          <>
            {/* expanded stream takes all remaining space; the panel must be stretched or its video area collapses to 0 px */}
            <div className="flex min-h-0 min-w-0 flex-1 [&>*]:flex-1" data-testid="expanded-slot">
              <StreamPanel streamer={exp} selected onSelect={() => undefined} onReplace={(s) => setDialog({ slot: s.slot, replacing: s })} />
            </div>
            {others.length > 0 && (
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:flex lg:w-[34%] lg:shrink-0 lg:flex-col">
                {others.map(({ s, i }) => (
                  <div key={s.id} className="flex min-h-0 flex-1 [&>*]:flex-1">
                    <StreamPanel streamer={s} selected={selected === i} onSelect={() => setS({ selected: i })} onReplace={(x) => setDialog({ slot: x.slot, replacing: x })} />
                  </div>
                ))}
              </div>
            )}
          </>
        ) : (
          bySlot.map((s, i) => (
            <div key={s?.id ?? `empty-${i}`}
              className={cn("flex min-h-0 min-w-0 [&>*]:flex-1", dragId && dropSlot === i && s?.id !== dragId && "outline outline-2 outline-offset-2 outline-violet-500")}
              onDragOver={(e) => { if (e.dataTransfer.types.includes(DRAG_TYPE)) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; setDropSlot(i); } }}
              onDragLeave={(e) => { if (!e.currentTarget.contains(e.relatedTarget as Node)) setDropSlot((d) => (d === i ? null : d)); }}
              onDrop={(e) => { e.preventDefault(); const id = e.dataTransfer.getData(DRAG_TYPE); setDropSlot(null); setDragId(null); if (id) move(id, i); }}>
              {s ? (
                <StreamPanel streamer={s} fitGrid selected={selected === i} onSelect={() => setS({ selected: i })}
                  onReplace={(x) => setDialog({ slot: x.slot, replacing: x })}
                  onMove={(slot) => move(s.id, slot)} dragging={dragId === s.id}
                  onDragStart={() => setDragId(s.id)} onDragEnd={() => { setDragId(null); setDropSlot(null); }} />
              ) : (
                <EmptySlot slot={i} onAdd={(slot) => setDialog({ slot, replacing: null })} />
              )}
            </div>
          ))
        )}
      </div>
      <Timeline />
      <StreamDialog open={!!dialog} onOpenChange={(o) => !o && setDialog(null)} slot={dialog?.slot ?? 0} replacing={dialog?.replacing} />
    </div>
  );
}

function Timeline() {
  const preference = useStore((s) => s.timelineOpen);
  const [compact, setCompact] = useState(() => window.matchMedia?.(COMPACT_MONITOR_QUERY).matches ?? false);
  const open = preference ?? !compact;
  const setS = useStore((s) => s.set);
  const streams = useStore((s) => s.streams);
  const bySlot = [0, 1, 2, 3].map((slot) => streams.find((s) => s.slot === slot));
  const [data, setData] = useState<Record<string, any>>({});
  const [now, setNow] = useState(Date.now() / 1000);
  const WINDOW = 600;

  useEffect(() => {
    const query = window.matchMedia?.(COMPACT_MONITOR_QUERY);
    if (!query) return;
    const update = () => setCompact(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    const load = async () => {
      try {
        const d = await api.timeline();
        if (alive) {
          setData(d);
          setNow(Date.now() / 1000);
        }
      } catch {
        /* ignore */
      }
    };
    load();
    const iv = setInterval(load, 4000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, [open]);

  const x = (t: number) => `${Math.max(0, Math.min(100, ((t - (now - WINDOW)) / WINDOW) * 100))}%`;
  const w = (a: number, b: number) => `${Math.max(0.15, ((Math.min(b, now) - Math.max(a, now - WINDOW)) / WINDOW) * 100)}%`;

  return (
    <section className="relative shrink-0 border border-ink-500 bg-ink-900/70">
      <button onClick={() => setS({ timelineOpen: !open })} className="focus-ring flex min-h-9 w-full flex-wrap items-center gap-x-3 px-3 py-1 text-left" aria-expanded={open}>
        <span className="font-display text-lg font-black xl:text-[22px]">Synchronized timeline</span>
        {!compact && <TimelineLegend />}
        <span className="ml-auto flex items-center gap-2 font-mono text-[10px] text-ink-400">
          <span className="h-2 w-2 rounded-full bg-live" /> NOW
          {open ? <ChevronDown className="h-4 w-4" /> : <ChevronUp className="h-4 w-4" />}
        </span>
      </button>
      {open && (
        <div className={cn("space-y-1 border-t border-ink-500 px-3 py-1.5", compact && "absolute inset-x-[-1px] bottom-full z-30 border border-ink-500 bg-ink-900 shadow-lg")}>
          {compact && <TimelineLegend />}
          <div className="flex justify-between pl-[52px] font-mono text-[10px] text-ink-500">
            <span>−10m</span><span>−7.5m</span><span>−5m</span><span>−2.5m</span><span>Now</span>
          </div>
          {bySlot.map((s, slot) => {
            const d = s ? data[s.id] : null;
            return (
              <div key={slot} className="flex items-center gap-2" aria-label={s?.name ?? `Unassigned input ${slot + 1}`}>
                <div className="flex w-11 items-center justify-between font-mono text-[11px] font-semibold" title={s?.name}>
                  <span className="border border-ink-500 px-1.5 leading-tight">{String(slot + 1).padStart(2, "0")}</span>
                  {s && <span className="h-2 w-2 rounded-full" style={{ background: s.color }} />}
                </div>
                <div className="timeline-track relative h-3 flex-1 overflow-hidden border-r-2 border-live bg-ink-750">
                  {d?.available?.map(([start, end]: number[], i: number) => (
                    <div key={i} className="absolute inset-y-0 bg-volt-500" style={{ left: x(start), width: w(start, end) }} />
                  ))}
                  {d?.protected?.map((p: number[], i: number) => (
                    <div key={i} className="absolute inset-y-0.5 rounded-sm bg-violet-500/60" style={{ left: x(p[0]), width: w(p[0], p[1]) }} />
                  ))}
                  {d?.clips?.map((c: any) => (
                    <Tip key={c.id} label={`Clip · ${c.status}`}>
                      <div className="absolute bottom-0 h-1 bg-ink-100" style={{ left: x(c.t0), width: w(c.t0, c.t1) }} />
                    </Tip>
                  ))}
                  {d?.events?.map((e: any) => (
                    <Tip key={e.id} label={`${e.kind} · score ${Math.round((e.score ?? 0) * 100)} · ${e.state}`}>
                      <div className={cn("absolute top-0 h-full w-[3px]", e.kind === "auto" ? "bg-fuchsia-300" : "bg-warn",
                        (e.score ?? 0) > 0.75 && "w-[5px]")} style={{ left: x(e.t) }} />
                    </Tip>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
  );
}

function TimelineLegend() {
  return (
    <span className="order-last flex w-full flex-wrap items-center gap-2 font-mono text-[10px] text-ink-400 xl:order-none xl:w-auto">
      <Legend c="bg-volt-500" t="Buffered" /><Legend c="bg-violet-500/70" t="Protected" /><Legend c="bg-ink-100" t="Clip" /><Legend c="bg-fuchsia-300" t="AI event" /><Legend c="bg-warn" t="Manual" />
    </span>
  );
}

function Legend({ c, t }: { c: string; t: string }) {
  return <span className="flex items-center gap-1"><span className={cn("h-2 w-2 rounded-sm", c)} />{t}</span>;
}
