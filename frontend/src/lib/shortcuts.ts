import { useEffect } from "react";
import { api } from "./api";
import { useStore } from "./store";

export function isTyping(el: EventTarget | null): boolean {
  if (!(el instanceof HTMLElement)) return false;
  const tag = el.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || el.isContentEditable;
}

/** Global keyboard shortcuts for the Live Monitor. Ignored while typing. */
export function useShortcuts() {
  useEffect(() => {
    const onKey = async (e: KeyboardEvent) => {
      if (e.ctrlKey || e.metaKey || e.altKey || isTyping(e.target)) return;
      const s = useStore.getState();
      if (s.page !== "live") return;
      const map: Record<string, string> = s.config?.shortcuts ?? {};
      const key = e.key.toLowerCase();
      const action = Object.entries(map).find(([, k]) => k.toLowerCase() === key)?.[0];
      const stream = s.streams.find((x) => x.slot === s.selected);
      const run = async (fn: () => Promise<unknown>, ok: string) => {
        try {
          await fn();
          s.toast("ok", ok);
        } catch (err) {
          s.toast("err", (err as Error).message);
        }
      };
      if (!action) {
        if (key === "f") {
          const { toggleFullscreen } = await import("@/components/Chrome");
          toggleFullscreen(s.set, s.fullscreen);
        }
        if (key === "escape" && s.expanded) s.set({ expanded: null });
        return;
      }
      e.preventDefault();
      if (action.startsWith("select")) {
        s.set({ selected: Number(action.slice(6)) - 1 });
        return;
      }
      if (!stream) return;
      const lat = s.latency[stream.id] ?? 0;
      switch (action) {
        case "mute":
          s.toggleAudio(stream.id, !!s.config?.audio_monitor?.allow_multi);
          break;
        case "save60":
          await run(() => api.save(stream.id, 60, lat), `Saving last 60s of ${stream.name}`);
          break;
        case "bookmark":
          await run(() => api.mark(stream.id, lat), `Marked moment on ${stream.name}`);
          break;
        case "expand":
          s.set({ expanded: s.expanded === stream.id ? null : stream.id });
          break;
        case "toggleAI": {
          const on = s.live[stream.id]?.ai_enabled ?? stream.ai_enabled;
          await run(() => api.setAI(stream.id, !on), `AI ${on ? "off" : "on"} for ${stream.name}`);
          break;
        }
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
