"""Framing analysis for vertical layouts.

Smart crop: sample low-res grayscale frames at 2 fps, measure where motion and
detail concentrate per column, and derive a *stable* crop path: windowed
centroids, heavy smoothing, a speed limit and a dead zone, so the crop only
glides when the action really moves. Optional OpenCV face detection proposes
a facecam rectangle (user can always override it manually).
"""
from __future__ import annotations

import subprocess

import numpy as np

from ..media import popen_flags

AW, AH = 192, 108


def sample_frames(ffmpeg: str, src: str, t0: float, t1: float, fps: float = 2.0) -> np.ndarray:
    cmd = [ffmpeg, "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{max(0.5, t1 - t0):.3f}", "-i", src,
           "-vf", f"fps={fps},scale={AW}:{AH}:flags=area,format=gray", "-f", "rawvideo", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True, timeout=120, **popen_flags("low"))
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    n = len(buf) // (AW * AH)
    return buf[: n * AW * AH].reshape(n, AH, AW).astype(np.float32) if n else np.zeros((0, AH, AW), np.float32)


def crop_path(frames: np.ndarray, src_w: int, src_h: int, crop_w: int, fps: float = 2.0,
              window_s: float = 2.0, max_speed_frac: float = 0.06, dead_zone_frac: float = 0.05) -> list[tuple[float, float]]:
    """Return keyframes [(t_seconds, x_left_px)] for a crop of width crop_w."""
    half = crop_w / 2
    centre = src_w / 2
    if len(frames) < 2 or crop_w >= src_w:
        x = max(0.0, min(src_w - crop_w, centre - half))
        return [(0.0, x)]
    diffs = np.abs(np.diff(frames, axis=0))                          # motion
    gx = np.abs(np.diff(frames, axis=2))                              # detail
    gx = np.pad(gx, ((0, 0), (0, 0), (0, 1)))[1:]
    energy = diffs.mean(axis=1) * 1.0 + gx.mean(axis=1) * 0.35        # (n-1, AW)
    # ignore the outer 4% (overlays/borders) and bias gently to the centre
    edge = int(AW * 0.04)
    energy[:, :edge] = 0
    energy[:, AW - edge:] = 0
    bias = 1 - 0.35 * np.abs(np.linspace(-1, 1, AW))
    energy = energy * bias
    per = max(1, int(window_s * fps))
    raw = []
    for i in range(0, len(energy), per):
        e = energy[i:i + per].sum(axis=0)
        if e.sum() <= 1e-6:
            c = AW / 2
        else:
            # centroid of the strongest crop-width window (not the global mean,
            # which would land between two separate subjects)
            win = max(1, int(AW * crop_w / src_w))
            cs = np.convolve(e, np.ones(win), mode="valid")
            j = int(np.argmax(cs))
            seg = e[j:j + win]
            c = j + (np.arange(len(seg)) * seg).sum() / max(seg.sum(), 1e-6)
        raw.append(((i + per / 2) / fps, c / AW * src_w))
    # smooth: median-ish clamp, dead zone, speed limit
    path = []
    cur = raw[0][1]
    max_step = max_speed_frac * src_w * window_s
    for t, c in raw:
        if abs(c - cur) > dead_zone_frac * src_w:
            cur += float(np.clip(c - cur, -max_step, max_step))
        x = float(np.clip(cur - half, 0, src_w - crop_w))
        path.append((round(t, 2), round(x, 1)))
    # drop redundant keyframes
    out = [path[0]]
    for k in path[1:]:
        if abs(k[1] - out[-1][1]) > 2:
            out.append(k)
    if out[0][0] > 0:
        out.insert(0, (0.0, out[0][1]))
    return out[:24]


def x_expression(keys: list[tuple[float, float]]) -> str:
    """Piecewise-linear FFmpeg expression for crop x over output time t."""
    if len(keys) == 1:
        return f"{keys[0][1]:.1f}"
    expr = f"{keys[-1][1]:.1f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(keys[:-1], keys[1:]))):
        dt = max(0.01, t1 - t0)
        # smoothstep easing between keyframes
        seg = f"({x0:.1f}+({x1 - x0:.1f})*(3*pow((t-{t0:.2f})/{dt:.2f},2)-2*pow((t-{t0:.2f})/{dt:.2f},3)))"
        expr = f"if(lt(t,{t1:.2f}),{seg},{expr})"
    return f"if(lt(t,{keys[0][0]:.2f}),{keys[0][1]:.1f},{expr})"


def detect_facecam(ffmpeg: str, src: str, duration: float) -> dict | None:
    """Propose a facecam rectangle using OpenCV's Haar face detector, if the
    optional `opencv-python-headless` dependency is installed. Only returns a
    rect when a face is found at a consistent location (typical facecam)."""
    try:
        import cv2  # type: ignore
    except Exception:
        return None
    if not hasattr(cv2, "CascadeClassifier"):   # OpenCV 5 moved the Haar detector out of the main package
        return None
    cmd = [ffmpeg, "-v", "error", "-i", src, "-vf", "fps=0.5,scale=640:360,format=gray", "-frames:v", "12",
           "-f", "rawvideo", "pipe:1"]
    p = subprocess.run(cmd, capture_output=True, timeout=120, **popen_flags("low"))
    buf = np.frombuffer(p.stdout, dtype=np.uint8)
    n = len(buf) // (640 * 360)
    if n == 0:
        return None
    frames = buf[: n * 640 * 360].reshape(n, 360, 640)
    casc = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    hits = []
    for f in frames:
        faces = casc.detectMultiScale(f, scaleFactor=1.15, minNeighbors=6, minSize=(24, 24))
        if len(faces):
            x, y, w, h = max(faces, key=lambda r: r[2] * r[3])
            hits.append((x + w / 2, y + h / 2, w, h))
    if len(hits) < max(2, n // 3):
        return None
    arr = np.array(hits)
    cx, cy, w, h = np.median(arr, axis=0)
    if np.std(arr[:, 0]) > 40 or np.std(arr[:, 1]) > 40:
        return None  # moving face = probably gameplay/IRL, not a fixed facecam
    bw, bh = w * 3.2, h * 2.4
    x0, y0 = max(0, cx - bw / 2), max(0, cy - bh / 2)
    bw, bh = min(640 - x0, bw), min(360 - y0, bh)
    return {"x": round(x0 / 640, 4), "y": round(y0 / 360, 4), "w": round(bw / 640, 4), "h": round(bh / 360, 4),
            "source": "auto"}
