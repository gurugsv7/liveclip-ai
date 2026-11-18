"""Tests for the high-recall detection additions: sound events (YAMNet), game-noise
gating, crowd "clip it", Twitch viewer clips and the storage-capped Maybe pool."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest

from liveclip.config import AppConfig
from liveclip.detection.chat import ChatAnalyzer
from liveclip.detection.sound_events import SoundEventClassifier, apply_sound, reaction_score

MODELS = Path(__file__).resolve().parents[2] / "data" / "models"
SR = 16000


# ---------------------------------------------------------------- game noise / reactions
def _snd(reaction: float, game: float, t_end: float = 100.0, peak_t: float = 99.0) -> dict:
    return {"reaction": reaction, "game": game, "t_end": t_end, "peak_t": peak_t, "laugh": reaction, "scream": 0.0,
            "top": "x"}


def test_gta_gunfire_loudness_is_discounted_when_nobody_reacts():
    a, t, react = apply_sound(0.9, 98.0, _snd(reaction=0.05, game=0.8), 100.0, 0.35, 0.4)
    assert a == pytest.approx(0.36) and react == 0.0


def test_laughter_counts_even_at_normal_volume():
    # real numbers: chess-win clip had Laughter 0.82 while loudness excitement was ~0.3
    a, t, react = apply_sound(0.3, 98.0, _snd(reaction=0.82, game=0.0, peak_t=97.5), 100.0, 0.35, 0.4)
    assert a == react == 1.0 and t == 97.5


def test_scream_over_gunfire_is_not_discounted():
    a, _, react = apply_sound(0.8, 98.0, _snd(reaction=0.6, game=0.9), 100.0, 0.35, 0.4)
    assert a >= 0.8 and react > 0.8


def test_stale_sound_window_is_ignored():
    a, _, react = apply_sound(0.5, 98.0, _snd(reaction=0.9, game=0.0, t_end=90.0), 100.0, 0.35, 0.4)
    assert a == 0.5 and react == 0.0


def test_reaction_score_mapping():
    assert reaction_score(0.1) == 0.0 and reaction_score(0.4) == pytest.approx(0.5) and reaction_score(0.9) == 1.0


@pytest.mark.skipif(not (MODELS / "yamnet" / "yamnet.onnx").exists(), reason="YAMNet model not downloaded")
def test_yamnet_does_not_call_a_tone_or_silence_a_reaction():
    clf = SoundEventClassifier(MODELS)
    t = np.arange(int(2.5 * SR)) / SR
    tone = (np.sin(2 * np.pi * 440 * t) * 0.5 * 32767).astype(np.int16)
    for pcm in (tone, np.zeros(int(2.5 * SR), np.int16)):
        r = clf.classify(pcm)
        assert r is not None and r["reaction"] < 0.15
    assert clf.status == "ready"


def test_missing_model_disables_sound_events_cleanly(tmp_path):
    clf = SoundEventClassifier(tmp_path)
    assert clf.classify(np.zeros(SR * 2, np.int16)) is None
    assert clf.status == "unavailable"


# ---------------------------------------------------------------- crowd "clip it"
def test_crowd_clip_calls_need_distinct_viewers():
    cfg = AppConfig()
    ca = ChatAnalyzer(cfg.detection.keywords, cfg.detection.laugh_tokens)
    now = time.time()
    for i in range(5):
        ca.add("same_guy", "CLIP IT", 0, now - 5 + i)
    assert ca.score(now)["clip_calls"] == 1
    for name, msg in (("a", "someone clip that"), ("b", "!clip"), ("c", "CLIPPED")):
        ca.add(name, msg, 0, now - 1)
    assert ca.score(now)["clip_calls"] == 4
    assert ca.score(now + 30)["clip_calls"] == 0          # only the last 15 s count


def test_short_message_ratio_feature():
    cfg = AppConfig()
    ca = ChatAnalyzer(cfg.detection.keywords, cfg.detection.laugh_tokens)
    now = time.time()
    for i in range(10):
        ca.add(f"u{i}", "LUL", 1, now - 3 + i * 0.2)
    assert ca.score(now)["short"] == 1.0


# ---------------------------------------------------------------- Twitch viewer clips
def test_viewer_clip_watcher_emits_only_new_clips_after_start(monkeypatch):
    import httpx

    from liveclip.platforms import twitch_clips as tc

    monkeypatch.setenv("TWITCH_CLIENT_ID", "cid")
    monkeypatch.setenv("TWITCH_CLIENT_SECRET", "secret")
    started = time.time()
    iso = lambda t: tc._rfc3339(t)
    clips = [{"id": "old", "created_at": iso(started - 600), "duration": 30, "title": "before", "creator_name": "x"},
             {"id": "new1", "created_at": iso(started + 5), "duration": 28.4, "title": "NO WAY", "creator_name": "viewer1",
              "url": "https://clips.twitch.tv/new1", "view_count": 3}]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.host == "id.twitch.tv":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["Client-Id"] == "cid" and req.headers["Authorization"] == "Bearer tok"
        if req.url.path.endswith("/users"):
            assert req.url.params["login"] == "jynxzi"
            return httpx.Response(200, json={"data": [{"id": "123"}]})
        assert req.url.params["broadcaster_id"] == "123"
        return httpx.Response(200, json={"data": clips})

    real = httpx.AsyncClient
    monkeypatch.setattr(tc.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    got = []

    async def run():
        w = tc.TwitchClipWatcher("jynxzi", got.append, interval_s=0.05)
        w.started_wall = started
        w.start()
        await asyncio.sleep(0.4)
        await w.stop()
        return w

    w = asyncio.run(run())
    assert [g["id"] for g in got] == ["new1"], "old clips ignored, each clip reported once"
    assert got[0]["creator"] == "viewer1" and got[0]["duration"] == 28.4
    assert w.info()["status"] == "watching" and w.info()["found"] == 1


def test_viewer_clip_watcher_without_credentials_is_unavailable(monkeypatch):
    from liveclip.platforms.twitch_clips import TwitchClipWatcher

    monkeypatch.delenv("TWITCH_CLIENT_ID", raising=False)
    monkeypatch.delenv("TWITCH_CLIENT_SECRET", raising=False)

    async def run():
        w = TwitchClipWatcher("x", lambda c: None)
        w.start()
        await asyncio.sleep(0.05)
        return w.info()

    assert asyncio.run(run())["status"] == "unavailable"


# ---------------------------------------------------------------- Maybe pool budget
def test_maybe_pool_is_capped_and_evicts_lowest_scores_first(tmp_path):
    from liveclip.context import AppContext

    ctx = AppContext(data_dir=tmp_path, config_overrides={"clips": {"maybe_budget_bytes": 2500}})
    ctx.db.insert("streamers", {"id": "s", "slug": "s", "name": "S", "platform": "local", "url": "local:x",
                                "created_at": time.time()})
    keep_review = tmp_path / "review.mp4"
    keep_review.write_bytes(b"x" * 5000)
    now = time.time()
    rows = [("m_low", "maybe", 0.26), ("m_mid", "maybe", 0.40), ("m_high", "maybe", 0.49), ("rv", "ready_for_review", 0.1)]
    for i, (cid, status, score) in enumerate(rows):
        f = keep_review if cid == "rv" else tmp_path / f"{cid}.mp4"
        if cid != "rv":
            f.write_bytes(b"x" * 1000)
        ctx.db.insert("clips", {"id": cid, "streamer_id": "s", "session_id": "ses", "platform": "local", "source_url": "x",
                                "t_start": 0, "t_end": 10, "detection_method": "auto", "status": status, "ai_score": score,
                                "file_path": str(f), "file_size": f.stat().st_size, "created_at": now + i, "updated_at": now})
    removed = ctx.clips.enforce_maybe_budget()
    assert removed == 1
    st = {r["id"]: r["status"] for r in ctx.db.all("SELECT id, status FROM clips")}
    assert st == {"m_low": "discarded", "m_mid": "maybe", "m_high": "maybe", "rv": "ready_for_review"}
    assert not (tmp_path / "m_low.mp4").exists() and keep_review.exists()


# ---------------------------------------------------------------- Layer B balance (real session numbers)
def _final(**sc):
    from liveclip.ai import scoring
    s = dict(sc)
    scoring.drop_quiet_partner(s)
    return scoring.weighted(s, AppConfig().detection.weights.model_dump())


def test_chat_backed_moment_goes_to_review_like_real_not_fair_clip():
    # real: chat 0.80, audio 0.009 -> old scoring discarded it at 0.46
    f = _final(chat=0.804, audio=0.0093, speech=None, transcript=0.397, laughter=0.214, novelty=0.914)
    assert f >= AppConfig().detection.save_threshold


def test_speech_only_moment_is_kept_in_maybe_pool_not_deleted_or_promoted():
    # real: "Let's go, let's go." — live speech 1.0, chat 0.1, no loudness
    cfg = AppConfig()
    f = _final(chat=0.10, audio=0.0, speech=1.0, transcript=0.3, laughter=0.05, novelty=0.9)
    assert cfg.clips.maybe_threshold <= f < cfg.detection.save_threshold


def test_boring_moment_still_discarded():
    cfg = AppConfig()
    f = _final(chat=0.05, audio=0.02, speech=0.1, transcript=0.02, laughter=0.0, novelty=0.3)
    assert f < cfg.clips.maybe_threshold


# ---------------------------------------------------------------- real R6 clips (2026-11-18)
def test_game_noise_with_nobody_voicing_is_not_excitement_like_real_comms_clip():
    """'I guess I'll play with comms': streamer silent, R6 footsteps/gunfire (YAMNet even said
    'Applause 0.80'). Scored 'audio excitement 1.00'. Loudness without a human voicing must not count."""
    from liveclip.detection.sound_events import REACTION
    assert "Applause" not in REACTION and "Cheering" not in REACTION
    # footsteps/cracks aren't in the gunfire list, but nobody is voicing -> discounted anyway
    a, _, react = apply_sound(1.0, 10.0, {"reaction": 0.05, "game": 0.2, "voice": 0.1, "t_end": 100, "peak_t": 99}, 100.0, 0.35, 0.4)
    assert a == pytest.approx(0.4) and react == 0.0
    # the same loudness while he is talking/yelling still counts
    a, _, _ = apply_sound(1.0, 10.0, {"reaction": 0.05, "game": 0.2, "voice": 0.9, "t_end": 100, "peak_t": 99}, 100.0, 0.35, 0.4)
    assert a == 1.0


def test_reel_keeps_untranscribed_crash_out_and_the_payoff_like_real_c4_clip():
    """'I had a C4, why did I swing?': he crashes out at ~24-34 s but Whisper returned no words
    there (garbled rage under replay music). The old cutter treated it as dead air and dropped it."""
    from liveclip.editing.highlight import best_reel_cut

    def ws(t, phrase):
        return [{"text": w, "start": round(t + i * 0.3, 2), "end": round(t + i * 0.3 + 0.25, 2)} for i, w in enumerate(phrase.split())]
    words = (ws(0.4, "What do you mean a piano") + ws(8.4, "Oh my god. I just have the worst name of all time")
             + ws(13.0, "Oh, yo, I have a C4 new bow.") + ws(20.2, "I just say I'll just say")
             + ws(36.0, "Come on, come on, ZZ, blow, blow, blow.") + ws(39.7, "Oh my god one more CS, one more CS.")
             + ws(43.0, "He was slow I had a C4, why did I swing?") + ws(46.5, "Yo, he put a come out of my door. Wait."))
    # YAMNet timeline: voice everywhere he talks, moderate voice (0.3-0.45) during the crash-out
    tl = []
    for k in range(105):
        t = round(k * 0.48, 2)
        voice = 0.4 if 23.0 <= t <= 35.0 else (0.9 if any(w["start"] - 0.5 <= t <= w["end"] + 0.5 for w in words) else 0.05)
        tl.append({"t": t, "voice": voice, "reaction": 0.0, "game": 0.0, "rms_db": -28.0})
    pieces = best_reel_cut(words, 50.0, None, 30.0, timeline=tl)
    total = sum(b - a for a, b in pieces)
    assert total <= 30.0
    covered = lambda t: any(a <= t <= b for a, b in pieces)
    assert all(covered(t) for t in (26.0, 30.0, 33.0)), f"crash-out dropped: {pieces}"
    assert covered(45.5), f"payoff 'why did I swing' dropped: {pieces}"


# ---------------------------------------------------------------- audit fixes (2026-11-18, 46 real clips)
def test_normal_talking_peaks_are_not_excitement_but_a_shout_is():
    """Rubius talk-show clips scored 'audio 0.7-0.9' just for talking: a window's loudest 0.5 s
    was compared with the per-second average. Peaks must be compared with normal peaks."""
    from liveclip.detection.audio import AudioAnalyzer
    a = AudioAnalyzer()
    a.start_run(0)
    rng = np.random.default_rng(0)
    for _ in range(90):                                   # syllables: 0.2 s loud, 0.2 s quiet
        for k in range(5):
            amp = 0.12 * (1.0 + 0.2 * rng.standard_normal()) if k % 2 == 0 else 0.015
            t = np.arange(int(0.2 * SR)) / SR
            a.feed((np.sin(2 * np.pi * 220 * t) * amp * 32767).astype(np.int16).tobytes())
    assert a.excitement(window=2)[0] < 0.3
    t = np.arange(int(1.0 * SR)) / SR
    a.feed((np.sin(2 * np.pi * 300 * t) * 0.6 * 32767).astype(np.int16).tobytes())   # ~+14 dB shout
    assert a.excitement(window=2)[0] > 0.6


def test_reaction_phrase_count_uses_whole_words_only():
    from liveclip.ai.scoring import transcript_features
    plain = "Let me show you how the running system works, what do you think, wait for it"
    assert transcript_features(plain, [{"text": "x", "start": 0, "end": 1}] * 15, 10)["shock"] == 0
    hyped = "No way! Oh my god, that was the worst sell of all time"
    assert transcript_features(hyped, [{"text": "x", "start": 0, "end": 1}] * 12, 10)["shock"] == 3


def test_hook_prefers_a_full_reaction_line_over_a_fragment():
    from liveclip.ai.scoring import title_suggestions

    def ws(t, phrase):
        return [{"text": w, "start": round(t + i * 0.3, 2), "end": round(t + i * 0.3 + 0.25, 2)} for i, w in enumerate(phrase.split())]
    words = ws(0, "So this is interesting because I have this on the board.") + ws(6, "Oh my god.") \
        + ws(9, "Am I actually Magnus Carlsen like legitimately?") + ws(14, "And you find someone who is taking a")
    t = title_suggestions("x", words, "", 9.0)
    assert t[0].startswith("Am I actually Magnus") and not t[0].startswith("And")


def test_auto_moment_inside_previous_clip_is_merged_not_duplicated(tmp_path):
    """7 real overlapping pairs (e.g. 248-314 s and 302-368 s): the same footage stored twice."""
    from types import SimpleNamespace
    from liveclip.context import AppContext

    ctx = AppContext(data_dir=tmp_path)
    ctx.db.insert("streamers", {"id": "s", "slug": "s", "name": "S", "platform": "local", "url": "local:x", "created_at": time.time()})
    ctx.db.insert("stream_sessions", {"id": "ses", "streamer_id": "s", "platform": "local", "url": "x", "state": "live",
                                      "started_at": time.time(), "dir": str(tmp_path)})
    live = SimpleNamespace(streamer={"id": "s", "platform": "local", "url": "local:x", "default_layout": "smart_crop"},
                           session_id="ses", origin_wall=time.time(), media_now=lambda: 400.0)
    first = ctx.clips.create_clip(live, 248, 314, "auto", None, t_event=278)
    same = ctx.clips.create_clip(live, 280, 340, "auto", None, t_event=300)      # moment inside the first clip
    assert same == first
    nxt = ctx.clips.create_clip(live, 302, 368, "auto", None, t_event=332)       # new moment after it
    r = ctx.db.one("SELECT t_start FROM clips WHERE id=?", (nxt,))
    assert nxt != first and r["t_start"] >= 312.0                              # lead-in doesn't re-store footage
    man = ctx.clips.create_clip(live, 250, 300, "manual_save", None, t_event=290) # manual saves are always honoured
    assert man not in (first, nxt)


def test_game_callouts_repetition_is_not_hype_but_reaction_repetition_is():
    """Real R6 clip: 'up metal, up metal, up metal, up metal' (comms) scored live speech 0.75."""
    from liveclip.ai.scoring import live_speech_raw

    def ws(phrase):
        return [{"text": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate(phrase.split())]
    callouts = live_speech_raw(ws("Yeah, up metal, up metal, up metal, up metal, swinging, swinging"))[0]
    reaction = live_speech_raw(ws("No, he didn't he didn't he didn't he didn't Oh, thank god. Yes."))[0]
    assert callouts < 0.3 and reaction >= 0.6


def test_hook_never_ends_mid_sentence():
    from liveclip.ai.scoring import title_suggestions
    words = [{"text": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in
             enumerate("For a game as important as GTA, they will need a huge list.".split())]
    t = title_suggestions("x", words, "", None)
    assert not t or not t[0].endswith(",")


# ---------------------------------------------------------------- xqc reaction-content false clip (2026-11-18)
def test_single_bang_is_not_audio_excitement_but_a_sustained_yell_is():
    """Real xqc clip 'The suspects are now transported to the jail': steady talking with
    peaks around -20 dB, then ONE 0.5 s hit at -11 dB (a thud in the bodycam video he was
    watching) scored audio 0.90 and produced a clip with nothing in it."""
    from liveclip.detection.audio import AudioAnalyzer

    def talk(a, seconds, rng):
        for _ in range(seconds):
            for k in range(5):                           # syllables: 0.2 s loud, 0.2 s quiet
                amp = 0.1 * (1.0 + 0.2 * rng.standard_normal()) if k % 2 == 0 else 0.015
                t = np.arange(int(0.2 * SR)) / SR
                a.feed((np.sin(2 * np.pi * 220 * t) * amp * 32767).astype(np.int16).tobytes())

    def burst(a, seconds, amp=0.45):
        t = np.arange(int(seconds * SR)) / SR
        a.feed((np.sin(2 * np.pi * 300 * t) * amp * 32767).astype(np.int16).tobytes())

    rng = np.random.default_rng(1)
    bang = AudioAnalyzer(); bang.start_run(0)
    talk(bang, 90, rng); burst(bang, 0.5); talk(bang, 1, rng)
    e_bang = bang.excitement(window=5)[0]
    yell = AudioAnalyzer(); yell.start_run(0)
    talk(yell, 90, rng); burst(yell, 2.5)
    e_yell = yell.excitement(window=5)[0]
    assert e_bang < 0.6, f"a lone bang scored {e_bang:.2f}"
    assert 0.85 * e_bang < AppConfig().detection.trigger_threshold    # can't trigger on its own
    assert e_yell > 0.7, f"a real 2.5 s yell scored only {e_yell:.2f}"


def test_audio_only_moment_with_busy_but_quiet_chat_is_not_saved_like_real_xqc_clip():
    """Real numbers from that clip: audio 0.90 but chat 3.2 msg/s vs xqc's normal 6/s
    (z -1.06). The quiet chat was thrown away as 'no evidence' and the clip scored 0.60."""
    from liveclip.ai import scoring
    w = AppConfig().detection.weights.model_dump()
    s = {"chat": 0.091, "audio": 0.899, "speech": None, "transcript": 0.273, "laughter": 0.136, "novelty": 0.811}
    scoring.drop_quiet_partner(s, chat_info={"baseline": 6.0, "z": -1.06, "rate": 3.17})
    final = scoring.weighted(s, w)
    assert s["chat"] == 0.091
    assert AppConfig().clips.maybe_threshold <= final < AppConfig().detection.save_threshold   # Maybe pool, not review


def test_real_chat_spike_during_reaction_content_is_still_kept():
    """Same stream, real moment: chat spammed 'Stare' + LOL (13/s vs 3.6/s normal, z 4.5)."""
    from liveclip.ai import scoring
    w = AppConfig().detection.weights.model_dump()
    s = {"chat": 0.72, "audio": 0.878, "speech": None, "transcript": 0.641, "laughter": 0.097, "novelty": 1.0}
    scoring.drop_quiet_partner(s, chat_info={"baseline": 3.6, "z": 4.53, "rate": 13.0})
    assert scoring.weighted(s, w) >= AppConfig().detection.save_threshold


def test_slow_chat_channel_stays_no_evidence_for_a_loud_reaction():
    """Small channels (chat well under 1 msg/s) can't be expected to react: keep the old rule."""
    from liveclip.ai import scoring
    s = {"chat": 0.05, "audio": 0.9, "transcript": 0.4, "laughter": 0.3, "novelty": 0.8}
    scoring.drop_quiet_partner(s, chat_info={"baseline": 0.3, "z": -0.2})
    assert s["chat"] is None


# real xqc session 2026-11-18, 8:26-8:56: (media_t, audio excitement 5 s window, chat score, chat z)
XQC_BUILD = [(826, 0.0, 0.07, 1.1), (827, 0.0, 0.1, 1.4), (828, 0.0, 0.05, 1.0), (829, 0.0, 0.08, 1.2), (830, 0.0, 0.08, 1.1),
             (831, 0.0, 0.17, 1.8), (832, 0.0, 0.1, 1.3), (833, 0.0, 0.14, 1.5), (834, 0.02, 0.23, 2.2), (835, 0.02, 0.11, 1.1),
             (836, 0.27, 0.15, 1.4), (837, 0.37, 0.19, 1.7), (838, 0.37, 0.18, 1.6), (839, 0.37, 0.2, 1.8), (840, 0.47, 0.26, 2.3),
             (841, 0.47, 0.25, 2.2), (842, 0.47, 0.16, 1.6), (843, 0.47, 0.1, 1.2), (844, 0.47, 0.12, 1.3), (845, 0.47, 0.08, 0.2),
             (846, 0.38, 0.07, -0.6), (847, 0.38, 0.06, 0.1), (848, 0.39, 0.05, 0.0), (849, 0.45, 0.06, 0.1), (850, 0.45, 0.05, 0.1),
             (851, 0.45, 0.05, 0.3), (852, 0.45, 0.04, 0.9), (853, 0.45, 0.04, 0.5), (854, 0.45, 0.04, 0.5), (855, 0.31, 0.05, 1.1)]


def _run_detector(rows):
    from liveclip.detection.hype import HypeDetector, HypeParams
    fired = []
    d = AppConfig().detection
    det = HypeDetector(HypeParams(trigger=d.trigger_threshold, release=d.release_threshold,
                                  single_signal_factor=d.single_signal_factor), lambda: "h",
                       lambda c, a, b: fired.append(c), lambda *a: None, lambda *a: None)
    for t, a, cs, z in rows:
        for half in (0.0, 0.5):                       # the live loop ticks every 0.5 s
            det.tick(t + half, a, t + half, cs, 600, extra={"chat": {"z": z}})
    return fired


def test_sustained_loud_stretch_with_engaged_chat_triggers_like_real_xqc_miss():
    """15 s of loud audio (+9 dB) while chat ran 15 msg/s (z 2.3) never crossed the instant
    trigger (combined ~0.40 < 0.62) and was missed."""
    fired = _run_detector(XQC_BUILD)
    assert len(fired) == 1 and "sustained" in fired[0].signals
    assert 836 <= fired[0].t_event <= 852


def test_sustained_rule_needs_chat_not_just_a_long_loud_video():
    """Same loudness with chat at its normal pace (a loud stretch of the watched video) must not trigger."""
    flat = [(t, a, cs, 0.3) for t, a, cs, _ in XQC_BUILD]
    assert _run_detector(flat) == []
    no_chat = _run_detector([(t, a, None, None) for t, a, _, _ in XQC_BUILD])
    assert no_chat == []


def test_compressed_loud_streamer_yell_still_counts_like_real_kai_cenat():
    """Real Kai Cenat session: sample peaks at 0 dBFS most of the time (limited audio), loudness
    range ~7 dB (rms p50 -19, p99 -12). Audio excitement never passed 0.28 in 11 minutes, so his
    yelling could never trigger. Loud streams are now scored against their own range."""
    from liveclip.detection.audio import AudioAnalyzer

    def voice(seconds, amp, f=220):
        # speech-level tone with brief full-scale transients: sample peaks hit 0 dBFS like a limited mic
        t = np.arange(int(seconds * SR)) / SR
        x = np.sin(2 * np.pi * f * t) * amp
        x[:: SR // 20] = 1.0
        return (np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes()

    def talk(a, seconds, rng):
        for _ in range(seconds):
            for k in range(5):            # syllables ~-19 dB rms, softer gaps; peaks always at the ceiling
                a.feed(voice(0.2, 0.15 * (1 + 0.15 * rng.standard_normal())) if k % 2 == 0 else voice(0.2, 0.05))

    rng = np.random.default_rng(7)
    a = AudioAnalyzer(); a.start_run(0)
    talk(a, 120, rng)
    assert a.compressed()
    assert a.excitement(window=2)[0] < 0.3, "normal (limited) talking must stay calm"
    a.feed(voice(2.5, 0.35))              # sustained yell, ~+7 dB (Kai's p50 -> p99 range)
    e = a.excitement(window=3)[0]
    assert e >= 0.75, f"a sustained yell on a limited mic scored only {e:.2f}"
    b = AudioAnalyzer(); b.start_run(0)
    talk(b, 120, rng); b.feed(voice(0.4, 0.35)); talk(b, 1, rng)
    assert 0.85 * b.excitement(window=5)[0] < AppConfig().detection.trigger_threshold   # a short burst alone can't trigger


def test_normal_dynamic_streamer_is_not_treated_as_compressed():
    from liveclip.detection.audio import AudioAnalyzer
    a = AudioAnalyzer(); a.start_run(0)
    t = np.arange(SR) / SR
    for _ in range(60):
        a.feed((np.sin(2 * np.pi * 220 * t) * 0.1 * 32767).astype(np.int16).tobytes())
    assert not a.compressed()
