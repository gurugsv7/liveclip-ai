"""Validation of past-broadcast (VOD) sources: https links on Twitch / YouTube / Kick,
or a video file already on this computer."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..platforms import ALLOWED_HOSTS, SourceError

MEDIA_EXT = {".mp4", ".mkv", ".mov", ".ts", ".flv", ".webm", ".m4v", ".m4a", ".mp3", ".aac", ".wav"}


@dataclass
class VodSource:
    platform: str          # twitch | youtube | kick | local
    url: str | None        # canonical page URL (None for a local file)
    video_id: str
    path: str | None = None


def resolve_vod(url: str | None = None, path: str | None = None) -> VodSource:
    if path:
        p = Path(path.strip().strip('"')).expanduser()
        if not p.is_absolute():
            raise SourceError("Give the full path of the video file")
        if not p.is_file():
            raise SourceError(f"File not found: {p}")
        if p.suffix.lower() not in MEDIA_EXT:
            raise SourceError(f"Not a video/audio file: {p.name}")
        return VodSource("local", None, p.stem, str(p.resolve()))
    url = (url or "").strip()
    if not url:
        raise SourceError("Paste a VOD link or a file path")
    u = urlparse(url)
    if u.scheme != "https":
        raise SourceError("Only https:// VOD links are accepted")
    if u.username or u.password or u.port not in (None, 443):
        raise SourceError("URLs with credentials or custom ports are not accepted")
    host = (u.hostname or "").lower()
    seg = [s for s in u.path.split("/") if s]
    if host in ALLOWED_HOSTS["twitch"]:
        # https://www.twitch.tv/videos/123456789  (also /<channel>/v/123 and /<channel>/video/123)
        vid = None
        if len(seg) >= 2 and seg[0] == "videos":
            vid = seg[1]
        elif len(seg) >= 3 and seg[1] in ("v", "video"):
            vid = seg[2]
        if not vid or not re.fullmatch(r"\d{5,15}", vid):
            raise SourceError("Twitch VOD links look like https://www.twitch.tv/videos/123456789")
        return VodSource("twitch", f"https://www.twitch.tv/videos/{vid}", vid)
    if host in ALLOWED_HOSTS["youtube"]:
        q = parse_qs(u.query)
        vid = None
        if host == "youtu.be" and seg:
            vid = seg[0]
        elif seg and seg[0] == "watch":
            vid = (q.get("v") or [None])[0]
        elif len(seg) >= 2 and seg[0] in ("live", "shorts", "embed"):
            vid = seg[1]
        if not vid or not re.fullmatch(r"[A-Za-z0-9_\-]{6,20}", vid):
            raise SourceError("YouTube VOD links look like https://www.youtube.com/watch?v=...")
        return VodSource("youtube", f"https://www.youtube.com/watch?v={vid}", vid)
    if host in ALLOWED_HOSTS["kick"]:
        # https://kick.com/<channel>/videos/<uuid>  or  https://kick.com/video/<uuid>
        vid = None
        if len(seg) >= 3 and seg[1] == "videos":
            vid = seg[2]
        elif len(seg) >= 2 and seg[0] == "video":
            vid = seg[1]
        if not vid or not re.fullmatch(r"[A-Za-z0-9\-]{8,64}", vid):
            raise SourceError("Kick VOD links look like https://kick.com/<channel>/videos/<id>")
        canon = f"https://kick.com/{seg[0]}/videos/{vid}" if seg[1] == "videos" else f"https://kick.com/video/{vid}"
        return VodSource("kick", canon, vid)
    raise SourceError(f"Unsupported host '{host}'. Supported: Twitch, YouTube, Kick VODs or a local file")


def archive_listing_url(platform: str, channel_url: str) -> str | None:
    """Where a channel's past broadcasts are listed (newest first)."""
    u = urlparse(channel_url)
    seg = [s for s in u.path.split("/") if s]
    if not seg:
        return None
    if platform == "twitch":
        return f"https://www.twitch.tv/{seg[0]}/videos?filter=archives&sort=time"
    if platform == "kick":
        return f"https://kick.com/{seg[0]}/videos"
    if platform == "youtube":
        if seg[0] == "watch":
            return None                       # a single live video: its VOD is the same link
        base = "/".join(seg[:2] if seg[0] in ("channel", "c", "user") else seg[:1])
        return f"https://www.youtube.com/{base}/streams"
    return None
