import * as TooltipP from "@radix-ui/react-tooltip";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { StreamPanel } from "@/components/StreamPanel";
import { useShortcuts } from "@/lib/shortcuts";
import { useStore } from "@/lib/store";
import type { LiveStatus, Streamer } from "@/lib/types";

const mkStreamer = (id: string, slot: number, name: string): Streamer => ({
  id, slug: name.toLowerCase(), name, platform: "twitch", url: `https://www.twitch.tv/${name.toLowerCase()}`, slot, priority: 2,
  color: "#8b5cf6", ai_enabled: true, facecam: null, default_layout: "smart_crop", usage_notes: "", live: null, meters: null,
});
const mkLive = (sid: string, ses: string): LiveStatus => ({
  streamer_id: sid, session_id: ses, state: "live", detail: "", platform: "twitch", url: "", width: 1920, height: 1080, fps: 60,
  video_codec: "h264", audio_codec: "aac", audio_rate: 48000, measured_kbps: 7800, quality_requested: ["1080p60"],
  quality_selected: "1080p60", reconnects: 0, recording_s: 42, media_now: 200,
  buffer: { segments: 24, bytes: 1e8, t_first: 80, t_last: 200, seconds: 120, protected: [], protected_segments: 0 },
  ai_enabled: true, chat: { platform: "twitch", status: "connected", detail: "", messages: 10 }, chat_messages: 10,
});

let calls: { method: string; url: string; body: any }[] = [];

beforeEach(() => {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return new Response(JSON.stringify({ clip_id: "clip_x", deleted: 3, protected_kept: 1, ai_enabled: false }), { status: 200 });
  }));
  const a = mkStreamer("str_a", 0, "Speed");
  const b = mkStreamer("str_b", 1, "Marlon");
  useStore.setState({
    page: "live", streams: [a, b], live: { str_a: mkLive("str_a", "ses_aaaaaaaaaaaa"), str_b: mkLive("str_b", "ses_bbbbbbbbbbbb") },
    audible: [], selected: 0,
    config: { audio_monitor: { allow_multi: false }, shortcuts: { select1: "1", select2: "2", mute: "m", save60: "c", bookmark: "b", expand: "e", toggleAI: "a" }, detection: { trigger_threshold: 0.62 } },
    latency: { str_a: 6.5 }, toasts: [], clips: [], events: [],
  });
});
afterEach(() => vi.unstubAllGlobals());

const wrap = (ui: React.ReactNode) => render(<TooltipP.Provider>{ui}</TooltipP.Provider>);
const posts = () => calls.filter((c) => c.method === "POST");

describe("stream panel controls call real backend operations", () => {
  it("Save 60s posts to the streamer's own save endpoint with preview latency", async () => {
    wrap(<StreamPanel streamer={useStore.getState().streams[0]} selected onSelect={() => {}} onReplace={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /save 60s/i }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    expect(posts()[0].url).toBe("/api/streams/str_a/save");
    expect(posts()[0].body).toEqual({ seconds: 60, latency_s: 6.5 });
  });

  it("Save menu offers 15/30/60/120 and posts the chosen length", async () => {
    wrap(<StreamPanel streamer={useStore.getState().streams[1]} selected onSelect={() => {}} onReplace={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /choose save length/i }));
    for (const s of [15, 30, 60, 120]) expect(screen.getByText(`Save last ${s}s`)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Save last 120s"));
    await waitFor(() => expect(posts()[0]?.url).toBe("/api/streams/str_b/save"));
    expect(posts()[0].body.seconds).toBe(120);
  });

  it("Mark posts to the mark endpoint", async () => {
    wrap(<StreamPanel streamer={useStore.getState().streams[0]} selected onSelect={() => {}} onReplace={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /mark moment/i }));
    await waitFor(() => expect(posts()[0]?.url).toBe("/api/streams/str_a/mark"));
  });

  it("Delete buffer requires confirmation and sends confirm:true", async () => {
    wrap(<StreamPanel streamer={useStore.getState().streams[0]} selected onSelect={() => {}} onReplace={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /^more$/i }));
    fireEvent.click(screen.getByText(/delete buffer/i));
    expect(posts()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: /delete unprotected footage/i }));
    await waitFor(() => expect(posts()[0]?.url).toBe("/api/streams/str_a/delete-buffer"));
    expect(posts()[0].body).toEqual({ confirm: true });
  });

  it("AI toggle posts enabled:false without stopping the stream", async () => {
    wrap(<StreamPanel streamer={useStore.getState().streams[0]} selected onSelect={() => {}} onReplace={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /disable ai detection/i }));
    await waitFor(() => expect(posts()[0]?.url).toBe("/api/streams/str_a/ai"));
    expect(posts()[0].body).toEqual({ enabled: false });
    expect(calls.some((c) => c.url.includes("/stop"))).toBe(false);
  });
});

describe("dashboard audio monitoring", () => {
  it("single-stream mode: selecting a speaker mutes the other; clicking active mutes all", () => {
    useStore.getState().toggleAudio("str_a", false);
    expect(useStore.getState().audible).toEqual(["str_a"]);
    useStore.getState().toggleAudio("str_b", false);
    expect(useStore.getState().audible).toEqual(["str_b"]);
    useStore.getState().toggleAudio("str_b", false);
    expect(useStore.getState().audible).toEqual([]);
  });

  it("multi mode allows more than one audible stream", () => {
    useStore.getState().toggleAudio("str_a", true);
    useStore.getState().toggleAudio("str_b", true);
    expect([...useStore.getState().audible].sort()).toEqual(["str_a", "str_b"]);
  });

  it("video elements are muted unless their stream is the audible one; no backend calls", () => {
    const [a, b] = useStore.getState().streams;
    wrap(
      <>
        <StreamPanel streamer={a} selected onSelect={() => {}} onReplace={() => {}} />
        <StreamPanel streamer={b} selected={false} onSelect={() => {}} onReplace={() => {}} />
      </>,
    );
    const vids = screen.getAllByTestId("hls-video") as HTMLVideoElement[];
    expect(vids.map((v) => v.muted)).toEqual([true, true]);
    act(() => useStore.getState().toggleAudio("str_b", false));
    expect(vids.map((v) => v.muted)).toEqual([true, false]);
    act(() => useStore.getState().toggleAudio("str_a", false));
    expect(vids.map((v) => v.muted)).toEqual([false, true]);
    expect(calls.filter((c) => c.method !== "GET")).toHaveLength(0);
  });
});

function ShortcutHost() {
  useShortcuts();
  return <input aria-label="typing field" />;
}

describe("keyboard shortcuts", () => {
  it("C saves 60s for the selected stream, but not while typing", async () => {
    render(<ShortcutHost />);
    fireEvent.keyDown(screen.getByLabelText("typing field"), { key: "c" });
    await new Promise((r) => setTimeout(r, 20));
    expect(calls).toHaveLength(0);
    fireEvent.keyDown(window, { key: "2" });
    expect(useStore.getState().selected).toBe(1);
    fireEvent.keyDown(window, { key: "c" });
    await waitFor(() => expect(posts()[0]?.url).toBe("/api/streams/str_b/save"));
  });

  it("M toggles audio of the selected stream", async () => {
    render(<ShortcutHost />);
    fireEvent.keyDown(window, { key: "m" });
    await waitFor(() => expect(useStore.getState().audible).toEqual(["str_a"]));
    fireEvent.keyDown(window, { key: "m" });
    await waitFor(() => expect(useStore.getState().audible).toEqual([]));
  });
});
