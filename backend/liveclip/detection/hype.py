"""Hype candidate state machine (Layer A -> Layer B hand-off).

Pure logic, independent of media and IO so it can be unit tested. One instance
per stream session; it only ever sees that session's signals.

Lifecycle of a candidate:
  trigger  -> footage [t-pre, t+post+wait] is protected immediately
  extend   -> further hot ticks inside merge_window extend the same candidate
              (no near-duplicate clips from one scream/chat spike)
  finalize -> once the reaction has cooled (score < release for `cool_s`) or
              the max wait elapsed, final clip boundaries are computed.
Triggers inside `cooldown_s` after a finalized candidate are suppressed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Candidate:
    id: str
    t_event: float          # media time of the peak moment
    t_first: float          # first hot tick
    t_last_hot: float       # last tick above release threshold
    peak: float
    audio: float = 0.0
    chat: float = 0.0
    speech: float = 0.0
    merged: int = 1
    signals: dict = field(default_factory=dict)


@dataclass
class HypeParams:
    trigger: float = 0.62
    release: float = 0.40
    merge_window_s: float = 25.0
    cooldown_s: float = 20.0
    cool_s: float = 4.0
    pre_s: float = 30.0
    post_s: float = 20.0
    max_wait_s: float = 30.0
    max_clip_s: float = 90.0
    min_baseline_s: float = 45.0
    w_audio: float = 0.5
    w_chat: float = 0.5
    audio_only_penalty: float = 0.08
    single_signal_factor: float = 0.85
    chat_delay_s: float = 3.0
    w_speech_boost: float = 0.2


class HypeDetector:
    def __init__(self, params: HypeParams, new_id: Callable[[], str],
                 on_trigger: Callable[[Candidate, float, float], None],
                 on_extend: Callable[[Candidate, float, float], None],
                 on_finalize: Callable[[Candidate, float, float], None]):
        self.p = params
        self.new_id = new_id
        self.on_trigger = on_trigger
        self.on_extend = on_extend
        self.on_finalize = on_finalize
        self.open: Candidate | None = None
        self.last_final_end: float = -1e9
        self.suppressed = 0
        self.last_score = 0.0
        self.enabled = True

    def combine(self, audio: float, chat: float | None, speech: float = 0.0) -> float:
        f = self.p.single_signal_factor
        if chat is None:  # chat unavailable: audio only, slightly stricter
            both = max(0.0, audio - self.p.audio_only_penalty)
            single = f * speech
        else:
            both = self.p.w_audio * audio + self.p.w_chat * chat + 0.15 * min(audio, chat)
            # A single very strong signal is enough on its own (chat explodes over a
            # calm voice, a scream over slow chat, or excited words at normal volume).
            single = f * max(audio, chat, speech)
        both += self.p.w_speech_boost * speech
        return max(both, single)

    def _event_time(self, t: float, audio: float, audio_peak_t: float, chat: float | None,
                    speech: float, speech_peak_t: float | None) -> float:
        strongest = max(audio, chat or 0.0, speech)
        if speech_peak_t is not None and speech >= strongest and speech > 0:
            return speech_peak_t
        if audio >= (chat or 0):
            return audio_peak_t
        return max(0.0, t - self.p.chat_delay_s)    # chat reacts a few seconds after the moment

    def tick(self, t: float, audio: float, audio_peak_t: float, chat: float | None,
             baseline_s: float, extra: dict | None = None, speech: float = 0.0,
             speech_peak_t: float | None = None) -> float:
        score = min(1.0, self.combine(audio, chat, speech))
        self.last_score = score
        if not self.enabled:
            if self.open:
                self._finalize(t)
            return score
        c = self.open
        if c is not None:
            if score >= self.p.release:
                c.t_last_hot = t
                if score > c.peak:
                    c.peak = score
                    c.audio, c.chat, c.speech = audio, chat or 0.0, speech
                    # move the event point to the stronger peak (chat-led peaks keep the original point)
                    if audio >= (chat or 0) or (speech_peak_t is not None and speech >= max(audio, chat or 0)):
                        c.t_event = self._event_time(t, audio, audio_peak_t, chat, speech, speech_peak_t)
                if score >= self.p.trigger and t - c.t_first <= self.p.merge_window_s + self.p.max_wait_s:
                    c.merged += 1
                    self.on_extend(c, *self._protect_range(c))
            if (t - c.t_last_hot >= self.p.cool_s) or (t - c.t_event >= self.p.max_wait_s):
                self._finalize(t)
            return score
        if score >= self.p.trigger and baseline_s >= self.p.min_baseline_s:
            if t - self.last_final_end < self.p.cooldown_s:
                self.suppressed += 1
                return score
            ev_t = self._event_time(t, audio, audio_peak_t, chat, speech, speech_peak_t)
            c = Candidate(self.new_id(), ev_t, t, t, score, audio, chat or 0.0, speech=speech, signals=dict(extra or {}))
            self.open = c
            self.on_trigger(c, *self._protect_range(c))
        return score

    def _protect_range(self, c: Candidate) -> tuple[float, float]:
        return max(0.0, c.t_event - self.p.pre_s), c.t_event + self.p.post_s + self.p.max_wait_s

    def boundaries(self, c: Candidate) -> tuple[float, float]:
        start = max(0.0, c.t_event - self.p.pre_s)
        end = max(c.t_event + self.p.post_s, c.t_last_hot + 3.0)
        if end - start > self.p.max_clip_s:
            # keep the reaction, trim setup first
            start = max(start, end - self.p.max_clip_s)
            if c.t_event - start < 8:
                start = max(0.0, c.t_event - 8)
                end = start + self.p.max_clip_s
        return start, end

    def _finalize(self, t: float) -> None:
        c = self.open
        if not c:
            return
        self.open = None
        s, e = self.boundaries(c)
        self.last_final_end = e
        self.on_finalize(c, s, e)

    def force_finalize(self, t: float) -> None:
        self._finalize(t)
