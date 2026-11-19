"""VOD jobs: find the reel-worthy moments in a whole past broadcast (or a local video file).

  1. audio only is downloaded (Twitch Audio_Only / bestaudio), rate-limited so live capture
     always keeps its bandwidth
  2. the same detectors as live run over it on the recording's own clock (vod.analyzer)
  3. for each moment only that stretch of video is downloaded (yt-dlp --download-sections) or
     cut from the local file, registered as a segment of a 'vod' session, and handed to the
     normal clip pipeline (extract -> transcribe -> score -> Review / Maybe)

One job runs at a time. Every row/file carries the job's own streamer_id + session_id.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import threading
import time
from pathlib import Path

from ..ai.tanglish import is_tamil, romanize_text, romanize_words
from ..ai.transcriber import TranscriptionUnavailable
from ..db import new_id
from ..media import MediaError, ffprobe, popen_flags, run, stream_summary
from ..platforms import SourceError, slugify
from .analyzer import Cancelled, VodAnalyzer, VodMoment
from .source import VodSource, archive_listing_url, resolve_vod
from .ytdlp import Cancelled as DlCancelled
from .ytdlp import YtDlp, YtDlpError, parse_youtube_live_chat

log = logging.getLogger("liveclip.vod")

ACTIVE = ("downloading", "analyzing", "cutting")
OPEN = ("queued", "waiting_vod", *ACTIVE)


class _VodSession:
    """What ClipService.create_clip needs from a capture session."""

    def __init__(self, streamer: dict, session_id: str, end: float):
        self.streamer = streamer
        self.session_id = session_id
        self.origin_wall = None
        self._end = end

    def media_now(self) -> float:
        return self._end


class VodService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db = ctx.db
        self.yt = YtDlp(ctx.tools.ffmpeg)
        self._cancel: dict[str, threading.Event] = {}
        self._task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._hls_cache: dict[str, tuple[str | None, float]] = {}

    # --- public API -------------------------------------------------------------
    def info(self) -> dict:
        return {"ytdlp": self.yt.available}

    def submit(self, url: str | None = None, path: str | None = None, streamer_id: str | None = None) -> dict:
        src = resolve_vod(url, path)
        if src.platform != "local" and not self.yt.available:
            raise SourceError("yt-dlp is not installed; run the setup script again")
        if streamer_id and not self.db.one("SELECT 1 FROM streamers WHERE id=?", (streamer_id,)):
            raise SourceError("Unknown streamer")
        key = src.url or src.path
        dup = self.db.one(f"SELECT id FROM vod_jobs WHERE COALESCE(url, path)=? AND state IN ({','.join('?' * len(OPEN))})",
                          (key, *OPEN))
        if dup:
            raise SourceError("That VOD is already being processed")
        jid = new_id("vod_")
        self.db.insert("vod_jobs", {"id": jid, "streamer_id": streamer_id, "source": "manual", "platform": src.platform,
                                    "url": src.url, "path": src.path, "title": Path(src.path).name if src.path else None,
                                    "state": "queued", "created_at": time.time()})
        self._publish(jid)
        self._poke()
        return self.get(jid)

    def after_stream(self, streamer: dict, live_since: float, ended: float) -> str | None:
        """Called from the capture worker thread when a monitored stream goes offline."""
        if not streamer.get("vod_after_stream") or streamer["platform"] not in ("twitch", "youtube", "kick"):
            return None
        if ended - live_since < 120:
            return None                                 # a blip, not a broadcast
        if self.db.one("SELECT 1 FROM vod_jobs WHERE streamer_id=? AND source='after_stream' AND state IN ('waiting_vod','queued')",
                       (streamer["id"],)):
            return None
        jid = new_id("vod_")
        self.db.insert("vod_jobs", {
            "id": jid, "streamer_id": streamer["id"], "source": "after_stream", "platform": streamer["platform"],
            "url": None, "title": f"{streamer['name']} — stream that ended {time.strftime('%H:%M', time.localtime(ended))}",
            "state": "waiting_vod", "not_before": time.time() + 90, "detail": "Waiting for the past broadcast to appear",
            "payload": json.dumps({"channel_url": streamer["url"], "live_since": live_since, "ended": ended}),
            "created_at": time.time()})
        log.info("[%s] stream ended; will analyse its past broadcast (%s)", streamer["name"], jid)
        self._publish(jid)
        self._poke()
        return jid

    def list(self, limit: int = 50) -> list[dict]:
        return [self._view(r) for r in self.db.all("SELECT * FROM vod_jobs ORDER BY created_at DESC LIMIT ?", (limit,))]

    def get(self, jid: str) -> dict | None:
        r = self.db.one("SELECT * FROM vod_jobs WHERE id=?", (jid,))
        return self._view(r) if r else None

    def cancel(self, jid: str) -> dict:
        r = self.db.one("SELECT state FROM vod_jobs WHERE id=?", (jid,))
        if not r:
            raise KeyError(jid)
        if r["state"] in ("queued", "waiting_vod"):
            self._set(jid, state="cancelled", finished_at=time.time(), detail="Cancelled")
        elif r["state"] in ACTIVE and jid in self._cancel:
            self._cancel[jid].set()
        return self.get(jid)

    def remove(self, jid: str) -> None:
        r = self.db.one("SELECT state FROM vod_jobs WHERE id=?", (jid,))
        if r and r["state"] in ACTIVE:
            raise SourceError("Cancel the job first")
        self.db.execute("DELETE FROM vod_jobs WHERE id=?", (jid,))
        self.ctx.hub.publish("vod", {"id": jid, "deleted": True})

    # --- loop ---------------------------------------------------------------------
    def recover(self) -> None:
        """Jobs interrupted by an app stop start over (their audio is re-used when complete)."""
        self.db.execute(f"UPDATE vod_jobs SET state='queued', detail='Restarted after the app stopped' "
                        f"WHERE state IN ({','.join('?' * len(ACTIVE))})", ACTIVE)

    def start(self) -> None:
        self._wake = asyncio.Event()
        self._task = asyncio.create_task(self._loop(), name="vod-loop")

    async def stop(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        if self._task:
            self._task.cancel()

    def _poke(self) -> None:
        w = self._wake
        if w is None:
            return
        try:
            loop = self._task.get_loop() if self._task else None
            if loop:
                loop.call_soon_threadsafe(w.set)
        except RuntimeError:
            pass

    async def _loop(self) -> None:
        while True:
            try:
                job = self.db.one("SELECT * FROM vod_jobs WHERE state IN ('queued','waiting_vod') AND COALESCE(not_before,0)<=? "
                                  "ORDER BY created_at LIMIT 1", (time.time(),))
                if job:
                    ev = self._cancel.setdefault(job["id"], threading.Event())
                    try:
                        await asyncio.to_thread(self._run, job, ev)
                    finally:
                        self._cancel.pop(job["id"], None)
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("vod loop failed")
            try:
                assert self._wake is not None
                self._wake.clear()
                await asyncio.wait_for(self._wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass

    # --- one job (worker thread) ----------------------------------------------------
    def _run(self, job: dict, cancel: threading.Event) -> None:
        jid = job["id"]
        session_id = None
        try:
            if job["state"] == "waiting_vod":
                url = self._find_after_stream_vod(job)
                if url is None:
                    return
                self.db.update("vod_jobs", "id", jid, {"url": url})
                job["url"] = url
            src = resolve_vod(job["url"], job["path"])
            self._set(jid, state="downloading", started_at=time.time(), progress=0.0, detail="Reading the VOD", error=None)
            meta = self._metadata(src)
            if meta.get("live_status") == "is_live":
                if job["source"] == "after_stream":
                    self._set(jid, state="waiting_vod", not_before=time.time() + 300, detail="Stream is live again; waiting")
                    return
                raise SourceError("This broadcast is still live — use the Live monitor for it, or try again after it ends")
            duration = float(meta.get("duration") or 0) or None
            streamer = self._streamer_for(job, src, meta)
            session_id = new_id("ses_")
            sdir = self.ctx.paths.session_dir(streamer["slug"], session_id)
            sdir.mkdir(parents=True, exist_ok=True)
            self.db.insert("stream_sessions", {
                "id": session_id, "streamer_id": streamer["id"], "platform": src.platform,
                "url": src.url or src.path, "state": "vod", "kind": "vod", "started_at": time.time(), "dir": str(sdir)})
            self._set(jid, streamer_id=streamer["id"], session_id=session_id, duration=duration,
                      title=(meta.get("title") or job["title"] or src.video_id)[:200])

            # 1) audio
            if src.platform == "local":
                audio_path = Path(src.path)
            else:
                self._set(jid, detail="Downloading audio")
                audio_path = self.yt.download_audio(src.url, sdir / "vod_audio", rate_limit=self._rate_limit(),
                                                    progress=lambda f: self._progress(jid, 0.30 * f), cancel=cancel)
            if not duration:
                duration = self._probe_duration(audio_path)
                self._set(jid, duration=duration)

            # 2) chat replay (YouTube keeps it with the VOD)
            chat: list = []
            if src.platform == "youtube":
                self._set(jid, detail="Downloading chat replay")
                cj = self.yt.download_live_chat(src.url, sdir / "chat")
                if cj:
                    chat = parse_youtube_live_chat(cj)
                    self._store_chat(session_id, streamer["id"], chat)

            # 3) detection
            self._set(jid, state="analyzing", progress=0.30,
                      detail=f"Finding moments ({'chat + ' if chat else ''}voice, sound, speech)")
            moments, stats = self._analyze(jid, streamer, session_id, audio_path, duration, chat, cancel)
            self._set(jid, moments=len(moments))
            log.info("VOD %s: %d moments (%s)", jid, len(moments), stats)

            # 4) only the moments' video
            keep = self._select(moments, streamer, meta, job)
            self._set(jid, state="cutting", progress=0.85, detail=f"Downloading video of {len(keep)} moments")
            made = 0
            sess = _VodSession(streamer, session_id, duration or 0.0)
            for i, m in enumerate(keep):
                if cancel.is_set():
                    raise Cancelled("cancelled")
                try:
                    self._make_clip(src, sess, sdir, i, m, cancel)
                    made += 1
                except (YtDlpError, MediaError) as e:
                    log.warning("VOD %s: moment at %.0fs skipped: %s", jid, m.t_event, e)
                self._set(jid, clips=made, progress=0.85 + 0.15 * (i + 1) / max(1, len(keep)))
            if src.platform != "local" and not self.ctx.config.vod.keep_audio:
                audio_path.unlink(missing_ok=True)
            self.db.execute("UPDATE stream_sessions SET state='ended', ended_at=? WHERE id=?", (time.time(), session_id))
            self._set(jid, state="done", progress=1.0, finished_at=time.time(), clips=made,
                      detail=f"{made} clip{'s' if made != 1 else ''} sent to Review from {len(moments)} moments")
        except (Cancelled, DlCancelled):
            self._set(jid, state="cancelled", finished_at=time.time(), detail="Cancelled")
            self._end_session(session_id)
        except Exception as e:  # noqa: BLE001 - a VOD failing never affects capture
            log.exception("VOD job %s failed", jid)
            self._set(jid, state="failed", finished_at=time.time(), error=str(e)[:500], detail="Failed")
            self._end_session(session_id)

    def _end_session(self, session_id: str | None) -> None:
        if session_id:
            self.db.execute("UPDATE stream_sessions SET state='ended', ended_at=? WHERE id=?", (time.time(), session_id))

    def _metadata(self, src: VodSource) -> dict:
        if src.platform == "local":
            summ = stream_summary(ffprobe(self.ctx.tools.ffprobe, src.path))
            if not summ.get("width"):
                raise SourceError("That file has no video; reels need the picture")
            return {"title": Path(src.path).stem, "duration": summ.get("duration"), "live_status": "not_live"}
        return self.yt.probe(src.url)

    def _probe_duration(self, path: Path) -> float | None:
        try:
            return stream_summary(ffprobe(self.ctx.tools.ffprobe, path)).get("duration")
        except (MediaError, ValueError, OSError):
            return None

    def _streamer_for(self, job: dict, src: VodSource, meta: dict) -> dict:
        """The job's streamer; else the monitored streamer with this channel; else an archived row
        named after the channel (so its own language/facecam settings can be set later)."""
        if job.get("streamer_id"):
            s = self.db.one("SELECT * FROM streamers WHERE id=?", (job["streamer_id"],))
            if s:
                return s
        chan = (meta.get("uploader_id") or meta.get("channel") or meta.get("uploader") or "").lower()
        if chan and src.platform != "local":
            for s in self.db.all("SELECT * FROM streamers WHERE platform=? ORDER BY priority", (src.platform,)):
                url = s["url"].lower().rstrip("/")
                if url.endswith("/" + chan) or url.endswith("/@" + chan) or s["name"].lower() == chan:
                    return s
        name = meta.get("uploader") or meta.get("channel") or chan or (Path(src.path).stem if src.path else src.video_id)
        base = slugify(f"{name}-vod")
        slug, i = base, 2
        while self.db.scalar("SELECT 1 FROM streamers WHERE slug=?", (slug,)):
            slug, i = f"{base}-{i}", i + 1
        sid = new_id("str_")
        page = {"twitch": f"https://www.twitch.tv/{chan}", "kick": f"https://kick.com/{chan}"}.get(src.platform) \
            or src.url or "https://localhost/"
        slot = 100 + int(self.db.scalar("SELECT COUNT(*) FROM streamers") or 0)
        self.db.insert("streamers", {"id": sid, "slug": slug, "name": name[:60], "platform": src.platform, "url": page,
                                     "slot": slot, "priority": 99, "ai_enabled": 0, "created_at": time.time()})
        return self.db.one("SELECT * FROM streamers WHERE id=?", (sid,))

    def _find_after_stream_vod(self, job: dict) -> str | None:
        p = json.loads(job["payload"] or "{}")
        jid = job["id"]
        ended, since = float(p.get("ended") or 0), float(p.get("live_since") or 0)
        give_up = ended + self.ctx.config.vod.after_stream_wait_s
        listing = archive_listing_url(job["platform"], p.get("channel_url") or "")
        try:
            if listing is None:
                url = p.get("channel_url")                # YouTube watch?v= live link: same link is the VOD
            else:
                entries = self.yt.list_latest(listing, 3)
                want = 0.5 * max(0.0, ended - since)
                url = next((e["url"] for e in entries
                            if e.get("live_status") != "is_live" and (e.get("duration") or 0) >= want), None)
                if url and self.db.one("SELECT 1 FROM vod_jobs WHERE url=? AND id<>? AND state NOT IN ('failed','cancelled')",
                                       (url, jid)):
                    url = None                             # already processed
        except YtDlpError as e:
            url = None
            log.info("past broadcast lookup failed for %s: %s", jid, e)
        if url:
            return url
        if time.time() > give_up:
            self._set(jid, state="failed", finished_at=time.time(), detail="Failed",
                      error="No past broadcast appeared (the streamer may not save VODs). Paste the VOD link instead.")
        else:
            self._set(jid, state="waiting_vod", not_before=time.time() + 300, detail="Waiting for the past broadcast to appear")
        return None

    def _rate_limit(self) -> str | None:
        """yt-dlp --limit-rate: none when nothing is being captured, else a share of what capture leaves."""
        mgr = self.ctx.manager
        if not mgr.sessions:
            return None
        bw = mgr.bandwidth.status()
        v = self.ctx.config.vod
        spare = max(0.0, self.ctx.config.network.link_capacity_mbps - bw["used_mbps"])
        mbps = max(v.min_rate_mbps, spare * v.spare_bandwidth_share)
        return f"{int(mbps * 125)}K"                       # Mbit/s -> KiB/s (approx.)

    def _store_chat(self, session_id: str, streamer_id: str, chat: list) -> None:
        origin = time.time() - 10 * 86400                  # any constant: media_t is what matters
        rows = [(session_id, streamer_id, origin + t, round(t, 2), a[:40], txt[:500], e) for t, a, txt, e in chat]
        for i in range(0, len(rows), 2000):
            self.db.executemany("INSERT INTO chat_events(session_id, streamer_id, wall_t, media_t, author, text, emote_count) "
                                "VALUES(?,?,?,?,?,?,?)", rows[i:i + 2000])

    def _analyze(self, jid: str, streamer: dict, session_id: str, audio_path: Path, duration: float | None,
                 chat: list, cancel: threading.Event) -> tuple[list[VodMoment], dict]:
        ctx = self.ctx
        lang = streamer.get("language") or ctx.config.ai.language
        state = {"speech_ok": True}

        def speech(pcm):
            if not state["speech_ok"]:
                return None
            try:
                res = ctx.transcriber.transcribe_pcm(pcm, streamer_id=streamer["id"], session_id=session_id, language=lang)
            except TranscriptionUnavailable:
                state["speech_ok"] = False
                return None
            finally:
                time.sleep(0.02)                       # let live speech windows take the shared model in between
            if is_tamil(res["text"]):
                res["words"], res["text"] = romanize_words(res["words"]), romanize_text(res["text"])
            return res

        sound = ctx.sound.classify if ctx.sound.status != "unavailable" else None
        rows: list[tuple] = []

        def on_second(t, rms, peak, exc, sp):
            rows.append((session_id, streamer["id"], round(t, 2), round(rms, 2), round(peak, 2), round(exc, 3), round(sp, 2)))
            if len(rows) >= 300:
                ctx.db.executemany("INSERT OR REPLACE INTO audio_analysis VALUES(?,?,?,?,?,?,?)", rows)
                rows.clear()

        an = VodAnalyzer(ctx.config, new_id=lambda: new_id("hype_"), sound=sound, speech=speech, chat=chat,
                         progress=lambda f: self._progress(jid, 0.30 + 0.55 * f), cancel=cancel, on_second=on_second)
        proc = subprocess.Popen([ctx.tools.ffmpeg, "-v", "error", "-i", str(audio_path), "-vn", "-ac", "1", "-ar", "16000",
                                 "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, **popen_flags("low"))

        def chunks():
            assert proc.stdout is not None
            while True:
                b = proc.stdout.read(64000)
                if not b:
                    return
                yield b

        try:
            moments = an.run(chunks(), duration)
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if rows:
                ctx.db.executemany("INSERT OR REPLACE INTO audio_analysis VALUES(?,?,?,?,?,?,?)", rows)
        return moments, an.stats

    def _select(self, moments: list[VodMoment], streamer: dict, meta: dict, job: dict) -> list[VodMoment]:
        """Drop overlaps and — for a stream we were monitoring — moments already caught live."""
        start_wall = meta.get("timestamp") or meta.get("release_timestamp")
        seen = []
        if start_wall:
            seen = [r["created_at"] for r in self.db.all(
                "SELECT h.created_at FROM hype_events h JOIN stream_sessions s ON s.id=h.session_id "
                "WHERE h.streamer_id=? AND COALESCE(s.kind,'live')='live' AND h.created_at BETWEEN ? AND ?",
                (streamer["id"], start_wall - 60, start_wall + (meta.get("duration") or 0) + 600))]
        out: list[VodMoment] = []
        for m in sorted(moments, key=lambda m: m.t_event):
            if out and out[-1].t_start <= m.t_event <= out[-1].t_end - 2:
                continue
            if start_wall and any(-30 <= (h - (start_wall + m.t_event)) <= 60 for h in seen):
                log.info("VOD moment at %.0fs was already caught live; skipped", m.t_event)
                continue
            out.append(m)
        return out

    def _make_clip(self, src: VodSource, sess: _VodSession, sdir: Path, i: int, m: VodMoment, cancel) -> str:
        s, e = max(0.0, m.t_start), m.t_end
        out = sdir / f"vod_{i:03d}.mp4"
        if src.platform == "local":
            run([self.ctx.tools.ffmpeg, "-y", "-v", "error", "-ss", f"{s:.3f}", "-i", src.path, "-t", f"{e - s:.3f}",
                 "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero", str(out)], 600, "low")
        else:
            hls = self._hls_url(src)
            done = False
            if hls:
                try:
                    rl = self._rate_limit()
                    self.yt.download_section_hls(hls, s, e, out, cancel=cancel,
                                                 rate_bps=int(rl[:-1]) * 1024 if rl else None)
                    done = True
                except DlCancelled:
                    raise
                except Exception as ex:  # noqa: BLE001 - fall back to yt-dlp's own section download
                    log.info("fast section download failed (%s); using yt-dlp", ex)
                    self._hls_cache.pop(src.url, None)
            if not done:
                self.yt.download_section(src.url, s, e, out, max_height=self.ctx.config.vod.max_height,
                                         rate_limit=self._rate_limit(), cancel=cancel)
        summ = stream_summary(ffprobe(self.ctx.tools.ffprobe, out))
        dur = float(summ.get("duration") or (e - s))
        t0 = max(0.0, e - dur)                         # a stream-copy cut starts at the keyframe before `s`
        st = sess.streamer
        self.db.execute("INSERT INTO video_segments(session_id, streamer_id, seq, run, path, t_start, t_end, duration, size, created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (sess.session_id, st["id"], i, i, str(out), t0, t0 + dur, dur, out.stat().st_size, time.time()))
        self.db.insert("hype_events", {
            "id": m.id, "session_id": sess.session_id, "streamer_id": st["id"], "kind": "auto", "t_event": m.t_event,
            "t_start": s, "t_end": e, "peak_score": m.peak, "audio_score": m.audio, "chat_score": m.chat,
            "state": "finalizing", "reason": "Found in past broadcast", "signals": json.dumps(m.signals),
            "created_at": time.time()})
        return self.ctx.clips.create_clip(sess, t0, t0 + dur, "auto", m.id, t_event=m.t_event)

    def _hls_url(self, src: VodSource) -> str | None:
        """Signed playlist URLs expire; re-resolve after 20 minutes."""
        if src.platform not in ("twitch", "kick"):
            return None
        hit = self._hls_cache.get(src.url)
        if hit and time.time() - hit[1] < 1200:
            return hit[0]
        try:
            u = self.yt.stream_url(src.url, self.ctx.config.vod.max_height)
        except Exception:  # noqa: BLE001
            u = None
        self._hls_cache[src.url] = (u, time.time())
        return u

    # --- state ------------------------------------------------------------------
    def _progress(self, jid: str, f: float) -> None:
        self._set(jid, progress=round(max(0.0, min(1.0, f)), 3))

    def _set(self, jid: str, **fields) -> None:
        self.db.update("vod_jobs", "id", jid, fields)
        self._publish(jid)

    def _publish(self, jid: str) -> None:
        v = self.get(jid)
        if v:
            self.ctx.hub.publish("vod", v)

    def _view(self, r: dict) -> dict:
        r = dict(r)
        r["payload"] = None
        if r.get("streamer_id"):
            s = self.db.one("SELECT name, color FROM streamers WHERE id=?", (r["streamer_id"],))
            r["streamer_name"], r["streamer_color"] = (s["name"], s["color"]) if s else (None, None)
        return r
