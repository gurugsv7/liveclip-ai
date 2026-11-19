"""Offline detection over a whole recording.

Runs exactly the live pipeline — AudioAnalyzer (loudness vs this streamer's own
baseline), YAMNet sound events, live-speech windows, chat replay and the same
HypeDetector — but driven by the recording's own clock instead of the wall clock,
so a 3-hour VOD is analysed in minutes. Pure logic + injected models: unit-testable.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

import numpy as np

from ..ai.scoring import SpeechNormalizer, live_speech_raw
from ..detection.audio import SR, AudioAnalyzer
from ..detection.chat import ChatAnalyzer
from ..detection.hype import Candidate, HypeDetector, HypeParams
from ..detection.sound_events import apply_sound

TICK_S = 0.5
# YAMNet on 2.5 s windows every 2 s covers every instant (live runs it every 1 s because it only
# ever sees the newest audio); a sound result stays valid for 3.5 s (apply_sound max_age)
SOUND_EVERY_S = 2.0
CHAT_ORIGIN = 1_000_000_000.0   # chat wall clock = origin + media time (never 0: ChatAnalyzer treats 0 as "now")


class Cancelled(RuntimeError):
    pass


@dataclass
class VodMoment:
    id: str
    t_start: float
    t_end: float
    t_event: float
    peak: float
    audio: float
    chat: float | None
    speech: float
    signals: dict = field(default_factory=dict)


def params_from_config(cfg) -> HypeParams:
    d, c = cfg.detection, cfg.clips
    return HypeParams(trigger=d.trigger_threshold, release=d.release_threshold, merge_window_s=d.merge_window_s,
                      cooldown_s=d.cooldown_s, pre_s=c.pre_context_s, post_s=c.post_context_s,
                      max_wait_s=c.max_reaction_wait_s, max_clip_s=c.max_clip_s, min_baseline_s=d.min_baseline_s,
                      w_audio=d.lightweight_audio_weight, w_chat=d.lightweight_chat_weight,
                      single_signal_factor=d.single_signal_factor, chat_delay_s=d.chat_reaction_delay_s)


class VodAnalyzer:
    """
    sound(pcm_int16) -> YAMNet dict | None            (optional)
    speech(pcm_int16) -> {"words": [...], "text": str} (optional; word times relative to the window)
    chat: [(media_t, author, text, emotes)] sorted by media_t (optional; VOD chat replay)
    """

    def __init__(self, cfg, *, new_id: Callable[[], str], sound=None, speech=None,
                 chat: list[tuple[float, str, str, int]] | None = None,
                 progress: Callable[[float], None] | None = None, cancel: threading.Event | None = None,
                 on_second: Callable[[float, float, float, float, float], None] | None = None):
        self.cfg = cfg
        d = cfg.detection
        self.audio = AudioAnalyzer()
        self.audio.on_second = on_second
        self.chat = ChatAnalyzer(d.keywords, d.laugh_tokens)
        self.chat_msgs = chat or []
        self._ci = 0
        self.sound_fn = sound if d.sound_events_enabled else None
        self.speech_fn = speech if d.live_speech_enabled else None
        self.speech_norm = SpeechNormalizer()
        self.progress = progress
        self.cancel = cancel
        self.moments: list[VodMoment] = []
        self.detector = HypeDetector(params_from_config(cfg), new_id=new_id, on_trigger=lambda *a: None,
                                     on_extend=lambda *a: None, on_finalize=self._on_finalize)
        self.sound: dict | None = None
        self.speech: dict | None = None
        self.stats = {"ticks": 0, "sound_windows": 0, "speech_windows": 0, "chat_messages": len(self.chat_msgs)}

    def _on_finalize(self, c: Candidate, s: float, e: float) -> None:
        self.moments.append(VodMoment(c.id, s, e, c.t_event, round(c.peak, 3), round(c.audio, 3),
                                      round(c.chat, 3) if self.chat.available else None, round(c.speech, 3),
                                      {**c.signals, "chat_available": self.chat.available}))

    def run(self, pcm_chunks: Iterable[bytes], duration: float | None = None) -> list[VodMoment]:
        d = self.cfg.detection
        next_tick = TICK_S
        next_sound = 2.5
        next_speech = d.live_speech_window_s
        buf = b""
        step = int(SR * TICK_S) * 2                      # bytes of s16le mono per tick
        for chunk in pcm_chunks:
            buf += chunk
            while len(buf) >= step:
                if self.cancel is not None and self.cancel.is_set():
                    raise Cancelled("cancelled")
                self.audio.feed(buf[:step])
                buf = buf[step:]
                t = self.audio.latest_t()
                if t + 1e-6 < next_tick:
                    continue
                next_tick += TICK_S
                self._feed_chat(t)
                if self.sound_fn and t >= next_sound:
                    next_sound = t + SOUND_EVERY_S
                    self._run_sound(t)
                if self.speech_fn and t >= next_speech:
                    next_speech = t + d.live_speech_interval_s
                    self._run_speech(t)
                self._tick(t)
                if self.progress and duration and self.stats["ticks"] % 240 == 0:
                    self.progress(min(1.0, t / duration))
        self.detector.force_finalize(self.audio.latest_t())
        return self.moments

    # --- signals --------------------------------------------------------------
    def _feed_chat(self, t: float) -> None:
        msgs = self.chat_msgs
        while self._ci < len(msgs) and msgs[self._ci][0] <= t:
            mt, author, text, emotes = msgs[self._ci]
            self.chat.add(author, text, emotes, CHAT_ORIGIN + mt)
            self._ci += 1

    def _run_sound(self, t1: float) -> None:
        t0 = t1 - 2.5
        pcm = self.audio.get_pcm(t0, t1)
        res = self.sound_fn(pcm) if pcm is not None else None
        self.stats["sound_windows"] += 1
        if res is not None:
            self.sound = {**res, "t_end": t1, "peak_t": t0 + res["peak_offset"]}

    def _run_speech(self, t1: float) -> None:
        d = self.cfg.detection
        t0 = t1 - d.live_speech_window_s
        if self.audio.speech_ratio(t0, t1) < 0.15:
            return                                       # nobody talking: skip the GPU
        pcm = self.audio.get_pcm(t0, t1)
        if pcm is None or len(pcm) < SR * 3:
            return
        res = self.speech_fn(np.asarray(pcm))
        self.stats["speech_windows"] += 1
        if not res:
            return
        raw, peak, hits = live_speech_raw(res.get("words") or [])
        val = self.speech_norm(raw)
        self.speech = {"score": val, "raw": raw, "t_end": t1, "peak_t": (t0 + peak) if peak is not None else None,
                       "text": (res.get("text") or "")[-200:], "hits": hits}

    def _tick(self, t: float) -> None:
        """Same combination as LiveSession.detection_tick, on media time."""
        d = self.cfg.detection
        self.stats["ticks"] += 1
        a, a_t = self.audio.excitement(t, window=2.0 + d.chat_reaction_delay_s)
        a, a_t, _ = apply_sound(a, a_t, self.sound, t, d.game_noise_threshold, d.game_noise_discount)
        cs = self.chat.score(CHAT_ORIGIN + t)
        chat_val = cs["score"] if self.chat.available else None
        if chat_val is not None and cs.get("clip_calls", 0) >= d.crowd_clip_calls:
            chat_val = max(chat_val, 0.9)
        sp, speech, speech_t = self.speech, 0.0, None
        if sp and t - sp["t_end"] <= d.live_speech_interval_s + 4:
            speech, speech_t = sp["score"], sp["peak_t"]
        self.detector.tick(t, a, a_t, chat_val, self.audio.voiced_seconds,
                           extra={"chat": cs, "audio_db": self.audio.level_db(),
                                  "speech": {k: sp[k] for k in ("score", "raw", "hits", "text")} if speech else None},
                           speech=speech, speech_peak_t=speech_t)
