"""Publishing providers.

* local     – always available: the export file in data/exports/<streamer>/.
* youtube   – YouTube Data API v3 resumable upload via OAuth installed-app
              (loopback) flow. Needs a Google Cloud OAuth client JSON
              (`publishing.youtube_client_secrets_path`). Disabled until set.
              An upload is only reported as done when YouTube returns a video id.
* instagram – Instagram Graph API Reels publishing requires a Business/Creator
              account linked to a Facebook Page AND a publicly reachable
              video URL. A local-only app cannot provide that URL, so this
              provider reports "not available" and the UI offers manual upload.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

SCOPE = "https://www.googleapis.com/auth/youtube.upload"


class YouTubePublisher:
    def __init__(self, ctx):
        self.ctx = ctx
        self.token_file = ctx.paths.database / "youtube_token.json"

    def _client(self) -> dict | None:
        p = self.ctx.config.publishing.youtube_client_secrets_path
        if not p or not Path(p).exists():
            return None
        data = json.loads(Path(p).read_text(encoding="utf-8"))
        return data.get("installed") or data.get("web")

    def status(self) -> dict:
        if not self._client():
            return {"available": False, "detail": "Not configured: add an OAuth client JSON path in Settings → Publishing"}
        if not self.token_file.exists():
            return {"available": True, "authorized": False, "detail": "Click Connect YouTube to authorize"}
        return {"available": True, "authorized": True, "detail": "Connected"}

    async def authorize(self) -> dict:
        """Loopback OAuth: opens the system browser, receives the code on 127.0.0.1."""
        c = self._client()
        if not c:
            raise RuntimeError("YouTube OAuth client not configured")
        state = secrets.token_urlsafe(16)
        result: dict = {}
        got = asyncio.Event()

        async def handle(reader, writer):
            line = (await reader.readline()).decode()
            q = parse_qs(urlparse(line.split(" ")[1]).query) if " " in line else {}
            if q.get("state", [""])[0] == state and "code" in q:
                result["code"] = q["code"][0]
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n\r\n<h3>LiveClip AI: you can close this tab.</h3>")
            await writer.drain()
            writer.close()
            got.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        redirect = f"http://127.0.0.1:{port}"
        url = c["auth_uri"] + "?" + urlencode({"client_id": c["client_id"], "redirect_uri": redirect, "response_type": "code",
                                               "scope": SCOPE, "state": state, "access_type": "offline", "prompt": "consent"})
        webbrowser.open(url)
        try:
            await asyncio.wait_for(got.wait(), timeout=300)
        finally:
            server.close()
        if "code" not in result:
            raise RuntimeError("Authorization was not completed")
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(c["token_uri"], data={"code": result["code"], "client_id": c["client_id"],
                                                   "client_secret": c.get("client_secret", ""), "redirect_uri": redirect,
                                                   "grant_type": "authorization_code"})
            r.raise_for_status()
            tok = r.json()
        tok["obtained_at"] = time.time()
        self.token_file.write_text(json.dumps(tok), encoding="utf-8")
        return self.status()

    async def _access_token(self) -> str:
        c = self._client()
        tok = json.loads(self.token_file.read_text(encoding="utf-8"))
        if time.time() < tok["obtained_at"] + tok.get("expires_in", 0) - 60:
            return tok["access_token"]
        async with httpx.AsyncClient(timeout=20) as h:
            r = await h.post(c["token_uri"], data={"client_id": c["client_id"], "client_secret": c.get("client_secret", ""),
                                                   "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
            r.raise_for_status()
            new = r.json()
        tok.update(new)
        tok["obtained_at"] = time.time()
        self.token_file.write_text(json.dumps(tok), encoding="utf-8")
        return tok["access_token"]

    async def upload(self, path: str, title: str, description: str, tags: list[str]) -> str:
        token = await self._access_token()
        meta = {"snippet": {"title": title[:100], "description": description[:4900], "tags": tags[:15], "categoryId": "20"},
                "status": {"privacyStatus": self.ctx.config.publishing.youtube_privacy, "selfDeclaredMadeForKids": False}}
        size = Path(path).stat().st_size
        async with httpx.AsyncClient(timeout=httpx.Timeout(60, read=600)) as h:
            r = await h.post("https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
                             headers={"Authorization": f"Bearer {token}", "X-Upload-Content-Type": "video/mp4",
                                      "X-Upload-Content-Length": str(size)}, json=meta)
            r.raise_for_status()
            loc = r.headers["Location"]
            with open(path, "rb") as f:
                r = await h.put(loc, content=f.read(), headers={"Authorization": f"Bearer {token}", "Content-Type": "video/mp4"})
            r.raise_for_status()
            vid = r.json().get("id")
        if not vid:
            raise RuntimeError("YouTube did not return a video id; upload not confirmed")
        return vid


def instagram_status() -> dict:
    return {"available": False, "detail": "Instagram Reels API publishing needs a Business/Creator account linked to a "
            "Facebook Page and a public video URL; a local-only app can't host one. Export the MP4 and upload manually."}
