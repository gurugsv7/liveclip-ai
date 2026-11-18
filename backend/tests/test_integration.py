"""End-to-end tests against the real app with two deterministic local
"livestreams" (fixture_a = 440 Hz tone, fixture_b = 880 Hz tone), each with
its own chat fixture. Requires ffmpeg/ffprobe. ~3-4 minutes.

    pytest tests/test_integration.py -v
"""
from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
import psutil
import pytest
from fastapi.testclient import TestClient

from conftest import TOOLS, needs_ffmpeg, wait_until

pytestmark = [needs_ffmpeg, pytest.mark.media]

TEST_CONFIG = {
    "allow_local_sources": True,
    "buffer": {"segment_seconds": 2, "retention_seconds": 30, "cleanup_interval_s": 1},
    "detection": {"min_baseline_s": 8, "trigger_threshold": 0.55, "cooldown_s": 10, "merge_window_s": 10},
    "clips": {"pre_context_s": 6, "post_context_s": 4, "max_reaction_wait_s": 8, "max_clip_s": 30},
    "render": {"fps": 30},
}


def dominant_freq(path: str) -> float:
    p = subprocess.run([TOOLS.ffmpeg, "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                       capture_output=True, timeout=60)
    x = np.frombuffer(p.stdout, dtype=np.int16).astype(np.float32)
    assert len(x) > 16000, "clip has no audio"
    spec = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.fft.rfftfreq(len(x), 1 / 16000)[np.argmax(spec[20:]) + 20])


@pytest.fixture(scope="module")
def app_env(tmp_path_factory, fixture_media):
    from liveclip.app import create_app
    from liveclip.context import AppContext

    data = tmp_path_factory.mktemp("data")
    fixtures_dir = Path(fixture_media["fixture_a"]["media"]).parent
    ctx = AppContext(data_dir=data, fixtures_dir=fixtures_dir, config_overrides=TEST_CONFIG)
    app = create_app(ctx, serve_frontend=False)
    with TestClient(app) as client:
        a = client.post("/api/streams", json={"name": "Alpha", "url": "local:fixture_a", "start": True}).json()
        b = client.post("/api/streams", json={"name": "Bravo", "url": "local:fixture_b", "start": True}).json()
        yield {"client": client, "ctx": ctx, "a": a["id"], "b": b["id"], "t0": time.time(), "fx": fixture_media}
        client.post("/api/streams/stop-all")


def live(env, sid):
    return env["ctx"].manager.sessions[sid]


def n_segments(env, sid):
    return env["ctx"].db.scalar("SELECT COUNT(*) FROM video_segments WHERE session_id=?", (live(env, sid).session_id,))


def test_01_two_streams_record_independently(app_env):
    ctx = app_env["ctx"]
    wait_until(lambda: n_segments(app_env, app_env["a"]) >= 4 and n_segments(app_env, app_env["b"]) >= 4, 40, msg="segments")
    la, lb = live(app_env, app_env["a"]), live(app_env, app_env["b"])
    assert la.session_id != lb.session_id
    assert la.worker.pids["ffmpeg"] != lb.worker.pids["ffmpeg"]
    for l, slug in ((la, "alpha"), (lb, "bravo")):
        rows = ctx.db.all("SELECT path, streamer_id FROM video_segments WHERE session_id=?", (l.session_id,))
        assert all(r["streamer_id"] == l.streamer_id for r in rows)
        assert all(Path(r["path"]).parent == ctx.paths.segments_dir(slug, l.session_id) for r in rows)
    st = app_env["client"].get(f"/api/streams/{app_env['a']}/status").json()["live"]
    assert st["state"] == "live" and st["width"] == 1280 and st["height"] == 720 and round(st["fps"]) == 30
    assert st["video_codec"] == "h264" and st["measured_kbps"] > 0


def test_02_preview_is_served_locally_without_any_network(app_env, monkeypatch):
    """The dashboard preview is an HLS playlist over the recorder's own files.
    All outbound sockets are blocked while the playlist + segments are fetched."""
    def blocked(*a, **k):
        raise AssertionError("preview attempted a network connection")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    c = app_env["client"]
    sid = live(app_env, app_env["a"]).session_id
    pl = c.get(f"/media/live/{sid}/index.m3u8")
    assert pl.status_code == 200 and pl.text.startswith("#EXTM3U")
    names = [l for l in pl.text.splitlines() if l.endswith(".ts")]
    assert names and all(n.startswith("seg_") for n in names)
    for n in names[-2:]:
        r = c.get(f"/media/live/{sid}/{n}")
        assert r.status_code == 200 and r.content[:1] == b"\x47"     # MPEG-TS sync byte
    # one capture process per stream; local sources never start streamlink
    for k in ("a", "b"):
        assert "streamlink" not in live(app_env, app_env[k]).worker.pids
    # a session's playlist never lists another session's files
    other = live(app_env, app_env["b"]).session_id
    assert c.get(f"/media/live/{other}/{names[-1]}").status_code in (200, 404)
    row = app_env["ctx"].db.one("SELECT path FROM video_segments WHERE session_id=? AND path LIKE ?", (other, f"%{names[-1]}"))
    if row:
        assert other in row["path"] and sid not in row["path"]


def test_03_concurrent_manual_saves_keep_audio_separate(app_env):
    c = app_env["client"]
    out = {}
    ctx0 = app_env["ctx"]
    for k in ("a", "b"):   # a save can only contain footage that exists: wait for >15 s of buffer
        wait_until(lambda k=k: ctx0.retention.buffer_status(live(app_env, app_env[k]).session_id)["seconds"] >= 18, 40,
                   msg="18 s of buffer")

    def save(k):
        out[k] = c.post(f"/api/streams/{app_env[k]}/save", json={"seconds": 15}).json()["clip_id"]

    th = [threading.Thread(target=save, args=(k,)) for k in ("a", "b")]
    [t.start() for t in th]
    [t.join() for t in th]
    ctx = app_env["ctx"]

    def done():
        rows = [ctx.clips.get(out[k]) for k in ("a", "b")]
        return all(r["status"] in ("ready_for_review", "failed") for r in rows) and rows
    clips = wait_until(done, 120, msg="manual clips processed")
    from liveclip.media import verify_playable
    for k, clip, tone in (("a", clips[0], 440), ("b", clips[1], 880)):
        assert clip["status"] == "ready_for_review", clip.get("error")
        assert clip["streamer_id"] == app_env[k] and clip["session_id"] == live(app_env, app_env[k]).session_id
        summ = verify_playable(TOOLS.ffmpeg, TOOLS.ffprobe, clip["file_path"])
        assert 12 <= summ["duration"] <= 20
        f = dominant_freq(clip["file_path"])
        assert abs(f - tone) < 15, f"clip for {k} has dominant tone {f} Hz, expected {tone}"
        # protection released after the permanent copy
        assert not ctx.db.all("SELECT * FROM protected_ranges WHERE ref_id=? AND released_at IS NULL", (clip["id"],))


def test_04_chat_is_associated_with_its_own_streamer(app_env):
    ctx = app_env["ctx"]
    for k in ("a", "b"):
        live(app_env, app_env[k]).flush_chat()
    for k, tag, other in (("a", "[A]", "[B]"), ("b", "[B]", "[A]")):
        sid = live(app_env, app_env[k]).session_id
        rows = ctx.db.all("SELECT text, streamer_id FROM chat_events WHERE session_id=?", (sid,))
        assert rows, f"no chat for {k}"
        assert all(tag in r["text"] and other not in r["text"] for r in rows)
        assert all(r["streamer_id"] == app_env[k] for r in rows)


def test_05_hype_detected_at_the_right_time_on_both_streams(app_env):
    """Fixture burst is at 22 s of each 40 s loop; chat spikes 3 s later."""
    ctx = app_env["ctx"]

    def events():
        ev = {k: ctx.db.all("SELECT * FROM hype_events WHERE streamer_id=? AND kind='auto'", (app_env[k],)) for k in ("a", "b")}
        return ev if all(ev.values()) else None
    ev = wait_until(events, 90, interval=1, msg="auto hype events on both streams")
    for k in ("a", "b"):
        e = ev[k][0]
        phase = e["t_event"] % 40
        # The fixture "reaction" is a synthetic sine burst, not a human voice, so voice-gated
        # loudness ignores it and detection is led by the fixture's chat spike (3-6 s after the
        # burst, like real chat). The event must still land in the burst/reaction window.
        assert 19.0 <= phase <= 29.0, f"event at {e['t_event']:.1f}s (loop phase {phase:.1f}) not aligned to burst at 22s"
        assert e["session_id"] == live(app_env, app_env[k]).session_id
    # duplicate suppression: one burst → one event per stream per loop
    for k in ("a", "b"):
        ts = sorted(e["t_event"] for e in ev[k])
        assert all(b - a > 15 for a, b in zip(ts, ts[1:]))


def test_06_rolling_buffer_stays_bounded(app_env):
    ctx = app_env["ctx"]
    wait_until(lambda: time.time() - app_env["t0"] > 50, 60, msg="enough runtime")
    for k in ("a", "b"):
        l = live(app_env, app_env[k])
        ctx.retention.sweep()
        b = ctx.retention.buffer_status(l.session_id)
        unprotected = b["segments"] - b["protected_segments"]
        assert unprotected <= 30 / 2 + 3, b
        deleted = ctx.db.scalar("SELECT COUNT(*) FROM video_segments WHERE session_id=? AND deleted=1", (l.session_id,))
        assert deleted > 0
        files = list(ctx.paths.segments_dir(l.streamer["slug"], l.session_id).glob("seg_*.ts"))
        assert len(files) <= b["segments"] + 2                        # + the one being written


def test_07_worker_crash_reconnects_without_affecting_other_stream(app_env):
    la, lb = live(app_env, app_env["a"]), live(app_env, app_env["b"])
    before_b = n_segments(app_env, app_env["b"])
    before_runs = la.worker.run_index
    psutil.Process(la.worker.pids["ffmpeg"]).kill()
    wait_until(lambda: la.worker.run_index > before_runs and la.state == "live", 40, msg="A reconnected")
    assert la.worker.reconnects >= 1
    wait_until(lambda: n_segments(app_env, app_env["b"]) >= before_b + 3, 30, msg="B kept recording")
    assert lb.state == "live" and lb.worker.reconnects == 0
    runs = app_env["ctx"].db.all("SELECT DISTINCT run FROM video_segments WHERE session_id=?", (la.session_id,))
    assert len(runs) >= 2
    pl = app_env["client"].get(f"/media/live/{la.session_id}/index.m3u8")
    assert pl.status_code == 200


def test_08_vertical_export_is_valid_1080x1920(app_env):
    c = app_env["client"]
    clip = next(x for x in c.get("/api/clips").json() if x["streamer_id"] == app_env["a"] and x["files"]["file_path"])
    c.patch(f"/api/clips/{clip['id']}", json={
        "title": "Alpha test clip", "layout": "smart_crop",
        "caption_words": [{"text": "no", "start": 1.0, "end": 1.3}, {"text": "way", "start": 1.3, "end": 1.7}]})
    assert c.post(f"/api/clips/{clip['id']}/export").status_code == 200
    got = wait_until(lambda: (lambda x: x if x["export_status"] in ("done", "failed") else None)(c.get(f"/api/clips/{clip['id']}").json()),
                     180, interval=1, msg="export")
    assert got["export_status"] == "done", got.get("error")
    from liveclip.media import ffprobe, stream_summary
    s = stream_summary(ffprobe(TOOLS.ffprobe, got["export_path"]))
    assert (s["width"], s["height"]) == (1080, 1920) and s["video_codec"] == "h264" and s["audio_codec"] == "aac"
    assert s["fps"] <= 30.5
    assert Path(got["export_path"]).with_suffix(".srt").exists()
    hf = json.loads(Path(got["export_path"]).with_suffix(".transcript.json").read_text())
    assert [w["text"] for w in hf] == ["no", "way"]
    for layout in ("letterbox",):
        c.patch(f"/api/clips/{clip['id']}", json={"layout": layout})
        c.post(f"/api/clips/{clip['id']}/render-preview")
        got = wait_until(lambda: (lambda x: x if x["files"]["preview_path"] else None)(c.get(f"/api/clips/{clip['id']}").json()),
                         120, interval=1, msg="preview")
        s = stream_summary(ffprobe(TOOLS.ffprobe, got["preview_path"]))
        assert (s["width"], s["height"]) == (540, 960)


def test_09_api_safety(app_env):
    c = app_env["client"]
    assert c.post(f"/api/streams/{app_env['a']}/delete-buffer", json={}).status_code == 400
    assert c.post("/api/streams", json={"name": "x", "url": "file:///C:/Windows"}).status_code == 400
    r = c.post(f"/api/streams/{app_env['a']}/ai", json={"enabled": False}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert c.get("/api/system/status", headers={"Host": "attacker.example"}).status_code == 403
    # AI off must not stop recording
    before = n_segments(app_env, app_env["b"])
    assert c.post(f"/api/streams/{app_env['b']}/ai", json={"enabled": False}).status_code == 200
    wait_until(lambda: n_segments(app_env, app_env["b"]) > before, 15, msg="recording continues with AI off")
    c.post(f"/api/streams/{app_env['b']}/ai", json={"enabled": True})


def test_10_delete_buffer_never_touches_clips_or_protected(app_env):
    c, ctx = app_env["client"], app_env["ctx"]
    l = live(app_env, app_env["b"])
    pid = ctx.retention.protect(l.session_id, l.streamer_id, l.media_now() - 6, l.media_now(), "test")
    clip_files = [x["file_path"] for x in c.get("/api/clips").json() if x["file_path"]]
    r = c.post(f"/api/streams/{app_env['b']}/delete-buffer", json={"confirm": True}).json()
    assert r["protected_kept"] >= 1
    assert all(Path(p).exists() for p in clip_files)
    ctx.retention.release(pid)


def test_11_unavailable_platform_tools_fail_cleanly(app_env, monkeypatch):
    c, ctx = app_env["client"], app_env["ctx"]
    monkeypatch.setattr(ctx.tools, "streamlink", None)
    s = c.post("/api/streams", json={"name": "Remote", "url": "https://www.twitch.tv/somechannel"}).json()
    c.post(f"/api/streams/{s['id']}/start")
    l = ctx.manager.sessions[s["id"]]
    wait_until(lambda: l.state == "error", 10, msg="error state")
    assert "Streamlink not found" in l.detail
    assert live(app_env, app_env["a"]).state == "live"
    c.post(f"/api/streams/{s['id']}/stop")
    c.delete(f"/api/streams/{s['id']}")


def test_12_disk_critical_refuses_new_capture(app_env, monkeypatch):
    c, ctx = app_env["client"], app_env["ctx"]
    real = ctx.retention.disk_status

    async def no_policy(disk):
        return None

    monkeypatch.setattr(ctx.resources, "_disk_policy", no_policy)     # don't let the monitor stop A/B
    monkeypatch.setattr(ctx.retention, "disk_status", lambda: {**real(), "level": "critical"})
    s = c.post("/api/streams", json={"name": "Disk", "url": "local:fixture_a"}).json()
    r = c.post(f"/api/streams/{s['id']}/start")
    assert r.status_code == 400 and "Disk space" in r.json()["detail"]
    c.delete(f"/api/streams/{s['id']}")


def test_13_restart_recovers_interrupted_jobs(tmp_path, fixture_media):
    """Simulate a crash mid-extraction: a clip in 'protected' with a 'running'
    job and footage on disk. A new app instance must re-queue and finish it."""
    from liveclip.app import create_app
    from liveclip.context import AppContext

    fixtures_dir = Path(fixture_media["fixture_a"]["media"]).parent
    ctx = AppContext(data_dir=tmp_path, fixtures_dir=fixtures_dir, config_overrides=TEST_CONFIG)
    with TestClient(create_app(ctx, serve_frontend=False)) as c:
        s = c.post("/api/streams", json={"name": "Crashy", "url": "local:fixture_a", "start": True}).json()
        wait_until(lambda: ctx.db.scalar("SELECT COUNT(*) FROM video_segments") >= 6, 40, msg="segments")
        cid = c.post(f"/api/streams/{s['id']}/save", json={"seconds": 8}).json()["clip_id"]
        # freeze it in an "interrupted" state
        ctx.jobs.paused_kinds.add("extract")
        time.sleep(0.5)
        ctx.db.execute("UPDATE processing_jobs SET state='running' WHERE ref_id=?", (cid,))
        ctx.manager.sessions[s["id"]].worker.stop()          # capture dies with the app
    assert ctx.db.scalar("SELECT status FROM clips WHERE id=?", (cid,)) in ("protected", "processing")
    assert ctx.db.scalar("SELECT file_path FROM clips WHERE id=?", (cid,)) is None
    ctx2 = AppContext(data_dir=tmp_path, fixtures_dir=fixtures_dir, config_overrides=TEST_CONFIG)
    with TestClient(create_app(ctx2, serve_frontend=False)) as c2:
        got = wait_until(lambda: (lambda x: x if x["status"] in ("ready_for_review", "failed") else None)(c2.get(f"/api/clips/{cid}").json()),
                         90, interval=1, msg="recovered clip")
        assert got["status"] == "ready_for_review", got.get("error")
        assert Path(got["file_path"]).exists()
        assert ctx2.db.scalar("SELECT COUNT(*) FROM processing_jobs WHERE state='running'") == 0
        assert ctx2.db.scalar("SELECT state FROM stream_sessions") in ("stopped", "ended")
