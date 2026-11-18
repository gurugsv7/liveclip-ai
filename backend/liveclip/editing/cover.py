"""Reel cover image + ready-to-paste post caption, made on export.

Cover: the best real frame of the finished 1080x1920 reel (sharp, well exposed, a face if
there is one, close to the hype peak, no half-shown caption word), with the reel's own hook
line drawn in the same caption style. Nothing generated: it is a frame of the video.

Post caption (house post-kit rules adapted to streamer clips): hook line with at most one
emoji, one more real line the streamer said, a credit line (it is their footage), then
3-5 lowercase specific hashtags. Only words that were actually said; nothing invented.
"""
from __future__ import annotations

import logging
import math
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np

from ..media import popen_flags

log = logging.getLogger(__name__)

SAMPLE_FPS = 3.0
SW, SH = 270, 480             # analysis size (1/4 of 1080x1920)
CAPTION_HOLD = 0.45           # a caption page stays up a little after its last word


def _frames(ffmpeg: str, video: str) -> np.ndarray:
    cmd = [ffmpeg, "-v", "error", "-i", video, "-vf", f"fps={SAMPLE_FPS},scale={SW}:{SH}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    p = subprocess.run(cmd, capture_output=True, timeout=180, **popen_flags("low"))
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-400:])
    n = len(p.stdout) // (SW * SH * 3)
    return np.frombuffer(p.stdout[: n * SW * SH * 3], dtype=np.uint8).reshape(n, SH, SW, 3)


def _rank(x: np.ndarray) -> np.ndarray:
    """0..1 by rank, so one blurry/sharp outlier doesn't squash the rest."""
    if len(x) < 2:
        return np.ones_like(x, dtype=float)
    order = x.argsort().argsort()
    return order / (len(x) - 1)


def score_frames(frames: np.ndarray, times: list[float], duration: float, *, peak: float | None = None,
                 words: list[dict] | None = None, cuts: list[float] | None = None, hook_until: float = 0.0,
                 face_weight: float = 0.25) -> np.ndarray:
    """Higher = better cover. Pure function over decoded frames (unit-testable)."""
    import cv2  # type: ignore

    n = len(frames)
    sharp, face, expo, contrast = (np.zeros(n) for _ in range(4))
    casc = None
    if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
        casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    for i, f in enumerate(frames):
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        sharp[i] = cv2.Laplacian(g, cv2.CV_64F).var()
        m, s = g.mean() / 255.0, g.std() / 255.0
        expo[i] = max(0.0, 1.0 - abs(m - 0.45) / 0.35)
        contrast[i] = min(1.0, s / 0.22)
        if casc is not None:
            found = casc.detectMultiScale(g, scaleFactor=1.15, minNeighbors=5, minSize=(24, 24))
            if len(found):
                area = max(w * h for (_, _, w, h) in found) / (SW * SH)
                face[i] = min(1.0, area / 0.03)
    total = 0.35 * _rank(sharp) + face_weight * face + 0.15 * expo + 0.10 * contrast
    for i, t in enumerate(times):
        if peak is not None:
            total[i] += 0.15 * math.exp(-((t - peak) / 5.0) ** 2)
        if t < 0.3 or t > duration - 0.3 or t < hook_until:
            total[i] -= 1.0                                   # fades/edges, and the hook already has text
        if cuts and any(abs(t - c) < 0.25 for c in cuts):
            total[i] -= 0.5                                   # a jump-cut frame can be a blend
        if words and any(w["start"] - 0.05 <= t <= w["end"] + CAPTION_HOLD for w in words):
            total[i] *= 0.6                                   # a half caption on a cover looks broken
        if sharp[i] < 15:
            total[i] -= 0.5                                   # black/flat frame
    return total


def pick_cover_time(ffmpeg: str, video: str, duration: float, **kw) -> float:
    frames = _frames(ffmpeg, video)
    if not len(frames):
        return min(duration / 2, 1.0)
    times = [(i + 0.5) / SAMPLE_FPS for i in range(len(frames))]
    s = score_frames(frames, times, duration, **kw)
    return float(times[int(np.argmax(s))])


def pick_face_time(ffmpeg: str, src: str, pieces: list[tuple[float, float]], facecam: dict,
                   src_w: int, src_h: int, peak: float | None = None) -> float | None:
    """Source time where the STREAMER's face (inside their facecam rect) looks best: face found,
    sharp, well lit, close to the peak. Faces elsewhere (thumbnails, people in a watched video)
    are ignored. None if the face is never found."""
    import cv2  # type: ignore

    if not hasattr(cv2, "CascadeClassifier"):
        return None
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    fx, fy = int(facecam["x"] * src_w) // 2 * 2, int(facecam["y"] * src_h) // 2 * 2
    fw, fh = max(32, int(facecam["w"] * src_w) // 2 * 2), max(32, int(facecam["h"] * src_h) // 2 * 2)
    W = 240
    H = int(W * fh / fw) // 2 * 2
    cands = []
    for a, b in pieces:
        cmd = [ffmpeg, "-v", "error", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", src,
               "-vf", f"fps={SAMPLE_FPS},crop={fw}:{fh}:{fx}:{fy},scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
        p = subprocess.run(cmd, capture_output=True, timeout=120, **popen_flags("low"))
        n = len(p.stdout) // (W * H * 3)
        fr = np.frombuffer(p.stdout[: n * W * H * 3], dtype=np.uint8).reshape(n, H, W, 3)
        for i, f in enumerate(fr):
            t = a + (i + 0.5) / SAMPLE_FPS
            if t < a + 0.3 or t > b - 0.3:
                continue
            g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
            found = casc.detectMultiScale(g, scaleFactor=1.1, minNeighbors=5, minSize=(W // 8, W // 8))
            if not len(found):
                continue
            area = max(w * h for (_, _, w, h) in found) / (W * H)
            cands.append((t, cv2.Laplacian(g, cv2.CV_64F).var(), max(0.0, 1 - abs(g.mean() / 255 - 0.45) / 0.35), area))
    if not cands:
        return None
    sharp = _rank(np.array([c[1] for c in cands]))
    best, best_s = None, -1e9
    for (t, _, expo, area), sh in zip(cands, sharp):
        sc = 0.45 * sh + 0.2 * expo + 0.15 * min(1.0, area / 0.12)
        if peak is not None:
            sc += 0.3 * math.exp(-((t - peak) / 4.0) ** 2)
        if sc > best_s:
            best, best_s = t, sc
    return best


def make_cover(ffmpeg: str, video: str, out: Path, at: float, *, hook: str | None, work: Path,
               style: str = "bold_pop", font: str = "", fonts_dir: str | None = None,
               uppercase: bool | None = None, hook_y: int | None = None) -> Path:
    """Grab the frame at `at` (full 1080x1920) and draw the hook line on it in the reel's style
    (at `hook_y` px, centred, when given; e.g. on the face/content seam of a facecam cover)."""
    from .captions import build_ass

    work.mkdir(parents=True, exist_ok=True)
    vf = "null"
    if hook:
        ass = build_ass([], style=style, font=font, width=1080, height=1920, hook=hook, hook_seconds=3600,
                        duration=3600, uppercase=uppercase)
        if hook_y is not None:
            ass = ass.replace(r"{\fad(0,180)}", r"{\an5\pos(540," + str(int(hook_y)) + r")}")
        (work / "cover.ass").write_text(ass, encoding="utf-8")
        if fonts_dir and Path(fonts_dir).is_dir():
            shutil.copytree(fonts_dir, work / "fonts", dirs_exist_ok=True)
            vf = "subtitles=cover.ass:fontsdir=fonts"
        else:
            vf = "subtitles=cover.ass"
    tmp = work / "cover.jpg"
    # -ss before -i resets timestamps to 0, so the hook (shown 0..3600 s) is on this frame
    p = subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{at:.3f}", "-i", video, "-frames:v", "1",
                        "-vf", vf, "-q:v", "2", "cover.jpg"], cwd=work, capture_output=True, timeout=60,
                       **popen_flags("low"))
    if p.returncode != 0 or not tmp.exists():
        raise RuntimeError(p.stderr.decode("utf-8", "replace")[-400:])
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(tmp), out)
    return out


# ------------------------------------------------------------------ post caption
PLATFORM_NAME = {"twitch": "Twitch", "youtube": "YouTube", "kick": "Kick", "local": "stream"}
_LAUGH = re.compile(r"\b(ha(ha)+|lmao|lol|i can'?t)\b", re.I)
_SHOCK = re.compile(r"\b(no way|what the|oh my god|omg|bro what|are you serious|why did i|wtf)\b", re.I)


def _sentences(words: list[dict]) -> list[str]:
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w["text"].strip())
        gap = (words[i + 1]["start"] - w["end"]) if i + 1 < len(words) else 9
        if re.search(r"[.!?]$", w["text"].strip()) or gap > 0.6:
            s = re.sub(r"\s+", " ", " ".join(cur)).strip()
            if s:
                out.append(s)
            cur = []
    return out


def _tag(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def post_caption(*, streamer: str, platform: str, url: str | None, hook: str | None, words: list[dict]) -> str:
    text = " ".join(w["text"] for w in words)
    emoji = "😂" if _LAUGH.search(text) else "😳" if _SHOCK.search(text) else "🔥"
    hook_line = (hook or "").strip().rstrip(".") or f"{streamer} moment"
    lines = [f"{hook_line} {emoji}"]
    # one more real line, the strongest-sounding complete one that isn't the hook
    hk = _tag(hook_line)
    extra = [s for s in _sentences(words) if 4 <= len(s.split()) <= 14 and _tag(s) not in hk and hk not in _tag(s)]
    if extra:
        best = max(extra, key=lambda s: (bool(_SHOCK.search(s) or _LAUGH.search(s)), s.count("!"), -abs(len(s.split()) - 8)))
        lines += ["", f"“{best.rstrip('.')}”"]
    pname = PLATFORM_NAME.get(platform, platform.title())
    credit = f"🎥 {streamer} live on {pname}"
    if url and platform != "local":
        credit += f": {url}"
    lines += ["", credit]
    tags = []
    for t in (_tag(streamer), f"{_tag(streamer)}clips", f"{_tag(pname)}clips" if platform != "local" else "", "streamerclips", "livestream"):
        if t and t not in tags:
            tags.append(t)
    lines += ["", " ".join(f"#{t}" for t in tags[:5])]
    return "\n".join(lines) + "\n"
