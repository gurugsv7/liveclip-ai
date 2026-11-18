"""Platform adapters.

``resolve_source(url)`` validates a user-supplied URL and returns a
``SourceInfo`` describing which adapter owns it. Only https URLs on known
streaming hosts are accepted (plus ``local:`` fixtures when explicitly
enabled), which prevents the capture engine from being pointed at local files,
LAN hosts or arbitrary network targets.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

ALLOWED_HOSTS = {
    "twitch": {"twitch.tv", "www.twitch.tv", "m.twitch.tv"},
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"},
    "kick": {"kick.com", "www.kick.com"},
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class SourceError(ValueError):
    pass


@dataclass
class SourceInfo:
    platform: str
    url: str
    channel: str               # channel login / handle / video id / fixture name
    capabilities: dict = field(default_factory=dict)
    local_path: str | None = None
    local_chat_path: str | None = None


def slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s[:40] or "stream"


def resolve_source(url: str, allow_local: bool = False, fixtures_dir: Path | None = None) -> SourceInfo:
    url = (url or "").strip()
    if not url:
        raise SourceError("URL is empty")
    if url.startswith("local:"):
        if not allow_local:
            raise SourceError("Local fixture sources are disabled (enable allow_local_sources in settings)")
        name = url[len("local:"):]
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", name):
            raise SourceError("Local fixture name may only contain letters, digits, '-' and '_'")
        if fixtures_dir is None:
            raise SourceError("No fixtures directory configured")
        media = (fixtures_dir / f"{name}.mp4").resolve()
        if fixtures_dir.resolve() not in media.parents or not media.exists():
            raise SourceError(f"Fixture '{name}' not found in {fixtures_dir}")
        chat = fixtures_dir / f"{name}.chat.jsonl"
        return SourceInfo("local", url, name, capabilities={"chat": chat.exists(), "capture": True},
                          local_path=str(media), local_chat_path=str(chat) if chat.exists() else None)

    p = urlparse(url)
    if p.scheme != "https":
        raise SourceError("Only https:// stream URLs are accepted")
    if p.username or p.password or (p.port not in (None, 443)):
        raise SourceError("URLs with credentials or custom ports are not accepted")
    host = (p.hostname or "").lower()
    for platform, hosts in ALLOWED_HOSTS.items():
        if host in hosts:
            break
    else:
        raise SourceError(f"Unsupported host '{host}'. Supported: Twitch, YouTube Live, Kick")

    path = [seg for seg in p.path.split("/") if seg]
    if platform == "twitch":
        if not path or path[0] in {"videos", "directory", "settings", "p"}:
            raise SourceError("Twitch URL must be a channel, e.g. https://www.twitch.tv/<channel>")
        channel = path[0].lower()
        if not re.fullmatch(r"[a-z0-9_]{2,25}", channel):
            raise SourceError("Invalid Twitch channel name")
        return SourceInfo("twitch", f"https://www.twitch.tv/{channel}", channel,
                          capabilities={"capture": True, "chat": True, "chat_auth": "anonymous"})
    if platform == "youtube":
        from urllib.parse import parse_qs

        q = parse_qs(p.query)
        if host == "youtu.be" and path:
            vid = path[0]
            return SourceInfo("youtube", f"https://www.youtube.com/watch?v={vid}", vid,
                              capabilities={"capture": True, "chat": "api_key"})
        if path and path[0] == "watch" and q.get("v"):
            vid = q["v"][0]
            if not re.fullmatch(r"[A-Za-z0-9_\-]{6,20}", vid):
                raise SourceError("Invalid YouTube video id")
            return SourceInfo("youtube", f"https://www.youtube.com/watch?v={vid}", vid,
                              capabilities={"capture": True, "chat": "api_key"})
        if path and path[0] == "live" and len(path) > 1:
            vid = path[1]
            return SourceInfo("youtube", f"https://www.youtube.com/watch?v={vid}", vid,
                              capabilities={"capture": True, "chat": "api_key"})
        if path and (path[0].startswith("@") or path[0] in {"channel", "c", "user"}):
            canon = "https://www.youtube.com/" + "/".join(path[:2] if path[0] in {"channel", "c", "user"} else path[:1])
            if not canon.endswith("/live"):
                canon += "/live"
            return SourceInfo("youtube", canon, path[0] if path[0].startswith("@") else path[1],
                              capabilities={"capture": True, "chat": "api_key"})
        raise SourceError("YouTube URL must be a live video, youtu.be link, or channel /live page")
    # kick
    if not path:
        raise SourceError("Kick URL must be a channel, e.g. https://kick.com/<channel>")
    channel = path[0].lower()
    if not re.fullmatch(r"[a-z0-9_\-]{2,30}", channel):
        raise SourceError("Invalid Kick channel name")
    return SourceInfo("kick", f"https://kick.com/{channel}", channel,
                      capabilities={"capture": True, "chat": "experimental"})
