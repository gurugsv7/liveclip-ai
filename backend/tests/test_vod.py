"""VOD (past broadcast) analysis: source validation, chat replay parsing, and a whole
local recording run through the job end to end (audio -> detection -> clip in Review)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import needs_ffmpeg, wait_until
from liveclip.platforms import SourceError
from liveclip.vod.source import archive_listing_url, resolve_vod
from liveclip.vod.ytdlp import parse_youtube_live_chat


@pytest.mark.parametrize("url,platform,canon", [
    ("https://www.twitch.tv/videos/2932568279", "twitch", "https://www.twitch.tv/videos/2932568279"),
    ("https://www.twitch.tv/jynxzi/v/2932568279", "twitch", "https://www.twitch.tv/videos/2932568279"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30", "youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ", "youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://www.youtube.com/live/dQw4w9WgXcQ", "youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://kick.com/grootr6/videos/0b9a3c2e-1f7d-4c55-9a7e-2b1c0d9e8f71", "kick",
     "https://kick.com/grootr6/videos/0b9a3c2e-1f7d-4c55-9a7e-2b1c0d9e8f71"),
])
def test_vod_links_accepted(url, platform, canon):
    s = resolve_vod(url)
    assert (s.platform, s.url) == (platform, canon)


@pytest.mark.parametrize("url", [
    "http://www.twitch.tv/videos/123456", "https://www.twitch.tv/jynxzi", "https://evil.com/videos/123456",
    "https://user:pw@www.twitch.tv/videos/123456", "https://www.youtube.com/@channel", "https://kick.com/grootr6", "",
])
def test_vod_links_rejected(url):
    with pytest.raises(SourceError):
        resolve_vod(url)


def test_local_vod_needs_an_existing_absolute_media_file(tmp_path):
    with pytest.raises(SourceError):
        resolve_vod(path="relative.mp4")
    with pytest.raises(SourceError):
        resolve_vod(path=str(tmp_path / "missing.mp4"))
    txt = tmp_path / "notes.txt"
    txt.write_text("x")
    with pytest.raises(SourceError):
        resolve_vod(path=str(txt))
    vid = tmp_path / "stream.mp4"
    vid.write_bytes(b"\0" * 10)
    assert resolve_vod(path=f'"{vid}"').platform == "local"


def test_archive_listing_urls():
    assert archive_listing_url("twitch", "https://www.twitch.tv/jynxzi") == \
        "https://www.twitch.tv/jynxzi/videos?filter=archives&sort=time"
    assert archive_listing_url("kick", "https://kick.com/grootr6") == "https://kick.com/grootr6/videos"
    assert archive_listing_url("youtube", "https://www.youtube.com/@IShowSpeed/live") == \
        "https://www.youtube.com/@IShowSpeed/streams"
    assert archive_listing_url("youtube", "https://www.youtube.com/watch?v=dQw4w9WgXcQ") is None


def test_youtube_chat_replay_is_parsed_with_video_offsets(tmp_path):
    def line(ms, author, runs):
        return json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(ms), "actions": [{"addChatItemAction": {
            "item": {"liveChatTextMessageRenderer": {"authorName": {"simpleText": author}, "message": {"runs": runs}}}}}]}})
    p = tmp_path / "x.live_chat.json"
    p.write_text("\n".join([
        line(12500, "a", [{"text": "no way"}]),
        line(3000, "b", [{"text": "W "}, {"emoji": {"shortcuts": [":fire:"]}}]),
        json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "4000", "actions": [{"addLiveChatTickerItemAction": {}}]}}),
        "not json",
    ]), encoding="utf-8")
    assert parse_youtube_live_chat(p) == [(3.0, "b", "W :fire:", 1), (12.5, "a", "no way", 0)]


@needs_ffmpeg
@pytest.mark.media
def test_local_recording_becomes_clips_in_review(tmp_path, fixture_media):
    """A 40 s recording with a loud burst at 22 s: the VOD job finds it, cuts only that
    stretch, and the clip goes through the normal pipeline in its own 'vod' session."""
    from liveclip.app import create_app
    from liveclip.context import AppContext

    cfg = {"detection": {"min_baseline_s": 8, "trigger_threshold": 0.55, "cooldown_s": 10, "merge_window_s": 10,
                         "sound_events_enabled": False, "live_speech_enabled": False},
           "clips": {"pre_context_s": 6, "post_context_s": 4, "max_reaction_wait_s": 8, "max_clip_s": 30},
           "resume_monitoring": False}
    ctx = AppContext(data_dir=tmp_path, config_overrides=cfg)
    media = fixture_media["fixture_a"]["media"]
    with TestClient(create_app(ctx, serve_frontend=False)) as c:
        assert c.post("/api/vods", json={"path": "relative/file.mp4"}).status_code == 400
        job = c.post("/api/vods", json={"path": media}).json()
        assert c.post("/api/vods", json={"path": media}).status_code == 400     # already running
        done = wait_until(lambda: (j := c.get("/api/vods").json()["jobs"][0])["state"] in ("done", "failed") and j,
                          120, msg="vod job")
        assert done["state"] == "done", done
        assert done["moments"] >= 1 and done["clips"] >= 1
        sess = ctx.db.one("SELECT * FROM stream_sessions WHERE id=?", (done["session_id"],))
        assert sess["kind"] == "vod" and sess["streamer_id"] == done["streamer_id"]
        st = ctx.db.one("SELECT * FROM streamers WHERE id=?", (done["streamer_id"],))
        assert st["priority"] == 99                                    # not a monitored slot
        clips = wait_until(lambda: [r for r in ctx.db.all("SELECT * FROM clips WHERE session_id=?", (sess["id"],))
                                    if r["file_path"] and r["status"] not in ("protected", "processing")], 120, msg="clip")
        cl = clips[0]
        assert cl["streamer_id"] == st["id"] and cl["detection_method"] == "auto"
        assert cl["t_start"] <= 22 <= cl["t_end"]                      # the burst is inside the clip
        assert Path(cl["file_path"]).exists()
        assert Path(media).exists()                                    # the user's own file is never touched
        assert c.get(f"/api/vods/{job['id']}").status_code in (404, 405)  # no such route; list is the API
        assert c.delete(f"/api/vods/{job['id']}").json() == {"ok": True}
