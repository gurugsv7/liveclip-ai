"""Per-session chat clients.

Each stream session owns exactly one chat client instance. The client only
knows its own channel and delivers messages to the callback bound to that
session, so chat from one streamer can never reach another streamer's
analyzer. Unavailable chat is reported honestly via ``status``; nothing is
ever synthesized.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import time
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("liveclip.chat")

OnMessage = Callable[[str, str, int, float], None]  # author, text, emote_count, wall_t


class ChatClient:
    platform = "none"

    def __init__(self, channel: str, on_message: OnMessage):
        self.channel = channel
        self.on_message = on_message
        self.status = "idle"         # idle|connecting|connected|unavailable|error|stopped
        self.detail = ""
        self.messages = 0
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        self._task = asyncio.create_task(self._supervise(), name=f"chat-{self.platform}-{self.channel}")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self.status = "stopped"

    async def _supervise(self) -> None:
        delay = 2.0
        while not self._stop.is_set():
            try:
                self.status = "connecting"
                await self.run()
                if self.status == "unavailable":
                    return
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors etc.
                self.status = "error"
                self.detail = f"{type(e).__name__}: {e}"[:200]
                log.warning("chat %s/%s error: %s", self.platform, self.channel, self.detail)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay + random.random())
            except asyncio.TimeoutError:
                pass
            delay = min(delay * 2, 60)

    async def run(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _emit(self, author: str, text: str, emotes: int, wall: float | None = None) -> None:
        self.messages += 1
        self.on_message(author, text, emotes, wall or time.time())

    def info(self) -> dict:
        return {"platform": self.platform, "status": self.status, "detail": self.detail, "messages": self.messages}


class NoChat(ChatClient):
    async def run(self) -> None:
        self.status = "unavailable"


class TwitchChat(ChatClient):
    """Anonymous read-only Twitch IRC over WebSocket (documented Twitch chat
    interface; `justinfanNNNN` nick needs no OAuth for reading)."""

    platform = "twitch"
    URL = "wss://irc-ws.chat.twitch.tv:443"

    async def run(self) -> None:
        import websockets

        async with websockets.connect(self.URL, ping_interval=60, close_timeout=3) as ws:
            await ws.send("CAP REQ :twitch.tv/tags")
            await ws.send("PASS SCHMOOPIIE")
            await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
            await ws.send(f"JOIN #{self.channel}")
            self.status = "connected"
            self.detail = f"#{self.channel}"
            async for raw in ws:
                if self._stop.is_set():
                    return
                for line in str(raw).split("\r\n"):
                    if not line:
                        continue
                    if line.startswith("PING"):
                        await ws.send("PONG :tmi.twitch.tv")
                        continue
                    msg = parse_twitch_privmsg(line, self.channel)
                    if msg:
                        self._emit(*msg)
                    elif " RECONNECT" in line:
                        return


_TW_RE = re.compile(r"^(?:@(?P<tags>\S+) )?:(?P<nick>[^!\s]+)!\S+ PRIVMSG #(?P<chan>\S+) :(?P<text>.*)$")


def parse_twitch_privmsg(line: str, channel: str) -> tuple[str, str, int] | None:
    m = _TW_RE.match(line)
    if not m or m.group("chan").lower() != channel.lower():
        return None  # never accept messages for another channel
    emotes = 0
    tags = m.group("tags") or ""
    for t in tags.split(";"):
        if t.startswith("emotes=") and len(t) > 7:
            emotes = sum(len(e.split(":", 1)[1].split(",")) for e in t[7:].split("/") if ":" in e)
    author = m.group("nick")
    for t in tags.split(";"):
        if t.startswith("display-name=") and len(t) > 13:
            author = t[13:]
    return author, m.group("text"), emotes


class YouTubeChat(ChatClient):
    """YouTube Data API v3 liveChatMessages polling. Requires YOUTUBE_API_KEY.
    Honors the server-provided polling interval and tracks quota usage."""

    platform = "youtube"
    API = "https://www.googleapis.com/youtube/v3"
    DAILY_QUOTA = 10_000
    LIST_COST = 5

    def __init__(self, channel: str, on_message: OnMessage, page_url: str, min_interval_s: float = 8.0):
        super().__init__(channel, on_message)
        self.page_url = page_url
        self.min_interval = min_interval_s
        self.quota_used = 0

    async def _video_id(self, client: httpx.AsyncClient) -> str | None:
        if re.fullmatch(r"[A-Za-z0-9_\-]{11}", self.channel):
            return self.channel
        # Channel /live page: read the canonical video id from the page.
        r = await client.get(self.page_url, headers={"Accept-Language": "en"}, follow_redirects=True)
        m = re.search(r'<link rel="canonical" href="https://www\.youtube\.com/watch\?v=([A-Za-z0-9_\-]{11})"', r.text)
        return m.group(1) if m else None

    async def run(self) -> None:
        key = os.environ.get("YOUTUBE_API_KEY")
        if not key:
            self.status = "unavailable"
            self.detail = "Set YOUTUBE_API_KEY to enable YouTube live chat"
            return
        async with httpx.AsyncClient(timeout=15) as client:
            vid = await self._video_id(client)
            if not vid:
                self.status = "unavailable"
                self.detail = "Could not resolve live video id"
                return
            r = await client.get(f"{self.API}/videos", params={"part": "liveStreamingDetails", "id": vid, "key": key})
            self.quota_used += 1
            r.raise_for_status()
            items = r.json().get("items", [])
            chat_id = items[0].get("liveStreamingDetails", {}).get("activeLiveChatId") if items else None
            if not chat_id:
                self.status = "unavailable"
                self.detail = "Live chat disabled or stream not live"
                return
            self.status = "connected"
            page = None
            first = True
            while not self._stop.is_set():
                if self.quota_used + self.LIST_COST > self.DAILY_QUOTA:
                    self.status = "unavailable"
                    self.detail = "YouTube API daily quota exhausted"
                    return
                params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "key": key, "maxResults": 2000}
                if page:
                    params["pageToken"] = page
                r = await client.get(f"{self.API}/liveChat/messages", params=params)
                self.quota_used += self.LIST_COST
                if r.status_code == 403:
                    self.status = "error"
                    self.detail = "403 from YouTube API (quota or permission)"
                    await asyncio.sleep(300)
                    continue
                r.raise_for_status()
                data = r.json()
                page = data.get("nextPageToken")
                if not first:  # skip backlog on first page
                    for it in data.get("items", []):
                        sn = it.get("snippet", {})
                        text = sn.get("displayMessage") or ""
                        author = it.get("authorDetails", {}).get("displayName", "")
                        if text:
                            self._emit(author, text, 0)
                first = False
                interval = max(self.min_interval, data.get("pollingIntervalMillis", 5000) / 1000)
                self.detail = f"polling every {interval:.0f}s, quota ~{self.quota_used}/{self.DAILY_QUOTA}"
                await asyncio.sleep(interval)


class KickChat(ChatClient):
    """Kick has no stable documented public chat API. This experimental client
    uses Kick's public Pusher channel and is disabled unless
    LIVECLIP_KICK_CHAT=1. Capture never depends on it."""

    platform = "kick"
    PUSHER = "wss://ws-us2.pusher.com/app/32cbd69e4b950bf97679?protocol=7&client=js&version=8.4.0&flash=false"

    async def run(self) -> None:
        if os.environ.get("LIVECLIP_KICK_CHAT") != "1":
            self.status = "unavailable"
            self.detail = "Kick chat is experimental; set LIVECLIP_KICK_CHAT=1 to try it"
            return
        import websockets

        async with httpx.AsyncClient(timeout=10, headers={"Accept": "application/json"}) as client:
            r = await client.get(f"https://kick.com/api/v2/channels/{self.channel}")
            if r.status_code != 200:
                self.status = "unavailable"
                self.detail = f"Kick channel API returned {r.status_code}"
                return
            room = r.json().get("chatroom", {}).get("id")
        if not room:
            self.status = "unavailable"
            self.detail = "No chatroom id"
            return
        async with websockets.connect(self.PUSHER, close_timeout=3) as ws:
            await ws.send(json.dumps({"event": "pusher:subscribe", "data": {"auth": "", "channel": f"chatrooms.{room}.v2"}}))
            self.status = "connected"
            async for raw in ws:
                if self._stop.is_set():
                    return
                try:
                    msg = json.loads(raw)
                    if msg.get("event") == "App\\Events\\ChatMessageEvent":
                        d = json.loads(msg["data"])
                        text = d.get("content", "")
                        emotes = text.count("[emote:")
                        self._emit(d.get("sender", {}).get("username", ""), text, emotes)
                    elif msg.get("event") == "pusher:ping":
                        await ws.send(json.dumps({"event": "pusher:pong", "data": {}}))
                except (ValueError, KeyError):
                    continue


class LocalChat(ChatClient):
    """Replays a JSONL chat fixture ({"t": seconds, "user": "...", "text": "..."})
    relative to session start. Used for deterministic tests and demos."""

    platform = "local"

    def __init__(self, channel: str, on_message: OnMessage, path: str | None, loop_s: float | None = None,
                 start_wall: Callable[[], float | None] | None = None):
        super().__init__(channel, on_message)
        self.path = path
        self.loop_s = loop_s
        self.start_wall = start_wall

    async def run(self) -> None:
        if not self.path:
            self.status = "unavailable"
            self.detail = "No chat fixture for this source"
            return
        rows = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        rows.sort(key=lambda r: r["t"])
        self.status = "connected"
        # wait for capture to begin so fixture time aligns with media time
        t0 = None
        while t0 is None and not self._stop.is_set():
            t0 = self.start_wall() if self.start_wall else time.time()
            if t0 is None:
                await asyncio.sleep(0.2)
        cycle = 0
        while not self._stop.is_set():
            base = t0 + cycle * (self.loop_s or 0)
            for r in rows:
                due = base + float(r["t"])
                delay = due - time.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                self._emit(r.get("user", "viewer"), r["text"], int(r.get("emotes", 0)), wall=due)
            if not self.loop_s:
                await self._stop.wait()
                return
            cycle += 1


def make_chat_client(platform: str, channel: str, url: str, on_message: OnMessage, *, local_chat_path: str | None = None,
                     loop_s: float | None = None, start_wall=None) -> ChatClient:
    if platform == "twitch":
        return TwitchChat(channel, on_message)
    if platform == "youtube":
        return YouTubeChat(channel, on_message, url)
    if platform == "kick":
        return KickChat(channel, on_message)
    if platform == "local":
        return LocalChat(channel, on_message, local_chat_path, loop_s=loop_s, start_wall=start_wall)
    return NoChat(channel, on_message)
