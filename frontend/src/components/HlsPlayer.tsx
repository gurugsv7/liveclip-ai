import Hls from "hls.js";
import { useEffect, useRef, useState } from "react";

/**
 * Plays the backend's local HLS playlist, which points at the very same .ts
 * segments the recorder writes — the browser never contacts Twitch/YouTube.
 * `muted` is the only thing that controls what you hear; recording audio is
 * untouched by anything in the UI.
 */
export function HlsPlayer({ src, muted, paused, onLatency, className }: {
  src: string | null; muted: boolean; paused?: boolean; onLatency?: (s: number) => void; className?: string;
}) {
  const ref = useRef<HTMLVideoElement>(null);
  const hlsRef = useRef<Hls | null>(null);
  const [status, setStatus] = useState<"idle" | "buffering" | "playing" | "error">("idle");

  useEffect(() => {
    const video = ref.current;
    if (!video || !src) {
      setStatus("idle");
      return;
    }
    let disposed = false;
    let retry: ReturnType<typeof setTimeout> | null = null;
    const attach = () => {
      if (disposed) return;
      setStatus("buffering");
      if (Hls.isSupported()) {
        const hls = new Hls({
          liveSyncDurationCount: 2,
          liveMaxLatencyDurationCount: 5,
          maxBufferLength: 12,
          backBufferLength: 8,
          manifestLoadingMaxRetry: 2,
          enableWorker: true,
        });
        hlsRef.current = hls;
        hls.loadSource(src);
        hls.attachMedia(video);
        hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => undefined));
        hls.on(Hls.Events.ERROR, (_e, data) => {
          if (data.fatal) {
            hls.destroy();
            hlsRef.current = null;
            setStatus(data.type === Hls.ErrorTypes.NETWORK_ERROR ? "buffering" : "error");
            retry = setTimeout(attach, 2500);
          }
        });
      } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
        video.src = src;
      }
    };
    attach();
    const onPlaying = () => setStatus("playing");
    const onWaiting = () => setStatus("buffering");
    video.addEventListener("playing", onPlaying);
    video.addEventListener("waiting", onWaiting);
    const iv = setInterval(() => {
      const h = hlsRef.current;
      if (h && onLatency && Number.isFinite(h.latency)) onLatency(h.latency);
    }, 1000);
    return () => {
      disposed = true;
      if (retry) clearTimeout(retry);
      clearInterval(iv);
      video.removeEventListener("playing", onPlaying);
      video.removeEventListener("waiting", onWaiting);
      hlsRef.current?.destroy();
      hlsRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [src]);

  useEffect(() => {
    if (ref.current) ref.current.muted = muted;
  }, [muted]);

  useEffect(() => {
    const v = ref.current;
    if (!v) return;
    if (paused) v.pause();
    else {
      // jump back to the live edge after an unpause
      const h = hlsRef.current;
      if (h && h.liveSyncPosition) v.currentTime = h.liveSyncPosition;
      v.play().catch(() => undefined);
    }
  }, [paused]);

  return (
    <div className={className}>
      <video ref={ref} muted={muted} playsInline className="h-full w-full bg-black object-contain" data-testid="hls-video" />
      {src && status === "buffering" && (
        <div className="pointer-events-none absolute inset-0 grid place-items-center">
          <div className="bg-black/75 px-2 py-1 text-[11px] text-stone-100">Buffering local preview…</div>
        </div>
      )}
    </div>
  );
}
