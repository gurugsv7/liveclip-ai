"""Deterministic sample media + chat fixtures for tests and demos.

Each fixture streamer has a unique test pattern and a unique audio tone
(A=440 Hz, B=880 Hz, C=1320 Hz, D=1760 Hz) so tests can prove — by FFT of
the extracted clip — that a clip contains only its own streamer's audio.
A loud "reaction" burst happens at a known time, and the chat fixture has a
matching spike `reaction_delay` seconds later, so detection and timestamp
alignment can be verified.

    python -m liveclip.fixtures [out_dir] [--duration 60] [--burst 35]
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from .config import Tools
from .media import run

STREAMERS = {
    "fixture_a": {"tone": 440, "pattern": "testsrc2", "tag": "[A]"},
    "fixture_b": {"tone": 880, "pattern": "smptehdbars", "tag": "[B]"},
    "fixture_c": {"tone": 1320, "pattern": "rgbtestsrc", "tag": "[C]"},
    "fixture_d": {"tone": 1760, "pattern": "testsrc", "tag": "[D]"},
}


def make_media(ffmpeg: str, out: Path, tone: int, pattern: str, duration: float, burst: float,
               size: str = "1280x720", fps: int = 30) -> None:
    """H.264/AAC, 1 s GOP (so 2-5 s segments cut cleanly). Quiet tone with a
    3 s loud burst starting at `burst` seconds; a short silence precedes it."""
    vol = (f"if(between(t,{burst - 2},{burst - 0.2}),0.003,"
           f"if(between(t,{burst},{burst + 3}),0.9,0.06))")
    run([ffmpeg, "-y", "-v", "error",
         "-f", "lavfi", "-i", f"{pattern}=size={size}:rate={fps}:duration={duration}",
         "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration={duration}",
         "-filter:a", f"volume='{vol}':eval=frame",
         "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-g", str(fps), "-keyint_min", str(fps),
         "-sc_threshold", "0", "-c:a", "aac", "-b:a", "128k", "-ac", "2", "-shortest", str(out)], timeout=300)


def make_chat(out: Path, tag: str, duration: float, burst: float, delay: float = 3.0, seed: int = 1) -> None:
    rnd = random.Random(seed)
    rows = []
    t = 0.0
    calm = ["hi chat", "what game is this", "lol ok", "first time here", "nice", "gg", "how long is stream"]
    hype = ["LMAO", "KEKW KEKW", "NO WAY", "CLIP IT", "BRO 💀", "LMAOOOO", "WTF", "😂😂😂", "OMEGALUL"]
    while t < duration:
        in_spike = burst + delay <= t <= burst + delay + 6
        rate = 9.0 if in_spike else 0.8
        t += rnd.expovariate(rate)
        if t >= duration:
            break
        txt = rnd.choice(hype if in_spike else calm)
        rows.append({"t": round(t, 3), "user": f"viewer{rnd.randint(1, 300)}", "text": f"{txt} {tag}",
                     "emotes": 1 if in_spike and rnd.random() < 0.5 else 0})
    out.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def generate(out_dir: Path, duration: float = 60, burst: float = 35, names: list[str] | None = None,
             ffmpeg: str | None = None) -> dict[str, dict]:
    ffmpeg = ffmpeg or Tools().ffmpeg
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found")
    out_dir.mkdir(parents=True, exist_ok=True)
    made = {}
    for name in names or list(STREAMERS):
        spec = STREAMERS[name]
        media = out_dir / f"{name}.mp4"
        chat = out_dir / f"{name}.chat.jsonl"
        marker = out_dir / f"{name}.json"
        meta = {"duration": duration, "burst": burst, **spec}
        if not (media.exists() and marker.exists() and json.loads(marker.read_text()) == meta):
            make_media(ffmpeg, media, spec["tone"], spec["pattern"], duration, burst)
            make_chat(chat, spec["tag"], duration, burst, seed=spec["tone"])
            marker.write_text(json.dumps(meta))
        made[name] = {"media": str(media), "chat": str(chat), **meta}
    return made


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?", default=str(Path(__file__).resolve().parents[2] / "fixtures" / "media"))
    ap.add_argument("--duration", type=float, default=60)
    ap.add_argument("--burst", type=float, default=35)
    a = ap.parse_args()
    for k, v in generate(Path(a.out), a.duration, a.burst).items():
        print(k, v["media"])
