"""yt-dlp wrapper for past broadcasts (VODs).

Only the audio of a VOD is downloaded for analysis (a 9.5 h Twitch VOD is ~0.9 GB
audio vs ~30 GB at source quality); video is fetched afterwards for the detected
moments only (``--download-sections``). Every call is an argument list, never a shell
string, and URLs are validated by ``vod.source`` before they get here.
"""
from __future__ import annotations

import importlib.util
import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from ..media import popen_flags

log = logging.getLogger("liveclip.vod")

_PCT_RE = re.compile(r"\[download\]\s+([\d.]+)%")


class YtDlpError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def ytdlp_cmd() -> list[str] | None:
    """yt-dlp from this backend's own environment (setup installs it), else from PATH."""
    if importlib.util.find_spec("yt_dlp") is not None:
        return [sys.executable, "-m", "yt_dlp"]
    exe = shutil.which("yt-dlp")
    if exe:
        return [exe]
    for d in ("/opt/homebrew/bin", "/usr/local/bin"):           # macOS apps don't inherit the shell PATH
        if Path(d, "yt-dlp").exists():
            return [str(Path(d, "yt-dlp"))]
    return None


class YtDlp:
    def __init__(self, ffmpeg: str | None):
        self.ffmpeg = ffmpeg
        self.base = ytdlp_cmd()

    @property
    def available(self) -> bool:
        return self.base is not None

    def _cmd(self, *args: str) -> list[str]:
        if not self.base:
            raise YtDlpError("yt-dlp is not installed (run the setup script again)")
        cmd = [*self.base, "--no-config", "--no-warnings", "--no-playlist", "--no-colors"]
        if self.ffmpeg:
            cmd += ["--ffmpeg-location", self.ffmpeg]
        return cmd + list(args)

    def _run_json(self, args: list[str], timeout: float = 120) -> dict:
        p = subprocess.run(self._cmd(*args), capture_output=True, timeout=timeout, **popen_flags())
        if p.returncode != 0:
            raise YtDlpError(_tail(p.stderr))
        return json.loads(p.stdout.decode("utf-8", "replace") or "{}")

    def probe(self, url: str) -> dict:
        """Metadata of one VOD: id, title, duration, uploader, timestamp, live_status, extractor."""
        info = self._run_json(["-J", "--skip-download", url])
        return {k: info.get(k) for k in ("id", "title", "duration", "uploader", "uploader_id", "channel", "channel_id",
                                         "timestamp", "release_timestamp", "live_status", "was_live", "is_live",
                                         "extractor_key", "webpage_url")}

    def list_latest(self, listing_url: str, n: int = 3) -> list[dict]:
        """Newest entries of a channel's past-broadcast listing (url, title, duration)."""
        cmd = [c for c in self._cmd("--flat-playlist", "-I", f"1:{n}", "-J", listing_url) if c != "--no-playlist"]
        p = subprocess.run(cmd, capture_output=True, timeout=120, **popen_flags())
        if p.returncode != 0:
            raise YtDlpError(_tail(p.stderr))
        data = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
        out = []
        for e in data.get("entries") or []:
            u = e.get("url") or e.get("webpage_url")
            if u:
                out.append({"url": u, "title": e.get("title"), "duration": e.get("duration"),
                            "timestamp": e.get("timestamp"), "live_status": e.get("live_status")})
        return out

    # --- downloads ------------------------------------------------------------
    def download_audio(self, url: str, out: Path, *, rate_limit: str | None = None,
                       progress: Callable[[float], None] | None = None, cancel: threading.Event | None = None) -> Path:
        """Audio only: Twitch's Audio_Only rendition, else the best audio, else the smallest
        combined format (some Kick VODs have no separate audio)."""
        args = ["-f", "Audio_Only/bestaudio/worst", "--newline", "--no-part", "-N", "4" if rate_limit else "8", "-o", str(out.with_suffix(".%(ext)s")),
                "--print", "after_move:filepath"]
        if rate_limit:
            args += ["--limit-rate", rate_limit]
        lines = self._stream(self._cmd(*args, url), progress, cancel)
        path = next((Path(l) for l in reversed(lines) if l and Path(l).exists()), None)
        if path is None:
            cands = sorted(out.parent.glob(out.stem + ".*"), key=lambda p: p.stat().st_size, reverse=True)
            path = cands[0] if cands else None
        if path is None:
            raise YtDlpError("audio download produced no file")
        return path

    def download_section(self, url: str, t0: float, t1: float, out: Path, *, max_height: int = 1080,
                         rate_limit: str | None = None, cancel: threading.Event | None = None) -> Path:
        """Video for [t0, t1] only (the moment), at most `max_height`p, merged to mp4."""
        fmt = f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
        args = ["-f", fmt, "--download-sections", f"*{t0:.2f}-{t1:.2f}", "--merge-output-format", "mp4",
                "--newline", "--no-part", "-o", str(out), "--force-overwrites"]
        if rate_limit:
            args += ["--limit-rate", rate_limit]
        self._stream(self._cmd(*args, url), None, cancel)
        if not out.exists():
            raise YtDlpError(f"section download {t0:.0f}-{t1:.0f}s produced no file")
        return out

    def stream_url(self, url: str, max_height: int = 1080) -> str | None:
        """Direct playlist URL of the single muxed rendition (Twitch/Kick HLS); None if the site
        serves separate video + audio (YouTube DASH) — then download_section is used instead."""
        p = subprocess.run(self._cmd("-g", "-f", f"b[height<={max_height}]/b", url), capture_output=True, timeout=120,
                           **popen_flags())
        lines = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l.startswith("https://")]
        return lines[0] if p.returncode == 0 and len(lines) == 1 and ".m3u8" in lines[0] else None

    def download_section_hls(self, playlist_url: str, t0: float, t1: float, out: Path, *, rate_bps: float | None = None,
                             cancel: threading.Event | None = None, workers: int = 6) -> Path:
        """Fetch only the HLS segments covering [t0, t1] in parallel, join them and cut with ffmpeg
        (stream copy). ~10x faster than ffmpeg reading the playlist one segment at a time."""
        import concurrent.futures as cf
        from urllib.parse import urljoin

        import httpx

        with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as http:
            text = http.get(playlist_url).raise_for_status().text
            segs, init, t, dur = [], None, 0.0, None
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("#EXT-X-MAP:"):
                    m = re.search(r'URI="([^"]+)"', line)
                    init = urljoin(playlist_url, m.group(1)) if m else None
                elif line.startswith("#EXTINF:"):
                    dur = float(line[8:].split(",")[0])
                elif line and not line.startswith("#") and dur is not None:
                    segs.append((t, t + dur, urljoin(playlist_url, line)))
                    t += dur
                    dur = None
            pick = [s for s in segs if s[1] > t0 and s[0] < t1]
            if not pick:
                raise YtDlpError(f"no segments cover {t0:.0f}-{t1:.0f}s")
            parts: list[bytes | None] = [None] * len(pick)
            per_worker = (rate_bps / max(1, workers)) if rate_bps else None

            def fetch(i: int) -> None:
                if cancel is not None and cancel.is_set():
                    raise Cancelled("cancelled")
                st = time.time()
                data = http.get(pick[i][2]).raise_for_status().content
                parts[i] = data
                if per_worker:                                  # stay under the bandwidth share
                    wait = len(data) / per_worker - (time.time() - st)
                    if wait > 0:
                        time.sleep(wait)

            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                for f in [ex.submit(fetch, i) for i in range(len(pick))]:
                    f.result()
            init_data = http.get(init).raise_for_status().content if init else b""
        joined = out.with_suffix(".join" + (".mp4" if init else ".ts"))
        with open(joined, "wb") as f:
            f.write(init_data)
            for p in parts:
                f.write(p or b"")
        try:
            subprocess.run([self.ffmpeg or "ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, t0 - pick[0][0]):.3f}",
                            "-i", str(joined), "-t", f"{t1 - t0:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                            "-avoid_negative_ts", "make_zero", "-movflags", "+faststart", str(out)],
                           check=True, capture_output=True, timeout=600, **popen_flags("low"))
        except subprocess.CalledProcessError as e:
            raise YtDlpError(_tail(e.stderr)) from None
        finally:
            joined.unlink(missing_ok=True)
        return out

    def download_live_chat(self, url: str, out_stem: Path) -> Path | None:
        """YouTube live replays keep their chat (live_chat.json with video offsets)."""
        args = ["--skip-download", "--write-subs", "--sub-langs", "live_chat", "-o", str(out_stem) + ".%(ext)s"]
        p = subprocess.run(self._cmd(*args, url), capture_output=True, timeout=1800, **popen_flags())
        if p.returncode != 0:
            log.info("no live chat replay for %s: %s", url, _tail(p.stderr)[-200:])
        cands = list(out_stem.parent.glob(out_stem.name + "*.live_chat.json"))
        return cands[0] if cands else None

    def _stream(self, cmd: list[str], progress, cancel) -> list[str]:
        log.info("yt-dlp: %s", " ".join(cmd[-6:]))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **popen_flags("low"))
        lines: list[str] = []
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                lines.append(line)
                if len(lines) > 400:
                    del lines[:200]
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    raise Cancelled("cancelled")
                m = _PCT_RE.search(line)
                if m and progress:
                    progress(min(100.0, float(m.group(1))) / 100.0)
            rc = proc.wait()
        finally:
            if proc.poll() is None:
                proc.kill()
        if cancel is not None and cancel.is_set():
            raise Cancelled("cancelled")
        if rc != 0:
            raise YtDlpError("\n".join(l for l in lines[-8:] if l))
        return lines


def parse_youtube_live_chat(path: Path) -> list[tuple[float, str, str, int]]:
    """live_chat.json (one JSON object per line) -> [(video_offset_s, author, text, emote_count)]."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            rp = obj.get("replayChatItemAction") or {}
            try:
                off = float(rp.get("videoOffsetTimeMsec")) / 1000.0
            except (TypeError, ValueError):
                continue
            for act in rp.get("actions") or []:
                item = ((act.get("addChatItemAction") or {}).get("item") or {})
                r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
                if not r:
                    continue
                text, emotes = [], 0
                for run in (r.get("message") or {}).get("runs") or []:
                    if "text" in run:
                        text.append(run["text"])
                    elif "emoji" in run:
                        emotes += 1
                        sc = (run["emoji"].get("shortcuts") or [""])[0]
                        text.append(sc)
                author = (r.get("authorName") or {}).get("simpleText") or ""
                t = "".join(text).strip()
                if t:
                    out.append((off, author, t, emotes))
    out.sort(key=lambda x: x[0])
    return out


def _tail(b: bytes) -> str:
    s = b.decode("utf-8", "replace").strip()
    errs = [l for l in s.splitlines() if "ERROR" in l]
    return (errs[-1] if errs else s[-600:]) or "yt-dlp failed"
