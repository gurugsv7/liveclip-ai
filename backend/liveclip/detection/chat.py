"""Per-stream chat activity analysis (Layer A)."""
from __future__ import annotations

import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass

import numpy as np

# viewers asking for a clip: "clip it", "clip that", "!clip", "clipped", "someone clip"
_CLIP_RE = re.compile(r"(^|\s)!clip\b|\bclip (it|that|this)\b|\bclipped\b|\bsomeone clip\b|\bclip clip\b")
_NORM_RE = re.compile(r"[^a-z0-9\U0001F300-\U0001FAFF ]+")


@dataclass
class ChatMsg:
    wall: float
    author: str
    text: str
    emotes: int
    keyword: bool
    laugh: bool
    norm: str
    clip_call: bool = False
    short: bool = False


def normalize(text: str) -> str:
    t = _NORM_RE.sub("", text.lower())
    t = re.sub(r"(.)\1{2,}", r"\1\1", t)  # loooool -> lool
    return " ".join(t.split())[:60]


class ChatAnalyzer:
    def __init__(self, keywords: list[str], laugh_tokens: list[str], history_s: float = 600.0):
        self._lock = threading.Lock()
        self.keywords = [k.lower() for k in keywords]
        self.laugh = [k.lower() for k in laugh_tokens]
        self.msgs: deque[ChatMsg] = deque(maxlen=20000)
        self.per_sec: deque[tuple[int, int]] = deque(maxlen=int(history_s))  # (sec, count)
        self.available = False
        self.total = 0

    def add(self, author: str, text: str, emotes: int, wall: float) -> ChatMsg:
        low = text.lower()
        kw = any(k in low for k in self.keywords)
        laugh = any(k in low for k in self.laugh) or bool(re.search(r"\b(ha){2,}|\bxd\b", low))
        clip_call = bool(_CLIP_RE.search(low))
        short = len(text.split()) <= 2
        m = ChatMsg(wall, author, text, emotes, kw, laugh, normalize(text), clip_call, short)
        with self._lock:
            self.available = True
            self.total += 1
            self.msgs.append(m)
            sec = int(wall)
            if self.per_sec and self.per_sec[-1][0] == sec:
                self.per_sec[-1] = (sec, self.per_sec[-1][1] + 1)
            else:
                last = self.per_sec[-1][0] if self.per_sec else sec - 1
                for s in range(last + 1, sec):
                    self.per_sec.append((s, 0))
                self.per_sec.append((sec, 1))
        return m

    def window(self, w0: float, w1: float) -> list[ChatMsg]:
        with self._lock:
            return [m for m in self.msgs if w0 <= m.wall <= w1]

    def rate(self, now: float | None = None, window: float = 5.0) -> float:
        now = now or time.time()
        return len(self.window(now - window, now)) / window

    def baseline(self, now: float) -> tuple[float, float]:
        with self._lock:
            counts = [c for s, c in self.per_sec if s < now - 10]
        # pad zero seconds since the last message
        if len(counts) < 30:
            return 0.5, 1.0
        arr = np.convolve(np.array(counts, dtype=float), np.ones(5) / 5, mode="valid")
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med))) * 1.4826
        return med, max(mad, 0.3 + 0.25 * med)

    def score(self, now: float | None = None, window: float = 6.0) -> dict:
        """Chat excitement relative to this channel's own baseline."""
        now = now or time.time()
        msgs = self.window(now - window, now)
        n = len(msgs)
        rate = n / window
        med, mad = self.baseline(now)
        z = (rate - med) / mad
        rate_score = min(1.0, max(0.0, (z - 1.0) / 5.0))
        if n == 0:
            return {"score": 0.0, "rate": 0.0, "baseline": med, "z": z, "keyword": 0.0, "laugh": 0.0,
                    "emote": 0.0, "repeat": 0.0, "short": 0.0, "clip_calls": 0, "available": self.available}
        kw = sum(m.keyword for m in msgs) / n
        laugh = sum(m.laugh for m in msgs) / n
        emote = min(1.0, sum(m.emotes for m in msgs) / max(n, 1) / 3)
        common = Counter(m.norm for m in msgs if m.norm).most_common(1)
        repeat = (common[0][1] / n) if common and n >= 4 else 0.0
        short = sum(m.short for m in msgs) / n          # LIGHTOR: reactions are short messages
        # distinct people asking for a clip in the last 15 s (crowd-sourced highlight)
        clip_calls = len({m.author for m in self.window(now - 15, now) if m.clip_call})
        content = 0.40 * kw + 0.25 * laugh + 0.15 * emote + 0.10 * repeat + 0.10 * short
        # Content only counts when the rate is at least near-normal.
        volume_gate = min(1.0, n / 6)
        score = min(1.0, 0.65 * rate_score + 0.35 * content * volume_gate + (0.1 if z > 3 and content > 0.3 else 0))
        return {"score": round(score, 3), "rate": round(rate, 2), "baseline": round(med, 2), "z": round(z, 2),
                "keyword": round(kw, 2), "laugh": round(laugh, 2), "emote": round(emote, 2),
                "repeat": round(repeat, 2), "short": round(short, 2), "clip_calls": clip_calls,
                "available": self.available}

    def laugh_ratio(self, w0: float, w1: float) -> float:
        msgs = self.window(w0, w1)
        return sum(m.laugh for m in msgs) / len(msgs) if msgs else 0.0

    def sample(self, w0: float, w1: float, limit: int = 40) -> list[str]:
        msgs = self.window(w0, w1)
        return [m.text[:120] for m in msgs[-limit:]]
