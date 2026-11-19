"""FFmpeg/FFprobe helpers. All invocations use argument lists (never a shell
string), so URLs and paths cannot inject commands."""
from __future__ import annotations

import json
import logging
import os
import subprocess
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("liveclip.media")

if os.name == "nt":
    CREATE_NO_WINDOW = 0x08000000
    BELOW_NORMAL = 0x00004000
    ABOVE_NORMAL = 0x00008000
    NEW_GROUP = 0x00000200
else:  # pragma: no cover
    CREATE_NO_WINDOW = BELOW_NORMAL = ABOVE_NORMAL = NEW_GROUP = 0


def popen_flags(priority: str = "normal") -> dict:
    """Process creation flags. Capture runs above normal; renders below normal
    so an export can never starve live recording."""
    if os.name != "nt":
        # macOS/Linux: own process group (like Windows NEW_GROUP: a Ctrl+C in the terminal
        # doesn't kill FFmpeg mid-segment; the app stops it itself) and renders at lower
        # priority via nice. Raising priority needs root, so capture stays at normal.
        kw: dict = {"start_new_session": True}
        if priority == "low":
            kw["preexec_fn"] = lambda: os.nice(10)
        return kw
    flags = CREATE_NO_WINDOW | NEW_GROUP
    if priority == "high":
        flags |= ABOVE_NORMAL
    elif priority == "low":
        flags |= BELOW_NORMAL
    return {"creationflags": flags}


class MediaError(RuntimeError):
    pass


def run(cmd: list[str], timeout: float = 600, priority: str = "normal") -> subprocess.CompletedProcess:
    log.debug("run: %s", cmd)
    p = subprocess.run(cmd, capture_output=True, timeout=timeout, **popen_flags(priority))
    if p.returncode != 0:
        tail = p.stderr.decode("utf-8", "replace")[-1500:]
        raise MediaError(f"{Path(cmd[0]).name} exited {p.returncode}: {tail}")
    return p


def ffprobe(ffprobe_bin: str, path: str | Path) -> dict:
    p = run([ffprobe_bin, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout=60)
    return json.loads(p.stdout.decode("utf-8", "replace") or "{}")


def stream_summary(info: dict) -> dict:
    out: dict = {"duration": None}
    try:
        out["duration"] = float(info.get("format", {}).get("duration") or 0) or None
    except ValueError:
        pass
    for s in info.get("streams", []):
        try:
            st = float(s.get("start_time") or 0.0)
        except ValueError:
            st = 0.0
        if s.get("codec_type") == "video" and "video_start" not in out:
            out["video_start"] = st
        elif s.get("codec_type") == "audio" and "audio_start" not in out:
            out["audio_start"] = st
        if s.get("codec_type") == "video" and "width" not in out:
            out["width"], out["height"] = s.get("width"), s.get("height")
            out["video_codec"] = s.get("codec_name")
            fr = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
            try:
                n, d = fr.split("/")
                out["fps"] = round(float(n) / float(d), 3) if float(d) else None
            except (ValueError, ZeroDivisionError):
                out["fps"] = None
        elif s.get("codec_type") == "audio" and "audio_codec" not in out:
            out["audio_codec"] = s.get("codec_name")
            out["audio_rate"] = int(s.get("sample_rate") or 0) or None
            out["audio_channels"] = s.get("channels")
    return out


def verify_playable(ffmpeg_bin: str, ffprobe_bin: str, path: str | Path) -> dict:
    """A file is accepted only if ffprobe sees a video stream with a positive
    duration and ffmpeg can decode its first second without errors."""
    path = Path(path)
    if not path.exists() or path.stat().st_size < 1024:
        raise MediaError(f"{path.name}: missing or too small")
    summ = stream_summary(ffprobe(ffprobe_bin, path))
    if not summ.get("width") or not summ.get("duration"):
        raise MediaError(f"{path.name}: no video stream or zero duration")
    p = subprocess.run([ffmpeg_bin, "-v", "error", "-xerror", "-i", str(path), "-t", "1", "-f", "null", "-"],
                       capture_output=True, timeout=60, **popen_flags())
    if p.returncode != 0:
        raise MediaError(f"{path.name}: decode check failed: {p.stderr.decode('utf-8', 'replace')[-400:]}")
    return summ


@lru_cache(maxsize=4)
def videotoolbox_available(ffmpeg_bin: str) -> bool:
    """Apple's hardware H.264 encoder (macOS)."""
    try:
        p = subprocess.run([ffmpeg_bin, "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                            "-c:v", "h264_videotoolbox", "-f", "null", "-"], capture_output=True, timeout=30, **popen_flags())
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@lru_cache(maxsize=4)
def nvenc_available(ffmpeg_bin: str) -> bool:
    try:
        p = subprocess.run([ffmpeg_bin, "-hide_banner", "-f", "lavfi", "-i", "color=c=black:s=256x256:d=0.1",
                            "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=30, **popen_flags())
        return p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def thumbnail(ffmpeg_bin: str, src: str | Path, dst: str | Path, at: float) -> None:
    run([ffmpeg_bin, "-y", "-v", "error", "-ss", f"{max(0.0, at):.3f}", "-i", str(src), "-frames:v", "1",
         "-vf", "scale=480:-2", "-q:v", "3", str(dst)], timeout=60)
