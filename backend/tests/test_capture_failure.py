from liveclip.capture.worker import classify_failure


def test_real_twitch_offline():
    # real output, `streamlink https://www.twitch.tv/jynxzi` on 2026-11-18
    lines = ["[streamlink] [cli][info] Found matching plugin twitch for URL https://www.twitch.tv/jynxzi",
             "[streamlink] error: No playable streams found on this URL: https://www.twitch.tv/jynxzi"]
    assert classify_failure(lines) == ("offline", "Channel is offline")


def test_youtube_bot_check_is_not_reported_as_offline():
    # format from streamlink/plugins/youtube.py: log.error(f"Could not get video info - {status}: {reason}")
    lines = ["[streamlink] [plugins.youtube][error] Could not get video info - LOGIN_REQUIRED: "
             "Sign in to confirm you're not a bot",
             "[streamlink] error: No playable streams found on this URL: https://www.youtube.com/watch?v=AxvjBuPKAig"]
    outcome, detail = classify_failure(lines)
    assert outcome == "offline"
    assert "LOGIN_REQUIRED" in detail and "bot check" in detail
    assert detail != "Channel is offline"


def test_youtube_not_started_and_protected():
    o, d = classify_failure(["[plugins.youtube][error] Could not get video info - LIVE_STREAM_OFFLINE: "
                             "This live event will begin in 2 hours."])
    assert o == "offline" and d.startswith("Stream not started yet")
    o, d = classify_failure(["error: This plugin does not support protected videos, try yt-dlp instead"])
    assert o == "offline" and "update streamlink" in d


def test_other_failures_stay_errors():
    assert classify_failure(["[ffmpeg] error: no media received within 90s"])[0] == "error"
