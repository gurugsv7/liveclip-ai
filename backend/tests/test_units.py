"""Fast unit tests (no ffmpeg needed)."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import numpy as np
import pytest

from liveclip.ai import scoring
from liveclip.ai.semantic import SemanticScorer
from liveclip.config import AppConfig, Paths
from liveclip.db import Database
from liveclip.detection.audio import SR, AudioAnalyzer
from liveclip.detection.chat import ChatAnalyzer
from liveclip.detection.hype import HypeDetector, HypeParams
from liveclip.editing.captions import build_ass, clean_words, map_words, paginate, STYLES
from liveclip.editing.framing import crop_path, x_expression
from liveclip.events import EventHub
from liveclip.jobs import JobRunner
from liveclip.platforms import SourceError, resolve_source
from liveclip.platforms.chat import parse_twitch_privmsg
from liveclip.storage.retention import RetentionManager


# ---------------------------------------------------------------- URL safety
@pytest.mark.parametrize("url", [
    "file:///C:/Windows/system32/config", "http://www.twitch.tv/x", "https://192.168.1.10/live", "https://localhost/a",
    "https://evil.com/twitch.tv", "https://user:pw@twitch.tv/x", "https://www.twitch.tv:8443/x", "local:fixture_a",
    "https://www.twitch.tv/../../etc", "",
])
def test_rejects_dangerous_urls(url):
    with pytest.raises(SourceError):
        resolve_source(url, allow_local=False)


def test_accepts_platform_urls(tmp_path):
    assert resolve_source("https://www.twitch.tv/Some_Streamer").channel == "some_streamer"
    yt = resolve_source("https://youtu.be/dQw4w9WgXcQ")
    assert yt.platform == "youtube" and yt.channel == "dQw4w9WgXcQ"
    assert resolve_source("https://www.youtube.com/@handle").url.endswith("/@handle/live")
    assert resolve_source("https://kick.com/abc").platform == "kick"


def test_local_fixture_is_confined(tmp_path):
    (tmp_path / "ok.mp4").write_bytes(b"x")
    assert resolve_source("local:ok", allow_local=True, fixtures_dir=tmp_path).local_path.endswith("ok.mp4")
    for bad in ("local:../ok", "local:..\\ok", "local:C:/x"):
        with pytest.raises(SourceError):
            resolve_source(bad, allow_local=True, fixtures_dir=tmp_path)


# ---------------------------------------------------------------- chat isolation
def test_twitch_parser_rejects_other_channels():
    line = "@badge-info=;display-name=Bob;emotes=25:0-4,6-10 :bob!bob@bob.tmi.twitch.tv PRIVMSG #speed :Kappa Kappa lol"
    assert parse_twitch_privmsg(line, "speed") == ("Bob", "Kappa Kappa lol", 2)
    assert parse_twitch_privmsg(line, "marlon") is None


def test_chat_analyzers_are_independent_and_baseline_normalized():
    cfg = AppConfig()
    a = ChatAnalyzer(cfg.detection.keywords, cfg.detection.laugh_tokens)
    b = ChatAnalyzer(cfg.detection.keywords, cfg.detection.laugh_tokens)
    now = time.time()
    # A is a busy chat (5 msg/s baseline), B is quiet (0.5 msg/s)
    for s in range(120):
        for k in range(5):
            a.add("u", "hello there", 0, now - 130 + s + k * 0.2)
        if s % 2 == 0:
            b.add("u", "hi", 0, now - 130 + s)
    # same absolute burst (8 msg/s of LMAO) in both
    for k in range(48):
        a.add("u", "LMAO KEKW", 1, now - 6 + k * 0.125)
        b.add("u", "LMAO KEKW", 1, now - 6 + k * 0.125)
    sa, sb = a.score(now), b.score(now)
    assert sb["score"] > sa["score"], "same burst must be more exciting for the normally quiet chat"
    assert len(a.window(0, now + 1)) != len(b.window(0, now + 1))


# ---------------------------------------------------------------- audio baseline
def _tone(seconds: float, amp: float, f: float = 440) -> bytes:
    t = np.arange(int(seconds * SR)) / SR
    return (np.sin(2 * np.pi * f * t) * amp * 32767).astype(np.int16).tobytes()


def test_loud_streamer_baseline_does_not_trigger_but_spike_does():
    loud = AudioAnalyzer()
    loud.start_run(0)
    for _ in range(60):
        loud.feed(_tone(1, 0.5))           # constantly loud streamer
    e_loud, _ = loud.excitement(window=2)
    assert e_loud < 0.2
    quiet = AudioAnalyzer()
    quiet.start_run(0)
    for _ in range(60):
        quiet.feed(_tone(1, 0.02))
    quiet.feed(_tone(1, 0.002))
    quiet.feed(_tone(2, 0.6))              # silence then scream
    e, peak_t = quiet.excitement(window=3)
    assert e > 0.7
    assert 61.0 <= peak_t <= 63.1


def test_audio_ring_returns_requested_window():
    a = AudioAnalyzer(ring_seconds=10)
    a.start_run(100.0)                     # media offset of this capture run
    a.feed(_tone(4, 0.1))
    pcm = a.get_pcm(101.0, 102.5)
    assert pcm is not None and len(pcm) == int(1.5 * SR)
    assert a.get_pcm(90, 95) is None


# ---------------------------------------------------------------- hype state machine
def _detector(events, **kw):
    p = HypeParams(min_baseline_s=0, **kw)
    n = iter(range(1000))
    return HypeDetector(p, new_id=lambda: f"c{next(n)}",
                        on_trigger=lambda c, a, b: events.append(("trigger", c.id, a, b)),
                        on_extend=lambda c, a, b: events.append(("extend", c.id)),
                        on_finalize=lambda c, s, e: events.append(("final", c.id, s, e)))


def test_duplicate_triggers_merge_into_one_clip():
    ev = []
    d = _detector(ev)
    t = 100.0
    for i in range(30):                    # 15 s of repeated screaming/chat spikes
        d.tick(t, 0.9 if i % 3 else 0.5, t, 0.9, 999)
        t += 0.5
    for _ in range(20):                    # cool down
        d.tick(t, 0.1, t, 0.1, 999)
        t += 0.5
    finals = [e for e in ev if e[0] == "final"]
    triggers = [e for e in ev if e[0] == "trigger"]
    assert len(triggers) == 1 and len(finals) == 1
    _, _, s, e = finals[0]
    assert e - s <= 90.0 + 1e-6
    assert s <= 100.0 - 25                 # pre-event context retained


def test_cooldown_suppresses_near_duplicate_and_new_event_later_allowed():
    ev = []
    d = _detector(ev, cooldown_s=20)
    d.tick(100, 0.95, 100, 0.95, 999)
    for t in np.arange(100.5, 110, 0.5):
        d.tick(t, 0.1, t, 0.1, 999)
    assert sum(1 for e in ev if e[0] == "final") == 1
    d.tick(125, 0.95, 125, 0.95, 999)      # within cooldown after clip end (~120)
    assert d.suppressed == 1
    d.tick(200, 0.95, 200, 0.95, 999)
    assert sum(1 for e in ev if e[0] == "trigger") == 2


def test_simultaneous_events_on_two_streams_are_independent():
    ea, eb = [], []
    da, db = _detector(ea), _detector(eb)
    for t in np.arange(50, 52, 0.5):
        da.tick(t, 0.95, 50.5, 0.9, 999)
        db.tick(t, 0.95, 51.0, 0.9, 999)
    for t in np.arange(52, 70, 0.5):
        da.tick(t, 0.05, t, 0.05, 999)
        db.tick(t, 0.05, t, 0.05, 999)
    assert len([e for e in ea if e[0] == "final"]) == 1
    assert len([e for e in eb if e[0] == "final"]) == 1


def test_no_trigger_before_baseline_and_when_disabled():
    ev = []
    d = HypeDetector(HypeParams(min_baseline_s=45), lambda: "x", lambda *a: ev.append(a), lambda *a: None, lambda *a: None)
    d.tick(10, 1.0, 10, 1.0, baseline_s=10)
    assert not ev
    d.enabled = False
    d.tick(100, 1.0, 100, 1.0, baseline_s=100)
    assert not ev


def test_audio_only_mode_when_chat_unavailable_is_stricter():
    d = _detector([])
    assert d.combine(0.7, None) < 0.7


# ---------------------------------------------------------------- retention
def _retention(tmp_path, **buf):
    paths = Paths(tmp_path)
    paths.ensure()
    db = Database(paths.db_file)
    cfg = AppConfig.model_validate({"buffer": buf} if buf else {})
    hub = EventHub()
    return db, RetentionManager(db, lambda: cfg, paths, hub), paths, cfg


def _seed_session(db, paths, sid="ses_aaaaaaaaaaaa", streamer="str_a", n=40, seg=5.0, size=1000):
    if not db.one("SELECT 1 FROM streamers WHERE id=?", (streamer,)):
        db.insert("streamers", {"id": streamer, "slug": streamer, "name": streamer, "platform": "local", "url": "local:x",
                                "created_at": time.time()})
    sdir = paths.session_dir(streamer, sid)
    segdir = sdir / "segments"
    segdir.mkdir(parents=True, exist_ok=True)
    db.insert("stream_sessions", {"id": sid, "streamer_id": streamer, "platform": "local", "url": "x", "state": "live",
                                  "started_at": time.time(), "dir": str(sdir)})
    for i in range(n):
        p = segdir / f"seg_{i:06d}.ts"
        p.write_bytes(b"\0" * size)
        db.insert("video_segments", {"session_id": sid, "streamer_id": streamer, "seq": i, "run": 1, "path": str(p),
                                     "t_start": i * seg, "t_end": (i + 1) * seg, "duration": seg, "size": size,
                                     "created_at": time.time() - (n - i)})
    # an in-progress segment that FFmpeg has not finished (not indexed)
    (segdir / f"seg_{n:06d}.ts").write_bytes(b"partial")
    return segdir


def test_rolling_buffer_deletes_old_unprotected_and_keeps_protected(tmp_path):
    db, ret, paths, cfg = _retention(tmp_path, retention_seconds=120)
    segdir = _seed_session(db, paths)
    ret.active_sessions["ses_aaaaaaaaaaaa"] = {"next_seq": lambda: 40}
    pid = ret.protect("ses_aaaaaaaaaaaa", "str_a", 20, 40, "manual")       # segments 4..7
    ret.sweep()
    alive = db.all("SELECT seq FROM video_segments WHERE deleted=0 ORDER BY seq")
    seqs = [r["seq"] for r in alive]
    # newest 120 s (24 segments ending at t=200) + protected 4..7
    assert seqs == [4, 5, 6, 7] + list(range(16, 40))
    assert (segdir / "seg_000005.ts").exists() and not (segdir / "seg_000001.ts").exists()
    assert (segdir / "seg_000040.ts").exists(), "segment being written must never be deleted"
    ret.release(pid)
    ret.sweep()
    assert db.scalar("SELECT COUNT(*) FROM video_segments WHERE deleted=0") == 24


def test_concurrent_protections_and_manual_delete_buffer(tmp_path):
    db, ret, paths, _ = _retention(tmp_path)
    _seed_session(db, paths)
    ret.active_sessions["ses_aaaaaaaaaaaa"] = {"next_seq": lambda: 40}
    import threading

    ids = []
    th = [threading.Thread(target=lambda a=a: ids.append(ret.protect("ses_aaaaaaaaaaaa", "str_a", a, a + 30, "save")))
          for a in (100, 140, 170)]
    [t.start() for t in th]
    [t.join() for t in th]
    sweeper = threading.Thread(target=ret.sweep)
    sweeper.start()
    sweeper.join()
    r = ret.delete_unprotected("ses_aaaaaaaaaaaa")
    left = [x["seq"] for x in db.all("SELECT seq FROM video_segments WHERE deleted=0 ORDER BY seq")]
    for a in (100, 140, 170):
        for s in range(int(a // 5), int((a + 30) // 5)):
            if s < 40:
                assert s in left, f"protected segment {s} was deleted"
    assert 39 in left, "newest segment kept for preview continuity"
    assert r["protected_kept"] > 0


def test_temp_budget_enforced_but_protected_survives(tmp_path):
    db, ret, paths, _ = _retention(tmp_path, max_temp_bytes=256 * 1024 ** 2)
    _seed_session(db, paths, n=40, size=10 * 1024 ** 2)                     # 400 MB, all inside window when seg=1s
    db.execute("UPDATE video_segments SET t_start=seq, t_end=seq+1")
    ret.active_sessions["ses_aaaaaaaaaaaa"] = {"next_seq": lambda: 40}
    ret.protect("ses_aaaaaaaaaaaa", "str_a", 0, 5, "manual")
    st = ret.sweep()
    total = db.scalar("SELECT SUM(size) FROM video_segments WHERE deleted=0")
    assert total <= 256 * 1024 ** 2
    assert st["budget_deleted"] > 0
    assert db.scalar("SELECT COUNT(*) FROM video_segments WHERE deleted=0 AND seq<5") == 5
    assert "temp_budget" in ret.hub.warnings


def test_crash_recovery_marks_state_consistent(tmp_path):
    db, ret, paths, _ = _retention(tmp_path)
    segdir = _seed_session(db, paths, n=10)
    (segdir / "seg_000003.ts").unlink()                                        # lost during crash
    orphan = segdir / "seg_000099.ts"
    orphan.write_bytes(b"x")
    rec = ret.recover_on_startup()
    assert rec["sessions_closed"] == 1 and rec["missing_segments"] == 1
    assert not orphan.exists()
    assert db.scalar("SELECT state FROM stream_sessions") == "ended"


# ---------------------------------------------------------------- jobs
def test_job_failures_retry_then_fail_and_recovery_requeues(tmp_path):
    db = Database(tmp_path / "j.db")
    hub = EventHub()

    async def scenario():
        hub.bind(asyncio.get_running_loop())
        jr = JobRunner(db, hub, max_attempts=2)
        calls = {"n": 0}
        failed = []

        async def bad(job):
            calls["n"] += 1
            raise RuntimeError("model crashed")

        async def on_fail(job):
            failed.append(job["ref_id"])

        jr.register("transcribe", bad)
        jr.register("transcribe:failed", on_fail)
        jid = jr.enqueue("transcribe", "clip_1")
        assert jr.enqueue("transcribe", "clip_1") == jid, "duplicate enqueue is idempotent"
        jr.not_before.clear()
        jr.start()
        for _ in range(100):
            await asyncio.sleep(0.1)
            jr.not_before.clear()
            if db.scalar("SELECT state FROM processing_jobs WHERE id=?", (jid,)) == "failed":
                break
        await jr.stop()
        return calls["n"], failed, jid

    n, failed, jid = asyncio.run(scenario())
    assert n == 2 and failed == ["clip_1"]
    db.execute("INSERT INTO processing_jobs(id,kind,ref_id,priority,state,created_at) VALUES('j2','extract','c2',1,'running',?)",
               (time.time(),))
    jr = JobRunner(db, hub)
    assert jr.recover() == 1
    assert db.scalar("SELECT state FROM processing_jobs WHERE id='j2'") == "queued"


# ---------------------------------------------------------------- captions & framing
WORDS = [{"text": "um", "start": 0.0, "end": 0.2}, {"text": "No", "start": 0.3, "end": 0.5},
         {"text": "way", "start": 0.5, "end": 0.8}, {"text": "bro!", "start": 0.8, "end": 1.2},
         {"text": "he", "start": 2.0, "end": 2.1}, {"text": "actually", "start": 2.1, "end": 2.5},
         {"text": "did", "start": 2.5, "end": 2.7}, {"text": "it", "start": 2.7, "end": 2.9}]


def test_captions_drop_fillers_and_page_at_punctuation():
    w = clean_words(WORDS)
    assert w[0]["text"] == "No"
    pages = paginate(w, STYLES["bold_pop"])
    assert [p[-1]["text"] for p in pages][0] == "bro!"
    assert all(len(p) <= 3 for p in pages)
    ass = build_ass(WORDS, hook="Speed can't believe it", duration=3.0)
    assert "Style: Cap" in ass and "Hook" in ass and "\\pos(540,1267)" in ass
    assert "UM" not in ass                                              # filler removed, nothing invented
    assert ass.count("Dialogue: 1") == 7                               # one event per spoken word


def test_word_times_follow_cold_open_pieces():
    out = map_words(WORDS, [(2.0, 3.0), (0.0, 3.0)])
    assert out[0]["text"] == "he" and out[0]["start"] == 0.0           # cold open first
    assert any(w["text"] == "No" and abs(w["start"] - 1.3) < 1e-6 for w in out)


def test_smart_crop_follows_motion_and_is_stable():
    frames = np.zeros((20, 108, 192), np.float32)
    for i in range(20):
        frames[i, 40:70, 150 + (i % 2) * 4:170 + (i % 2) * 4] = 255  # jittering object on the right
    keys = crop_path(frames, 1920, 1080, 608)
    assert keys[-1][1] > 1920 / 2 - 608 / 2                             # moved right
    assert len(keys) <= 3                                                # jitter ignored (dead zone)
    expr = x_expression(keys)
    assert expr.count("(") == expr.count(")")


# ---------------------------------------------------------------- scoring & unavailable services
def test_scoring_reweights_missing_signals():
    w = AppConfig().detection.weights.model_dump()
    s = scoring.weighted({"chat": None, "audio": 0.8, "transcript": 0.6, "laughter": None, "novelty": 1.0}, w)
    assert 0.6 < s < 0.9
    titles = scoring.title_suggestions("Speed", WORDS, "No way bro! he actually did it", 0.5)
    assert titles and not any(t.startswith("Speed") for t in titles)       # on-screen hook: no name prefix
    assert titles[0].lower().startswith("no way")                          # the reaction line wins


def test_ollama_unreachable_and_invalid_json_are_handled(monkeypatch):
    cfg = AppConfig.model_validate({"ai": {"ollama_enabled": True, "ollama_url": "http://127.0.0.1:9"}})
    sc = SemanticScorer(lambda: cfg)
    res = asyncio.run(sc.score(streamer="x", duration=30, peak=10, words=WORDS, chat=[], audio=0.5, chat_score=0.5))
    assert res is None and sc.status == "error"
    st = asyncio.run(sc.check())
    assert st["status"] == "unavailable"

    import httpx

    class FakeResp:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"response": json.dumps({"score": 7, "category": "punchline", "reason": "good",
                                            "start": 50, "end": 10, "titles": ["A", 3]})}

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    res = asyncio.run(sc.score(streamer="x", duration=30, peak=10, words=WORDS, chat=[], audio=0.5, chat_score=0.5))
    assert res is not None and res.score == 0.7 and res.start is None and res.titles == ["A"]


def test_ollama_url_must_be_local():
    with pytest.raises(Exception):
        AppConfig.model_validate({"ai": {"ollama_url": "http://example.com:11434"}})


def test_transcriber_reports_unavailable_without_crashing(tmp_path, monkeypatch):
    from liveclip.ai import transcriber as tr

    monkeypatch.setattr(tr.Transcriber, "installed", staticmethod(lambda: False))
    t = tr.Transcriber(lambda: AppConfig(), tmp_path)
    with pytest.raises(tr.TranscriptionUnavailable):
        t.transcribe("x.wav", streamer_id="a", session_id="b", clip_id="c")
    assert t.info()["status"] == "unavailable"


def test_strong_chat_alone_triggers_like_real_jynxzi_quiz_moment():
    """Regression from a real session: chat hit 0.81 while the streamer's voice
    stayed calm (audio 0.23). Combined-only scoring gave 0.51 and missed it."""
    ev = []
    d = _detector(ev)
    assert d.combine(0.23, 0.81) >= 0.62
    d.tick(93, 0.23, 92, 0.81, 999)
    assert [e[0] for e in ev] == ["trigger"]
    # ordinary chat (90th percentile in that session: 0.34) must not trigger alone
    assert d.combine(0.2, 0.34) < 0.62
    assert d.combine(0.32, 0.5) < 0.62


def test_titles_split_on_pauses_when_whisper_gives_no_punctuation():
    words, t = [], 0.0
    for chunk in (["name", "a", "word", "that", "means", "angry"], ["leak", "check", "everything"]):
        for w in chunk:
            words.append({"text": w, "start": t, "end": t + 0.25})
            t += 0.3
        t += 1.0                                              # pause between phrases
    titles = scoring.title_suggestions("Jynxzi", words, " ".join(x["text"] for x in words), 0.5)
    # calm narration with no reaction is not a hook; it may only appear as a weak fallback
    assert titles == [] or all(not t.startswith("Jynxzi") for t in titles)


def test_calm_voice_with_exploding_chat_is_kept_like_real_session():
    """Regression from a real Jynxzi clip: chat 0.80, audio 0.009 scored 0.46 and was
    discarded. A quiet partner signal must not count as evidence against."""
    w = AppConfig().detection.weights.model_dump()
    s = {"chat": 0.804, "audio": 0.0093, "transcript": 0.397, "laughter": 0.214, "novelty": 0.914}
    assert scoring.weighted(dict(s), w) < 0.5
    scoring.drop_quiet_partner(s)
    assert s["audio"] is None and scoring.weighted(s, w) >= AppConfig().detection.save_threshold
    dull = {"chat": 0.2, "audio": 0.1, "transcript": 0.1, "laughter": 0.0, "novelty": 0.9}
    scoring.drop_quiet_partner(dull)
    assert dull["audio"] == 0.1 and scoring.weighted(dull, w) < 0.5    # boring moments still discarded


def test_silence_before_talking_is_not_treated_as_baseline_like_real_session():
    """Regression from a real session: 270 s of digital silence (muted/BRB), then
    normal talking at about -34 dB. The silence-dominated baseline made every second
    look like max excitement and produced 3 back-to-back false clips."""
    a = AudioAnalyzer()
    a.start_run(0)
    for _ in range(270):
        a.feed(b"\0\0" * SR)                                 # -120 dB
    excs = []
    for _ in range(150):                                     # normal talking resumes
        a.feed(_tone(1, 0.02))
        excs.append(a.excitement(window=2)[0])
    assert max(excs) < 0.2, "returning from a silent break must not look like hype"
    assert a.voiced_seconds == 150 and a.has_baseline(45)
    a.feed(_tone(2, 0.6))                                    # a real scream on top of talking
    assert a.excitement(window=3)[0] > 0.7


def test_brb_silence_then_quiet_talk_gets_no_silence_bonus():
    a = AudioAnalyzer()
    a.start_run(0)
    for _ in range(60):
        a.feed(_tone(1, 0.02))
    for _ in range(60):
        a.feed(b"\0\0" * SR)                                 # one-minute break
    a.feed(_tone(1, 0.03))                                   # back, slightly louder than usual
    assert a.excitement(window=2)[0] < 0.3


def _words(text: str, t0: float = 0.0, step: float = 0.3) -> list[dict]:
    return [{"text": w, "start": round(t0 + i * step, 2), "end": round(t0 + i * step + 0.25, 2)} for i, w in enumerate(text.split())]


def test_live_speech_scores_real_jynxzi_lines():
    """Text from real session clips: the chess win the AI missed vs. a calm quiz segment."""
    win = _words("No, he didn't he didn't he didn't he didn't Oh, thank god. Yes. Oh my god. I win material there now Yes, yes, let's fucking go bro")
    calm = _words("Name a word that means angry frustrated mad seething Leak check everything name a variety of tomato")
    s_win, peak, hits = scoring.live_speech_raw(win)
    s_calm, _, _ = scoring.live_speech_raw(calm)
    assert s_win >= 0.8 and any("god" in h for h in hits) and peak is not None
    assert s_calm < 0.2


def test_speech_normalizer_discounts_a_streamers_habits():
    n = scoring.SpeechNormalizer()
    for _ in range(20):
        n(0.4)                                   # swears / "oh my god" all the time
    assert n(0.4) == 0.2                         # their normal is no longer hype
    assert n(1.0) == 0.8                         # a genuine outburst still stands out


def test_excited_words_alone_trigger_at_the_phrase_time():
    ev = []
    d = _detector(ev)
    assert d.combine(0.3, 0.2, speech=0.3) < 0.62            # ordinary chatter
    d.tick(60, 0.3, 59, 0.2, 999, speech=0.9, speech_peak_t=55.0)
    assert ev and ev[0][0] == "trigger"
    assert d.open.t_event == 55.0 and d.open.speech == 0.9


def _timed(spec: list[tuple[float, str]]) -> list[dict]:
    out = []
    for t, phrase in spec:
        for i, w in enumerate(phrase.split()):
            out.append({"text": w, "start": round(t + i * 0.3, 2), "end": round(t + i * 0.3 + 0.25, 2)})
    return out


def test_reels_are_cut_to_30s_best_moment_with_setup_like_real_magnus_clip():
    """House rule: reels <= 30 s. Modelled on a real 69 s Jynxzi clip: setup at the
    start, a long calm middle, then the payoff."""
    from liveclip.editing.highlight import best_reel_cut

    words = _timed([
        (0.0, "I already lost. Literally just GG. I don't know what the fuck."),
        (8.0, "Literally instantly GG."),
        (24.0, "Yeah, he's gonna take back. That's fine"),
        (31.5, "So this is interesting because I actually have this on the board."),
        (37.0, "He can't take because queen takes bro."),
        (41.3, "Oh my god. I actually he's dead. He lost I actually already won"),
        (47.0, "how do I see this am I actually Magnus Carlson like legitimately how the fuck do I see this oh my god"),
        (58.0, "I can't because he didn't move his alright now he's dead because"),
    ])
    pieces = best_reel_cut(words, 69.0, peak=42.0, max_s=30.0)
    total = sum(b - a for a, b in pieces)
    assert total <= 30.0 and total >= 12.0
    assert any(a <= 42.0 <= b for a, b in pieces), "the hype peak must be in the reel"
    assert pieces[0][1] < 15.0, "setup line from the start is spliced in front"
    assert pieces[-1][1] < 58.0, "reel ends on the payoff, not trailing filler"
    assert all(b2 > a2 for a2, b2 in pieces) and all(p[1] <= q[0] for p, q in zip(pieces, pieces[1:]))


def test_reel_cut_without_speech_centres_on_peak_and_respects_limit():
    from liveclip.editing.highlight import best_reel_cut

    pieces = best_reel_cut([], 90.0, peak=60.0, max_s=30.0)
    assert len(pieces) == 1 and pieces[0][1] - pieces[0][0] <= 30.0 and pieces[0][0] <= 60.0 <= pieces[0][1]


# ---------------------------------------------------------------- macOS support (same behaviour, other platform pieces)
def test_encoder_auto_prefers_nvenc_then_videotoolbox_then_x264(monkeypatch):
    from liveclip.editing import render as r

    monkeypatch.setattr(r, "nvenc_available", lambda ff: True)
    monkeypatch.setattr(r, "videotoolbox_available", lambda ff: True)
    assert r.pick_encoder("auto", "ffmpeg") == "nvenc"                     # Windows laptop: unchanged
    monkeypatch.setattr(r, "nvenc_available", lambda ff: False)
    assert r.pick_encoder("auto", "ffmpeg") == "videotoolbox"              # MacBook
    monkeypatch.setattr(r, "videotoolbox_available", lambda ff: False)
    assert r.pick_encoder("auto", "ffmpeg") == "x264"
    spec = r.RenderSpec(src="x", src_w=1920, src_h=1080, src_fps=60, duration=30, out="o.mp4", encoder="videotoolbox")
    args = r._encoder_args(spec, "ffmpeg")
    assert args[:2] == ["-c:v", "h264_videotoolbox"] and "yuv420p" in args


def test_posix_process_flags_lower_render_priority(monkeypatch):
    from liveclip import media

    monkeypatch.setattr(media.os, "name", "posix")
    low, normal = media.popen_flags("low"), media.popen_flags("normal")
    assert low["start_new_session"] and callable(low["preexec_fn"])       # renders are niced
    assert normal == {"start_new_session": True}                           # capture is not slowed down


# ---------------------------------------------------------------- cover + post caption
def test_cover_scoring_prefers_a_sharp_face_frame_near_the_peak_without_half_captions():
    import cv2
    from liveclip.editing.cover import SH, SW, score_frames
    rng = np.random.default_rng(3)
    sharp = (rng.random((SH, SW, 3)) * 255).astype(np.uint8)
    blurry = cv2.GaussianBlur(sharp, (31, 31), 0)
    black = np.zeros((SH, SW, 3), np.uint8)
    frames = np.stack([black, blurry, sharp, sharp, sharp])
    times = [0.1, 5.0, 10.0, 12.0, 20.0]
    words = [{"text": "no", "start": 9.8, "end": 10.2}]            # a caption word is on screen at 10.0
    s = score_frames(frames, times, 25.0, peak=12.0, words=words, cuts=[20.0])
    assert int(np.argmax(s)) == 3                                    # sharp, at the peak, no caption, not a cut
    assert s[0] < 0 and s[1] < s[3] and s[2] < s[3] and s[4] < s[3]


def test_post_caption_uses_only_real_words_credit_and_few_hashtags():
    from liveclip.editing.cover import post_caption

    def ws(t, phrase):
        return [{"text": w, "start": round(t + i * 0.3, 2), "end": round(t + i * 0.3 + 0.25, 2)} for i, w in enumerate(phrase.split())]
    words = ws(0, "Wait wait wait.") + ws(3, "No way he actually did that bro!") + ws(7, "I already lost the whole thing.")
    cap = post_caption(streamer="xQc", platform="twitch", url="https://www.twitch.tv/xqc",
                       hook="I already lost the whole thing", words=words)
    lines = cap.strip().splitlines()
    assert lines[0] == "I already lost the whole thing 😳"
    assert "“No way he actually did that bro!”" in cap                # a second real line, not the hook again
    assert "🎥 xQc live on Twitch: https://www.twitch.tv/xqc" in cap
    tags = lines[-1].split()
    assert 3 <= len(tags) <= 5 and all(t.startswith("#") and t == t.lower() for t in tags) and "#xqc" in tags
    assert sum(ch in "😂😳🔥" for ch in lines[0]) == 1


# ---------------------------------------------------------------- Tamil / Tanglish
def test_tamil_words_become_casual_tanglish():
    from liveclip.ai.tanglish import romanize_word
    cases = {"டேய்": "dey", "என்ன": "enna", "பண்ற": "panra", "செம்ம": "semma", "வேற": "vera", "ஐயோ": "aiyo",
             "கொஞ்சம்": "konjam", "வந்து": "vandhu", "அது": "adhu", "தம்பி": "thambi", "தமிழ்": "thamizh",
             "போடா": "poda", "வா": "vaa", "சரி": "sari", "விட்டு": "vittu", "வெற்றி": "vetri", "எங்க": "enga",
             "பச்சை": "pachai", "சத்தியமா": "sathiyama", "கடவுளே": "kadavule", "முடியல": "mudiyala"}
    assert {k: romanize_word(k) for k in cases} == cases


def test_tanglish_keeps_word_timings_and_english_words():
    from liveclip.ai.tanglish import romanize_words
    words = [{"text": "டேய்", "start": 1.0, "end": 1.3}, {"text": "semma", "start": 1.3, "end": 1.6},
             {"text": "game!", "start": 1.6, "end": 2.0}]
    out = romanize_words(words)
    assert [w["text"] for w in out] == ["dey", "semma", "game!"]
    assert [(w["start"], w["end"]) for w in out] == [(w["start"], w["end"]) for w in words]


def test_tamil_hype_counts_as_excited_speech_but_calm_tamil_does_not():
    from liveclip.ai import scoring
    from liveclip.ai.tanglish import romanize_words

    def ws(phrase):
        return [{"text": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate(phrase.split())]
    hype = romanize_words(ws("ஐயோ டேய் என்ன டா இது! வேற லெவல் செம்ம"))
    calm = romanize_words(ws("இப்போ நான் அந்த பக்கம் போறேன் சரி"))
    s_hype, _, hits = scoring.live_speech_raw(hype)
    s_calm, _, _ = scoring.live_speech_raw(calm)
    assert s_hype >= 0.8 and any("aiyo" in h for h in hits)
    assert s_calm < 0.2
    assert scoring.live_speech_raw(ws("ma ma ma okay"))[0] < 0.2          # "ma" is not "mass"


def test_tamil_script_captions_get_a_font_with_tamil_letters():
    from liveclip.clips.service import _tamil_font
    assert _tamil_font("Segoe UI Black", [{"text": "semma"}], "vera level") == "Segoe UI Black"
    assert _tamil_font("Segoe UI Black", [{"text": "செம்ம"}], None) in ("Nirmala UI", "Tamil Sangam MN")


def test_old_database_gets_language_columns():
    import sqlite3
    from liveclip.db import Database
    import tempfile, os
    p = os.path.join(tempfile.mkdtemp(), "old.db")
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE streamers (id TEXT PRIMARY KEY, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL, platform TEXT NOT NULL,"
                " url TEXT NOT NULL, slot INTEGER NOT NULL DEFAULT 0, priority INTEGER NOT NULL DEFAULT 2,"
                " color TEXT NOT NULL DEFAULT '#8b5cf6', ai_enabled INTEGER NOT NULL DEFAULT 1, facecam TEXT,"
                " default_layout TEXT NOT NULL DEFAULT 'smart_crop', usage_notes TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL)")
    con.commit(); con.close()
    db = Database(p)
    cols = {r[1] for r in db._conn.execute("PRAGMA table_info(streamers)")}
    assert {"language", "caption_mode", "monitoring"} <= cols
    db.close()


def test_whisper_repetition_loops_are_dropped_but_real_repeats_kept():
    """Real tamilgaming live windows: 'e, e, e, e, ...' and 'muthum muthum muthum ...' (music
    intro) came back as speech and the repetition counted as excitement."""
    from liveclip.ai.transcriber import drop_loops
    w = lambda ts: [{"text": t, "start": i * 0.2, "end": i * 0.2 + 0.15} for i, t in enumerate(ts)]
    assert drop_loops(w(["e,"] * 40)) == []
    assert [x["text"] for x in drop_loops(w(["paarkkum!"] + ["muthum"] * 25 + ["ma"]))] == ["paarkkum!", "ma"]
    assert [x["text"] for x in drop_loops(w(["yes", "yes", "yes", "let's", "go"]))] == ["yes", "yes", "yes", "let's", "go"]
    assert len(drop_loops(w(["sellungal"] * 4 + ["avvaarungal"]))) == 5


def test_facecam_layout_is_used_when_a_facecam_is_known_unless_user_chose():
    from liveclip.clips.service import effective_layout
    fc = {"x": 0.0, "y": 0.0, "w": 0.2, "h": 0.3}
    assert effective_layout("smart_crop", {}, fc) == "facecam"            # real xqc reel: face only 2 s on screen
    assert effective_layout("smart_crop", {"layout_locked": True}, fc) == "smart_crop"
    assert effective_layout("smart_crop", {}, None) == "smart_crop"
    assert effective_layout("letterbox", {}, fc) == "letterbox"
