"""Independent per-stream capture worker.

One worker == one stream session. It opens the source exactly once:

    streamlink --stdout <url> <quality>  ──►  ffmpeg -i pipe:0
                                               ├─ -c copy  -f segment  → segments/seg_%06d.ts  (recording + preview)
                                               └─ audio → 16 kHz mono PCM → stdout → AudioAnalyzer

The .ts segments are byte-for-byte the source stream (no transcode), and the
dashboard preview is an HLS playlist generated over those same files, so the
browser never downloads the stream from the internet a second time.

Workers are threads that supervise subprocesses; a crash in one worker cannot
affect another. Reconnects use exponential backoff with bounded attempts.
"""
from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..media import ffprobe, popen_flags, stream_summary

log = logging.getLogger("liveclip.capture")

PCM_CHUNK = 3200  # 100 ms of 16 kHz s16 mono


@dataclass
class SegmentInfo:
    seq: int
    run: int
    path: str
    t_start: float
    t_end: float
    size: int


class CaptureWorker(threading.Thread):
    def __init__(self, *, session_id: str, streamer_id: str, platform: str, url: str, local_path: str | None,
                 seg_dir: Path, ffmpeg: str, ffprobe_bin: str, streamlink: str | None, quality: list[str],
                 segment_seconds: float, reconnect_base: float, reconnect_max: float, max_attempts: int,
                 audio_feed: Callable[[bytes], None], on_run_start: Callable[[int, float], None],
                 on_segment: Callable[[SegmentInfo], None], on_state: Callable[[str, str], None],
                 on_probe: Callable[[dict], None], media_clock: Callable[[], float]):
        super().__init__(name=f"capture-{session_id}", daemon=True)
        self.session_id = session_id
        self.streamer_id = streamer_id
        self.platform = platform
        self.url = url
        self.local_path = local_path
        self.seg_dir = seg_dir
        self.ffmpeg = ffmpeg
        self.ffprobe = ffprobe_bin
        self.streamlink = streamlink
        self.quality = quality
        self.segment_seconds = segment_seconds
        self.reconnect_base = reconnect_base
        self.reconnect_max = reconnect_max
        self.max_attempts = max_attempts
        self.audio_feed = audio_feed
        self.on_run_start = on_run_start
        self.on_segment = on_segment
        self.on_state = on_state
        self.on_probe = on_probe
        self.media_clock = media_clock

        self._halt = threading.Event()
        self._procs: list[subprocess.Popen] = []
        self.run_index = 0
        self.next_seq = 0
        self.attempts = 0
        self.reconnects = 0
        self.last_segment_wall = 0.0
        self.selected_quality: str | None = None
        self.stderr_tail: deque[str] = deque(maxlen=40)
        self.state = "starting"
        self.pids: dict[str, int] = {}

    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._halt.set()
        self._kill()

    def _kill(self) -> None:
        for p in list(self._procs):
            try:
                if p.poll() is None:
                    p.terminate()
            except OSError:
                pass
        for p in list(self._procs):
            try:
                p.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    p.kill()
                except OSError:
                    pass

    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        try:
            self.on_state(state, detail)
        except Exception:
            log.exception("on_state failed")

    # ------------------------------------------------------------------
    def build_commands(self, csv_path: Path) -> tuple[list[str] | None, list[str]]:
        seg_pattern = str(self.seg_dir / "seg_%06d.ts")
        ff = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-y"]
        if self.local_path:
            ff += ["-nostdin", "-re", "-stream_loop", "-1", "-i", self.local_path]
        else:
            ff += ["-fflags", "+genpts+discardcorrupt", "-i", "pipe:0"]
        ff += [
            "-map", "0:v:0", "-map", "0:a:0", "-c", "copy",
            "-f", "segment", "-segment_time", f"{self.segment_seconds:g}", "-segment_format", "mpegts",
            "-segment_list", str(csv_path), "-segment_list_type", "csv",
            "-segment_start_number", str(self.next_seq), "-reset_timestamps", "0",
            seg_pattern,
            "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1",
        ]
        if self.local_path:
            return None, ff
        sl = [self.streamlink, "--stdout", "--loglevel", "info", "--stream-segment-threads", "2",
              "--retry-open", "2", "--stream-timeout", "30"]
        if self.platform == "twitch":
            sl += ["--twitch-disable-ads", "--twitch-low-latency"]
        sl += [self.url, ",".join(self.quality)]
        return sl, ff

    def run(self) -> None:
        self.seg_dir.mkdir(parents=True, exist_ok=True)
        if not self.ffmpeg:
            self._set_state("error", "FFmpeg not found. Run scripts/setup.ps1 or set LIVECLIP_FFMPEG.")
            return
        if not self.local_path and not self.streamlink:
            self._set_state("error", "Streamlink not found. Run scripts/setup.ps1 or set LIVECLIP_STREAMLINK.")
            return
        while not self._halt.is_set():
            outcome = self._run_once()
            if self._halt.is_set():
                break
            if outcome == "offline":
                self._set_state("offline", "Channel is offline; checking again in 45s")
                self.attempts = 0
                if self._halt.wait(45):
                    break
                continue
            self.attempts += 1
            if self.attempts > self.max_attempts:
                self._set_state("failed", f"Gave up after {self.max_attempts} reconnect attempts: {self._err_summary()}")
                return
            delay = min(self.reconnect_max, self.reconnect_base * (2 ** (self.attempts - 1)))
            self.reconnects += 1
            self._set_state("reconnecting", f"Attempt {self.attempts}/{self.max_attempts} in {delay:.0f}s: {self._err_summary()}")
            if self._halt.wait(delay):
                break
        self._set_state("stopped", "")

    def _err_summary(self) -> str:
        for line in reversed(self.stderr_tail):
            if "error" in line.lower():
                return line[-200:]
        return self.stderr_tail[-1][-200:] if self.stderr_tail else "stream ended"

    def _run_once(self) -> str:
        self.run_index += 1
        csv_path = self.seg_dir / f"segments_run{self.run_index:03d}.csv"
        sl_cmd, ff_cmd = self.build_commands(csv_path)
        media_offset = self.media_clock()
        self._set_state("connecting", "")
        self.stderr_tail.clear()
        self._procs = []
        try:
            if sl_cmd:
                sl = subprocess.Popen(sl_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_flags("high"))
                self._procs.append(sl)
                self.pids["streamlink"] = sl.pid
                threading.Thread(target=self._read_stderr, args=(sl, "streamlink"), daemon=True).start()
                ff = subprocess.Popen(ff_cmd, stdin=sl.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      **popen_flags("high"))
                sl.stdout.close()  # ffmpeg owns the read end now
            else:
                ff = subprocess.Popen(ff_cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      **popen_flags("high"))
            self._procs.append(ff)
            self.pids["ffmpeg"] = ff.pid
        except OSError as e:
            self.stderr_tail.append(f"error: failed to start process: {e}")
            self._kill()
            return "error"

        threading.Thread(target=self._read_stderr, args=(ff, "ffmpeg"), daemon=True).start()
        run_started = False
        run = self.run_index
        state = {"rows": 0, "first_start": None, "probed": False}
        self.last_segment_wall = time.time()
        stall_limit = max(30.0, self.segment_seconds * 4 + 10)

        def tail_csv() -> None:
            while ff.poll() is None and not self._halt.is_set():
                self._consume_csv(csv_path, run, media_offset, state)
                if time.time() - self.last_segment_wall > stall_limit and state["rows"] > 0:
                    self.stderr_tail.append(f"error: no new segment for {stall_limit:.0f}s (stalled); restarting")
                    self._kill()
                    break
                if state["rows"] == 0 and time.time() - self.last_segment_wall > 90:
                    self.stderr_tail.append("error: no media received within 90s")
                    self._kill()
                    break
                time.sleep(0.5)
            self._consume_csv(csv_path, run, media_offset, state)

        t = threading.Thread(target=tail_csv, daemon=True)
        t.start()
        try:
            while not self._halt.is_set():
                chunk = ff.stdout.read(PCM_CHUNK)
                if not chunk:
                    break
                if not run_started:
                    run_started = True
                    self.on_run_start(run, media_offset)
                    self._set_state("live", "")
                self.audio_feed(chunk)
        except (OSError, ValueError):
            pass
        ff.wait()
        self._kill()
        t.join(timeout=5)
        if state["rows"] > 0:
            self.attempts = 0  # we were live; reset retry budget
        text = "\n".join(self.stderr_tail).lower()
        if "no playable streams found" in text or "is offline" in text or "this channel does not exist" in text:
            return "offline"
        return "ended" if state["rows"] > 0 else "error"

    def _consume_csv(self, csv_path: Path, run: int, media_offset: float, state: dict) -> None:
        try:
            text = csv_path.read_text(encoding="utf-8", errors="replace")
        except (FileNotFoundError, PermissionError):
            return
        # only newline-terminated lines are complete; the tail may be mid-write
        lines = text.split("\n")[:-1]
        for line in lines[state["rows"]:]:
            parts = line.strip().split(",")
            if len(parts) < 3:
                break  # partially written line; retry next poll
            name, start, end = parts[0], float(parts[1]), float(parts[2])
            if state["first_start"] is None:
                state["first_start"] = start
            m = re.search(r"(\d+)", name)
            seq = int(m.group(1)) if m else self.next_seq
            path = self.seg_dir / name
            size = path.stat().st_size if path.exists() else 0
            state["rows"] += 1
            self.next_seq = max(self.next_seq, seq + 1)
            self.last_segment_wall = time.time()
            info = SegmentInfo(seq, run, str(path), media_offset + start - state["first_start"],
                               media_offset + end - state["first_start"], size)
            try:
                self.on_segment(info)
            except Exception:
                log.exception("on_segment failed")
            if not state["probed"]:
                state["probed"] = True
                try:
                    summ = stream_summary(ffprobe(self.ffprobe, path))
                    summ["quality_selected"] = self.selected_quality
                    self.on_probe(summ)
                except Exception as e:
                    log.warning("probe failed: %s", e)

    def _read_stderr(self, proc: subprocess.Popen, tag: str) -> None:
        try:
            for raw in iter(proc.stderr.readline, b""):
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self.stderr_tail.append(f"[{tag}] {line}")
                if tag == "streamlink":
                    m = re.search(r"Opening stream: (\S+)", line)
                    if m:
                        self.selected_quality = m.group(1)
                    m = re.search(r"Available streams: (.*)", line)
                    if m:
                        self.stderr_tail.append(f"[streamlink] qualities: {m.group(1)}")
        except (OSError, ValueError):
            pass
