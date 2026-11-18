"""Watch the clips *viewers* create on Twitch while the stream is live.

Every viewer clip is a human saying "this was a moment" — the strongest highlight
signal there is. Twitch Helix `GET /clips` (app access token via the free
client-credentials flow) is polled every ~15 s for the monitored channel; each new
clip's `created_at` (wall clock) is mapped onto this session's media timeline and
handed to the session, which protects and saves that range from its own buffer.

Needs TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (create a free app at
https://dev.twitch.tv/console). Without them the watcher reports "unavailable" and
nothing else changes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Callable

import httpx

log = logging.getLogger("liveclip.twitch_clips")

OnClip = Callable[[dict], None]   # {"id","created_wall","duration","title","creator","url","view_count"}


def _parse_ts(s: str) -> float:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def _rfc3339(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class TwitchClipWatcher:
    TOKEN_URL = "https://id.twitch.tv/oauth2/token"
    API = "https://api.twitch.tv/helix"

    def __init__(self, channel: str, on_clip: OnClip, interval_s: float = 15.0):
        self.channel = channel
        self.on_clip = on_clip
        self.interval = interval_s
        self.status = "idle"
        self.detail = ""
        self.seen: set[str] = set()
        self.found = 0
        self._task: asyncio.Task | None = None
        self._token: tuple[str, float] | None = None
        self.started_wall = time.time()

    @staticmethod
    def configured() -> bool:
        return bool(os.environ.get("TWITCH_CLIENT_ID") and os.environ.get("TWITCH_CLIENT_SECRET"))

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"twitch-clips-{self.channel}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass

    async def _token_for(self, c: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._token[1] - 120:
            return self._token[0]
        r = await c.post(self.TOKEN_URL, data={"client_id": os.environ["TWITCH_CLIENT_ID"],
                                                "client_secret": os.environ["TWITCH_CLIENT_SECRET"],
                                                "grant_type": "client_credentials"})
        r.raise_for_status()
        j = r.json()
        self._token = (j["access_token"], time.time() + j.get("expires_in", 3600))
        return self._token[0]

    async def _run(self) -> None:
        if not self.configured():
            self.status, self.detail = "unavailable", "Set TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET to use viewer clips"
            return
        cid = os.environ["TWITCH_CLIENT_ID"]
        delay = self.interval
        async with httpx.AsyncClient(timeout=15) as c:
            broadcaster = None
            while True:
                try:
                    h = {"Client-Id": cid, "Authorization": f"Bearer {await self._token_for(c)}"}
                    if broadcaster is None:
                        r = await c.get(f"{self.API}/users", params={"login": self.channel}, headers=h)
                        r.raise_for_status()
                        data = r.json().get("data") or []
                        if not data:
                            self.status, self.detail = "unavailable", f"Twitch user {self.channel} not found"
                            return
                        broadcaster = data[0]["id"]
                    since = max(self.started_wall, time.time() - 600) - 60
                    r = await c.get(f"{self.API}/clips", headers=h, params={
                        "broadcaster_id": broadcaster, "started_at": _rfc3339(since),
                        "ended_at": _rfc3339(time.time() + 60), "first": 50})
                    if r.status_code == 401:
                        self._token = None
                        continue
                    if r.status_code == 429:
                        delay = min(120, delay * 2)
                        self.detail = "rate limited, backing off"
                    else:
                        r.raise_for_status()
                        delay = self.interval
                        self.status = "watching"
                        for clip in r.json().get("data", []):
                            if clip["id"] in self.seen:
                                continue
                            self.seen.add(clip["id"])
                            created = _parse_ts(clip["created_at"])
                            if created < self.started_wall:
                                continue          # made before we started capturing
                            self.found += 1
                            self.on_clip({"id": clip["id"], "created_wall": created, "duration": float(clip.get("duration") or 30),
                                          "title": clip.get("title") or "", "creator": clip.get("creator_name") or "",
                                          "url": clip.get("url") or "", "view_count": clip.get("view_count") or 0})
                        self.detail = f"{self.found} viewer clips this session"
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self.status, self.detail = "error", f"{type(e).__name__}: {e}"[:200]
                    log.warning("viewer clip watcher %s: %s", self.channel, self.detail)
                    delay = min(120, delay * 2)
                await asyncio.sleep(delay)

    def info(self) -> dict:
        return {"status": self.status, "detail": self.detail, "found": self.found}
