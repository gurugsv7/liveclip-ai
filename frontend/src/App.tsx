import * as TooltipP from "@radix-ui/react-tooltip";
import { CheckCircle2, Info, XCircle } from "lucide-react";
import { useEffect } from "react";
import { Sidebar, TopBar } from "@/components/Chrome";
import { cn } from "@/lib/format";
import { useShortcuts } from "@/lib/shortcuts";
import { useStore } from "@/lib/store";
import { connectWS } from "@/lib/ws";
import { ClipsPage, ReviewQueue } from "@/pages/ClipsPage";
import { LiveMonitor } from "@/pages/LiveMonitor";
import { SettingsPage } from "@/pages/Settings";

let started = false;

export default function App() {
  const page = useStore((s) => s.page);
  const ws = useStore((s) => s.wsConnected);
  useShortcuts();
  useEffect(() => {
    if (!started) {
      started = true;
      connectWS();
    }
    const onFs = () => !document.fullscreenElement && useStore.getState().set({ fullscreen: false });
    document.addEventListener("fullscreenchange", onFs);
    return () => document.removeEventListener("fullscreenchange", onFs);
  }, []);

  return (
    <TooltipP.Provider>
      <div className="flex h-full flex-col">
        <TopBar />
        {!ws && <div role="status" className="border-b border-warn/20 bg-warn/10 px-4 py-1.5 text-center text-xs text-warn">Connecting to the LiveClip backend on 127.0.0.1:8765…</div>}
        <div className="flex min-h-0 flex-1 flex-col overflow-y-auto lg:flex-row lg:overflow-hidden">
          <main className="flex min-h-0 min-w-0 flex-auto shrink-0 flex-col lg:flex-1 lg:shrink">
            {page === "live" && <LiveMonitor />}
            {page === "clips" && <ClipsPage />}
            {page === "review" && <ReviewQueue />}
            {page === "settings" && <SettingsPage />}
          </main>
          {page === "live" && <Sidebar />}
        </div>
        <Toasts />
      </div>
    </TooltipP.Provider>
  );
}

function Toasts() {
  const toasts = useStore((s) => s.toasts);
  const dismiss = useStore((s) => s.dismissToast);
  return (
    <div className="pointer-events-none fixed bottom-4 left-1/2 z-50 flex -translate-x-1/2 flex-col items-center gap-2" role="status" aria-live="polite">
      {toasts.map((t) => (
        <button key={t.id} onClick={() => dismiss(t.id)}
          className={cn("pointer-events-auto flex max-w-lg items-center gap-2 rounded-lg border px-3 py-2 text-xs shadow-xl backdrop-blur animate-slideIn",
            t.kind === "ok" ? "border-ok/30 bg-ink-850/95 text-ink-100" : t.kind === "err" ? "border-live/40 bg-ink-850/95 text-live" : "border-violet-500/40 bg-ink-850/95 text-ink-100")}>
          {t.kind === "ok" ? <CheckCircle2 className="h-4 w-4 text-ok" /> : t.kind === "err" ? <XCircle className="h-4 w-4 text-live" /> : <Info className="h-4 w-4 text-violet-400" />}
          {t.text}
        </button>
      ))}
    </div>
  );
}
