"""Per-stream audio analysis (Layer A).

Consumes 16 kHz mono s16le PCM decoded by that stream's own FFmpeg process.
Keeps a ~150 s PCM ring (for transcription windows), 100 ms loudness frames,
and a robust per-streamer baseline (median/MAD of 1 s loudness over the last
5 minutes) so naturally loud streamers are normalized against themselves.
"""
from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass

import numpy as np

SR = 16000
FRAME = SR // 10  # 100 ms
SILENCE_DB = -65.0          # 1 s loudness at/below this = silence (muted, BRB, ad gap)
MIN_VOICED_BASELINE = 20    # voiced seconds needed before audio excitement is scored
# A reaction is sustained: full credit needs this many 100 ms frames of clearly-loud audio
# within +-2 s of the peak. A lone bang/thud/door slam/mic bump (real case: one 0.5 s hit in a
# bodycam video xqc was watching scored "audio 0.90") gets partial credit only.
SUSTAIN_FRAMES = 8


@dataclass
class AudioFrame:
    t: float          # session media time at frame end
    rms_db: float
    peak_db: float
    speech: bool


def _loudest_half_second(frames) -> float:
    best = -120.0
    for i in range(len(frames)):
        chunk = frames[i:i + 5]
        lin = [10 ** (f.rms_db / 20) for f in chunk]
        best = max(best, _db(math.sqrt(sum(x * x for x in lin) / len(lin))))
    return best


def _db(x: float) -> float:
    return 20 * math.log10(max(x, 1e-6))


class AudioAnalyzer:
    def __init__(self, ring_seconds: float = 150.0, baseline_seconds: float = 300.0):
        self._lock = threading.Lock()
        self.ring_len = int(ring_seconds * SR)
        self.ring = np.zeros(self.ring_len, dtype=np.int16)
        self.ring_pos = 0           # total samples ever written in current run
        self.run_offset = 0.0       # media time at sample 0 of current run
        self._pending = np.zeros(0, dtype=np.int16)
        self.frames: deque[AudioFrame] = deque(maxlen=int(ring_seconds * 10))
        self.sec_history: deque[float] = deque(maxlen=int(baseline_seconds))
        # loudest 0.5 s of each voiced second: "how loud this streamer's normal talking peaks get".
        # Excitement compares a window's loudest 0.5 s against THIS (peaks vs peaks) — comparing it
        # with the per-second average made every normal sentence look like shouting.
        self.peak_history: deque[float] = deque(maxlen=int(baseline_seconds))
        self._sec_acc: list[float] = []
        self.total_seconds = 0.0
        self.voiced_seconds = 0.0
        self.on_second = None  # callback(media_t, rms_db, peak_db, excitement, speech_ratio)
        self.noise_floor_db = -60.0

    # --- ingestion (called from the worker's PCM reader thread) ----------
    def start_run(self, media_offset: float) -> None:
        with self._lock:
            self.run_offset = media_offset
            self.ring_pos = 0
            self._pending = np.zeros(0, dtype=np.int16)

    def feed(self, pcm: bytes) -> None:
        if len(pcm) % 2:
            pcm = pcm[:-1]
        data = np.frombuffer(pcm, dtype=np.int16)
        with self._lock:
            # ring write
            n = len(data)
            if n >= self.ring_len:
                data_r = data[-self.ring_len:]
                self.ring[:] = data_r
            else:
                start = self.ring_pos % self.ring_len
                end = start + n
                if end <= self.ring_len:
                    self.ring[start:end] = data
                else:
                    k = self.ring_len - start
                    self.ring[start:] = data[:k]
                    self.ring[: n - k] = data[k:]
            buf = np.concatenate([self._pending, data]) if len(self._pending) else data
            nframes = len(buf) // FRAME
            base_pos = self.ring_pos - len(self._pending)
            self.ring_pos += n
            for i in range(nframes):
                fr = buf[i * FRAME:(i + 1) * FRAME].astype(np.float32) / 32768.0
                rms = float(np.sqrt(np.mean(fr * fr)))
                peak = float(np.max(np.abs(fr)))
                rms_db = _db(rms)
                # crude energy + zero-crossing voice activity estimate
                zc = float(np.mean(np.abs(np.diff(np.signbit(fr).astype(np.int8)))))
                speech = rms_db > self.noise_floor_db + 12 and 0.02 < zc < 0.35
                t = self.run_offset + (base_pos + (i + 1) * FRAME) / SR
                self.frames.append(AudioFrame(t, rms_db, _db(peak), speech))
                self._sec_acc.append(rms_db)
                if len(self._sec_acc) == 10:
                    self._finish_second(t)
            self._pending = buf[nframes * FRAME:].copy()

    def _finish_second(self, t: float) -> None:
        lin = [10 ** (d / 20) for d in self._sec_acc]
        sec_db = _db(math.sqrt(sum(x * x for x in lin) / len(lin)))
        recent = list(self.frames)[-10:]
        peak = max(f.peak_db for f in recent)
        speech_ratio = sum(1 for f in recent if f.speech) / len(recent)
        self.total_seconds += 1
        # Silent seconds (muted mic, BRB screen, ad break) are not "normal loudness":
        # counting them would make ordinary talking look like a huge spike afterwards.
        if sec_db > SILENCE_DB:
            self.sec_history.append(sec_db)
            self.voiced_seconds += 1
            self.peak_history.append(_loudest_half_second(recent))
        # adapt noise floor slowly to the quietest seconds
        if len(self.sec_history) >= 10:
            self.noise_floor_db = float(np.percentile(np.array(self.sec_history), 5))
        self._sec_acc = []
        if self.on_second:
            exc = self._excitement_locked(t, 1.0)[0]
            try:
                self.on_second(t, sec_db, peak, exc, speech_ratio)
            except Exception:
                pass

    # --- queries ----------------------------------------------------------
    def baseline(self) -> tuple[float, float]:
        if len(self.peak_history) < 5:
            return -30.0, 6.0
        arr = np.array(self.peak_history)
        med = float(np.median(arr))
        mad = float(np.median(np.abs(arr - med))) * 1.4826
        return med, max(mad, 3.0)

    def _excitement_locked(self, t_end: float, window: float) -> tuple[float, float]:
        """Return (excitement 0..1, peak time) for the last `window` seconds ending at t_end."""
        sel = [f for f in self.frames if t_end - window < f.t <= t_end]
        if not sel or len(self.sec_history) < MIN_VOICED_BASELINE:
            return 0.0, t_end          # no idea yet what "normal" sounds like for this streamer
        med, mad = self.baseline()
        # loudness of the loudest 500ms within the window
        best, best_t = -120.0, t_end
        for i in range(len(sel)):
            chunk = sel[i:i + 5]
            lin = [10 ** (f.rms_db / 20) for f in chunk]
            d = _db(math.sqrt(sum(x * x for x in lin) / len(lin)))
            if d > best:
                best, best_t = d, chunk[len(chunk) // 2].t
        z = (best - med) / mad
        score = min(1.0, max(0.0, (z - 1.0) / 4.0))
        # silence followed by a loud reaction
        # (a brief pause inside normal talking — not coming back from a long silent break)
        before = [f.rms_db for f in self.frames if best_t - 3.0 < f.t <= best_t - 0.6]
        earlier = [f.rms_db for f in self.frames if best_t - 12.0 < f.t <= best_t - 3.0]
        was_talking = bool(earlier) and sum(d > SILENCE_DB for d in earlier) >= 0.5 * len(earlier)
        if before and was_talking and np.mean(before) < med - 8 and best > med + 6:
            score = min(1.0, score + 0.2)
        loud = med + 1.5 * mad
        n_loud = sum(1 for f in self.frames if abs(f.t - best_t) <= 2.0 and f.rms_db >= loud)
        score *= min(1.0, max(0.0, (n_loud - 1) / (SUSTAIN_FRAMES - 1)))
        return score, best_t

    def excitement(self, t_end: float | None = None, window: float = 2.0) -> tuple[float, float]:
        with self._lock:
            if t_end is None:
                t_end = self.latest_t()
            return self._excitement_locked(t_end, window)

    def latest_t(self) -> float:
        return self.run_offset + self.ring_pos / SR

    def level_db(self) -> float:
        with self._lock:
            recent = list(self.frames)[-3:]
        return max((f.rms_db for f in recent), default=-120.0)

    def speech_ratio(self, t0: float, t1: float) -> float:
        with self._lock:
            sel = [f.speech for f in self.frames if t0 <= f.t <= t1]
        return sum(sel) / len(sel) if sel else 0.0

    def get_pcm(self, t0: float, t1: float) -> np.ndarray | None:
        """PCM for media interval [t0, t1] from the ring (current run only)."""
        with self._lock:
            s0 = int((t0 - self.run_offset) * SR)
            s1 = int((t1 - self.run_offset) * SR)
            oldest = max(0, self.ring_pos - self.ring_len)
            s0 = max(s0, oldest)
            s1 = min(s1, self.ring_pos)
            if s1 <= s0:
                return None
            idx = np.arange(s0, s1) % self.ring_len
            return self.ring[idx].copy()

    def has_baseline(self, min_seconds: float) -> bool:
        return self.voiced_seconds >= min_seconds
