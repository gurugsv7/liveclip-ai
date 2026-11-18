"""Stream Manager: owns every live session and wires its independent
pipeline (capture worker, audio analyzer, chat client + analyzer, hype
detector). Nothing in a LiveSession is shared with another session except the
central job queue, and every job carries immutable streamer/session ids."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path

from ..ai.tanglish import is_tamil, romanize_text, romanize_words
from ..ai.scoring import SpeechNormalizer, live_speech_raw
from ..ai.transcriber import TranscriptionUnavailable
from ..detection.sound_events import apply_sound
from ..db import new_id
from ..detection.audio import AudioAnalyzer
from ..detection.chat import ChatAnalyzer
from ..detection.hype import Candidate, HypeDetector, HypeParams
from ..platforms import SourceError, SourceInfo, resolve_source, slugify
from ..platforms.chat import make_chat_client
from ..platforms.twitch_clips import TwitchClipWatcher
from .worker import CaptureWorker, SegmentInfo

log = logging.getLogger("liveclip.manager")

PALETTE = ["#8b5cf6", "#22d3ee", "#f472b6", "#a3e635"]


class LiveSession:
    def __init__(self, mgr: "StreamManager", streamer: dict, source: SourceInfo, session_id: str, seg_dir: Path):
        self.mgr = mgr
        self.ctx = mgr.ctx
        self.streamer = streamer
        self.streamer_id = streamer["id"]
        self.source = source
        self.session_id = session_id
        self.seg_dir = seg_dir
        cfg = self.ctx.config
        self.audio = AudioAnalyzer()
        self.chat = ChatAnalyzer(cfg.detection.keywords, cfg.detection.laugh_tokens)
        self.origin_wall: float | None = None
        self.state = "starting"
        self.detail = ""
        self.started_wall = time.time()
        self.live_since: float | None = None
        self.measured: deque[tuple[float, int]] = deque(maxlen=12)  # (duration, bytes)
        self.probe: dict = {}
        self.ai_enabled = bool(streamer["ai_enabled"])
        self.events_recent: deque[dict] = deque(maxlen=30)
        self._audio_rows: list[tuple] = []
        self._chat_rows: list[tuple] = []
        d = cfg.detection
        c = cfg.clips
        self.detector = HypeDetector(
            HypeParams(trigger=d.trigger_threshold, release=d.release_threshold, merge_window_s=d.merge_window_s,
                       cooldown_s=d.cooldown_s, pre_s=c.pre_context_s, post_s=c.post_context_s,
                       max_wait_s=c.max_reaction_wait_s, max_clip_s=c.max_clip_s, min_baseline_s=d.min_baseline_s,
                       w_audio=d.lightweight_audio_weight, w_chat=d.lightweight_chat_weight,
                       single_signal_factor=d.single_signal_factor, chat_delay_s=d.chat_reaction_delay_s),
            new_id=lambda: new_id("hype_"), on_trigger=self._on_trigger, on_extend=self._on_extend,
            on_finalize=self._on_finalize)
        self.detector.enabled = self.ai_enabled
        self.protections: dict[str, str] = {}  # candidate id -> protection id
        self.speech: dict | None = None          # latest live speech window result (this stream only)
        self.speech_norm = SpeechNormalizer()
        self.speech_last_run = 0.0
        self.sound: dict | None = None           # latest YAMNet window (this stream only)
        # viewers' own Twitch clips for this channel (needs TWITCH_CLIENT_ID/SECRET)
        self.viewer_clips = TwitchClipWatcher(source.channel, self._on_viewer_clip) if source.platform == "twitch" else None
        self.audio.on_second = self._on_audio_second
        self.chat_client = make_chat_client(source.platform, source.channel, source.url, self._on_chat,
                                            local_chat_path=source.local_chat_path,
                                            loop_s=self._fixture_duration(), start_wall=lambda: self.origin_wall)
        self.quality = mgr.bandwidth.choose_quality(self)
        tools = self.ctx.tools
        self.worker = CaptureWorker(
            session_id=session_id, streamer_id=self.streamer_id, platform=source.platform, url=source.url,
            local_path=source.local_path, seg_dir=seg_dir, ffmpeg=tools.ffmpeg, ffprobe_bin=tools.ffprobe,
            streamlink=tools.streamlink, quality=self.quality, segment_seconds=cfg.buffer.segment_seconds,
            reconnect_base=cfg.network.reconnect_base_s, reconnect_max=cfg.network.reconnect_max_s,
            max_attempts=cfg.network.reconnect_max_attempts, audio_feed=self.audio.feed,
            on_run_start=self._on_run_start, on_segment=self._on_segment, on_state=self._on_state,
            on_probe=self._on_probe, media_clock=self._media_clock)

    def _fixture_duration(self) -> float | None:
        if self.source.platform != "local" or not self.source.local_path:
            return None
        try:
            from ..media import ffprobe, stream_summary
            return stream_summary(ffprobe(self.ctx.tools.ffprobe, self.source.local_path)).get("duration")
        except Exception:
            return None

    # --- clocks -------------------------------------------------------------
    def _media_clock(self) -> float:
        if self.origin_wall is None:
            self.origin_wall = time.time()
        return time.time() - self.origin_wall

    def media_now(self) -> float:
        """Newest captured media time (end of the latest completed segment, or audio clock)."""
        t = self.ctx.db.scalar("SELECT MAX(t_end) FROM video_segments WHERE session_id=?", (self.session_id,)) or 0.0
        return max(t, self.audio.latest_t() if self.state == "live" else 0.0)

    def chat_to_media(self, wall: float) -> float:
        return (wall - self.origin_wall) if self.origin_wall else 0.0

    def is_capturing(self) -> bool:
        return self.worker.is_alive() and self.state in ("live", "connecting", "reconnecting", "starting")

    # --- worker callbacks (worker thread) ---------------------------------
    def _on_run_start(self, run: int, media_offset: float) -> None:
        self.audio.start_run(media_offset)
        if self.live_since is None:
            self.live_since = time.time()

    def _on_segment(self, s: SegmentInfo) -> None:
        db = self.ctx.db
        db.execute("INSERT OR IGNORE INTO video_segments(session_id, streamer_id, seq, run, path, t_start, t_end, duration, size, created_at) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (self.session_id, self.streamer_id, s.seq, s.run, s.path, s.t_start, s.t_end, s.t_end - s.t_start,
                    s.size, time.time()))
        if s.t_end > s.t_start and s.size:
            self.measured.append((s.t_end - s.t_start, s.size))
        kbps = self.measured_kbps()
        db.execute("UPDATE stream_sessions SET measured_kbps=? WHERE id=?", (kbps, self.session_id))
        self.ctx.hub.publish("segment", {"streamer_id": self.streamer_id, "session_id": self.session_id, "seq": s.seq,
                                         "t_start": s.t_start, "t_end": s.t_end, "size": s.size})

    def _on_state(self, state: str, detail: str) -> None:
        if state != self.state or detail != self.detail:
            log.info("[%s] %s -> %s %s", self.streamer["name"], self.session_id, state, detail)
        self.state, self.detail = state, detail
        self.ctx.db.execute("UPDATE stream_sessions SET state=?, error=?, reconnects=? WHERE id=?",
                            (state, detail or None, self.worker.reconnects if hasattr(self, "worker") else 0, self.session_id))
        self.ctx.hub.publish("stream_state", {"streamer_id": self.streamer_id, "session_id": self.session_id,
                                              "state": state, "detail": detail}, notable=state in ("failed", "offline", "reconnecting"))
        if state in ("failed",):
            self.ctx.hub.warn(f"stream:{self.streamer_id}", "error", f"{self.streamer['name']}: {detail}")
        elif state == "live":
            self.ctx.hub.clear_warning(f"stream:{self.streamer_id}")

    def _on_probe(self, summ: dict) -> None:
        self.probe = summ
        log.info("[%s] source %sx%s %s fps %s/%s, quality %s", self.streamer["name"], summ.get("width"), summ.get("height"),
                 summ.get("fps"), summ.get("video_codec"), summ.get("audio_codec"), summ.get("quality_selected"))
        self.ctx.db.update("stream_sessions", "id", self.session_id, {
            "source_width": summ.get("width"), "source_height": summ.get("height"), "fps": summ.get("fps"),
            "video_codec": summ.get("video_codec"), "audio_codec": summ.get("audio_codec"),
            "audio_rate": summ.get("audio_rate"), "audio_channels": summ.get("audio_channels"),
            "quality_selected": summ.get("quality_selected") or self.worker.selected_quality})
        if summ.get("video_codec") not in (None, "h264"):
            self.ctx.hub.warn(f"codec:{self.streamer_id}", "warn",
                              f"{self.streamer['name']}: source codec {summ.get('video_codec')} may not play in the browser preview")

    def _on_audio_second(self, t: float, rms: float, peak: float, exc: float, speech: float) -> None:
        self._audio_rows.append((self.session_id, self.streamer_id, round(t, 2), round(rms, 2), round(peak, 2),
                                 round(exc, 3), round(speech, 2)))
        if len(self._audio_rows) >= 10:
            rows, self._audio_rows = self._audio_rows, []
            self.ctx.db.executemany("INSERT OR REPLACE INTO audio_analysis VALUES(?,?,?,?,?,?,?)", rows)

    # --- chat (event loop) ---------------------------------------------------
    def _on_chat(self, author: str, text: str, emotes: int, wall: float) -> None:
        self.chat.add(author, text, emotes, wall)
        self._chat_rows.append((self.session_id, self.streamer_id, wall, round(self.chat_to_media(wall), 2),
                                author[:40], text[:500], emotes))
        if len(self._chat_rows) >= 20:
            self.flush_chat()

    def flush_chat(self) -> None:
        if self._chat_rows:
            rows, self._chat_rows = self._chat_rows, []
            self.ctx.db.executemany("INSERT INTO chat_events(session_id, streamer_id, wall_t, media_t, author, text, emote_count) "
                                    "VALUES(?,?,?,?,?,?,?)", rows)

    # --- viewer clips (event loop) -------------------------------------------
    def _on_viewer_clip(self, vc: dict) -> None:
        """A viewer clipped this stream: save that moment from our own buffer.
        The viewer's clip ends at `created_wall` and covers ~`duration` s before it."""
        if not self.origin_wall:
            return
        end = vc["created_wall"] - self.origin_wall + 3.0
        start = end - max(30.0, vc["duration"]) - 8.0
        oldest = self.ctx.db.scalar("SELECT MIN(t_start) FROM video_segments WHERE session_id=? AND deleted=0", (self.session_id,))
        if oldest is None or end < oldest + 5:
            log.warning("[%s] viewer clip %s arrived too late; footage already rotated out", self.streamer["name"], vc["id"])
            return
        start = max(start, oldest)
        overlap = self.ctx.db.one("SELECT id, status FROM clips WHERE session_id=? AND t_end > ? AND t_start < ? "
                                  "AND status NOT IN ('discarded','failed') ORDER BY created_at DESC LIMIT 1",
                                  (self.session_id, start + 5, end - 5))
        if overlap:
            # we already have this moment: viewers confirm it (and rescue it from the Maybe pool)
            self.ctx.clips.confirm_by_viewers(overlap["id"], vc)
            return
        ev_id = new_id("hype_")
        self.ctx.db.insert("hype_events", {
            "id": ev_id, "session_id": self.session_id, "streamer_id": self.streamer_id, "kind": "viewer_clip",
            "t_event": end - 6, "t_start": start, "t_end": end, "state": "clipped",
            "reason": f"Viewer clip by {vc['creator']}: {vc['title']}"[:300], "signals": json.dumps({"viewer_clip": vc}),
            "created_at": time.time()})
        log.info("[%s] viewer clip by %s (%s) -> saving %.0f-%.0fs", self.streamer["name"], vc["creator"], vc["title"], start, end)
        self.ctx.hub.publish("hype", self.ctx.db.one("SELECT * FROM hype_events WHERE id=?", (ev_id,)), notable=True)
        self.ctx.clips.create_clip(self, start, end, "viewer_clip", ev_id, t_event=end - 6)

    # --- hype detector callbacks ----------------------------------------
    def _on_trigger(self, c: Candidate, p0: float, p1: float) -> None:
        log.info("[%s] hype trigger at %.1fs (score %.2f, audio %.2f, chat %.2f, speech %.2f)", self.streamer["name"],
                 c.t_event, c.peak, c.audio, c.chat, c.speech)
        pid = self.ctx.retention.protect(self.session_id, self.streamer_id, p0, p1, "hype_candidate", "hype", c.id)
        self.protections[c.id] = pid
        ev = {"id": c.id, "session_id": self.session_id, "streamer_id": self.streamer_id, "kind": "auto",
              "t_event": c.t_event, "t_start": p0, "t_end": p1, "peak_score": c.peak, "audio_score": c.audio,
              "chat_score": c.chat if self.chat.available else None, "state": "open",
              "signals": json.dumps({**c.signals, "chat_available": self.chat.available}), "created_at": time.time()}
        self.ctx.db.insert("hype_events", ev)
        self.events_recent.append(ev)
        self.ctx.hub.publish("hype", ev, notable=True)

    def _on_extend(self, c: Candidate, p0: float, p1: float) -> None:
        pid = self.protections.get(c.id)
        if pid:
            self.ctx.retention.update_protection(pid, p0, p1)
        self.ctx.db.execute("UPDATE hype_events SET peak_score=?, audio_score=?, chat_score=?, merged_count=?, t_event=? WHERE id=?",
                            (c.peak, c.audio, c.chat if self.chat.available else None, c.merged, c.t_event, c.id))

    def _on_finalize(self, c: Candidate, s: float, e: float) -> None:
        log.info("[%s] hype finalized %.1f-%.1fs (peak %.2f, merged %d)", self.streamer["name"], s, e, c.peak, c.merged)
        self.ctx.db.execute("UPDATE hype_events SET state='finalizing', t_start=?, t_end=?, peak_score=?, merged_count=? WHERE id=?",
                            (s, e, c.peak, c.merged, c.id))
        pid = self.protections.pop(c.id, None)
        self.ctx.clips.create_clip(self, s, e, "auto", c.id, protection_id=pid, t_event=c.t_event)

    def detection_tick(self) -> dict:
        now_media = self.audio.latest_t()
        d = self.ctx.config.detection
        a, a_t = self.audio.excitement(now_media, window=2.0 + d.chat_reaction_delay_s)
        snd = self.sound
        a, a_t, react = apply_sound(a, a_t, snd, now_media, d.game_noise_threshold, d.game_noise_discount)
        cs = self.chat.score()
        chat_val = cs["score"] if self.chat.available else None
        if chat_val is not None and cs.get("clip_calls", 0) >= d.crowd_clip_calls:
            chat_val = max(chat_val, 0.9)       # several different viewers asked for a clip
        sp, speech, speech_t = self.speech, 0.0, None
        if sp and now_media - sp["t_end"] <= d.live_speech_interval_s + 4:   # hold the latest window briefly
            speech, speech_t = sp["score"], sp["peak_t"]
        score = self.detector.tick(now_media, a, a_t, chat_val, self.audio.voiced_seconds,
                                   extra={"chat": cs, "audio_db": self.audio.level_db(),
                                          "speech": {k: sp[k] for k in ("score", "raw", "hits", "text")} if speech else None},
                                   speech=speech, speech_peak_t=speech_t)
        return {"audio": round(a, 3), "chat": chat_val, "chat_rate": cs["rate"], "speech": round(speech, 3),
                "reaction": round(react, 3), "sound_top": snd["top"] if snd else None,
                "game_noise": round(snd["game"], 2) if snd else None, "clip_calls": cs.get("clip_calls", 0),
                "speech_text": sp["text"] if sp else None, "score": round(score, 3),
                "level_db": round(self.audio.level_db(), 1), "open": self.detector.open is not None,
                "suppressed": self.detector.suppressed}

    def measured_kbps(self) -> float | None:
        if not self.measured:
            return None
        d = sum(x[0] for x in self.measured)
        b = sum(x[1] for x in self.measured)
        return round(b * 8 / d / 1000, 1) if d > 0 else None

    def status(self) -> dict:
        buf = self.ctx.retention.buffer_status(self.session_id)
        return {
            "streamer_id": self.streamer_id, "session_id": self.session_id, "state": self.state, "detail": self.detail,
            "platform": self.source.platform, "url": self.source.url,
            "width": self.probe.get("width"), "height": self.probe.get("height"), "fps": self.probe.get("fps"),
            "video_codec": self.probe.get("video_codec"), "audio_codec": self.probe.get("audio_codec"),
            "audio_rate": self.probe.get("audio_rate"), "measured_kbps": self.measured_kbps(),
            "quality_requested": self.quality, "quality_selected": self.worker.selected_quality or self.probe.get("quality_selected"),
            "reconnects": self.worker.reconnects, "pids": self.worker.pids,
            "recording_s": (time.time() - self.live_since) if self.live_since else 0, "media_now": self.media_now(),
            "buffer": buf, "ai_enabled": self.ai_enabled, "chat": self.chat_client.info(),
            "viewer_clips": self.viewer_clips.info() if self.viewer_clips else {"status": "unavailable", "detail": "Twitch only"},
            "chat_messages": self.chat.total,
        }


class BandwidthManager:
    """Chooses per-stream quality so the sum of capture bitrates stays inside
    the configured budget. Higher-priority streamers (priority 1) get full
    quality first. Uses measured bitrates once available."""

    def __init__(self, mgr: "StreamManager"):
        self.mgr = mgr

    def budget_mbps(self) -> float:
        n = self.mgr.ctx.config.network
        return n.link_capacity_mbps * n.safety_fraction

    def used_mbps(self, exclude: str | None = None) -> float:
        n = self.mgr.ctx.config.network
        total = 0.0
        for sid, live in self.mgr.sessions.items():
            if sid == exclude:
                continue
            k = live.measured_kbps()
            if k:
                total += k / 1000
            else:
                q = live.quality[0] if live.quality else "best"
                total += n.quality_estimates_mbps.get(q, 8.0)
        return total

    def choose_quality(self, live: LiveSession) -> list[str]:
        n = self.mgr.ctx.config.network
        if live.source.platform == "local":
            return ["source"]
        pref = n.preferred_quality
        est = n.quality_estimates_mbps.get(pref[0], 8.0)
        used = self.used_mbps(exclude=live.streamer_id)
        # does a lower-priority stream already hold full quality? (priority wins at next reconnect)
        if used + est <= self.budget_mbps() or live.streamer["priority"] <= 1:
            return pref
        return n.reduced_quality

    def status(self) -> dict:
        used = self.used_mbps()
        b = self.budget_mbps()
        return {"used_mbps": round(used, 2), "budget_mbps": round(b, 2), "over_budget": used > b}


class StreamManager:
    def __init__(self, ctx):
        self.ctx = ctx
        self.sessions: dict[str, LiveSession] = {}   # streamer_id -> live session
        self.bandwidth = BandwidthManager(self)
        self._tasks: list[asyncio.Task] = []
        self.meters: dict[str, dict] = {}

    # --- streamers ------------------------------------------------------
    def list_streamers(self) -> list[dict]:
        rows = self.ctx.db.all("SELECT * FROM streamers ORDER BY slot, created_at")
        for r in rows:
            r["facecam"] = json.loads(r["facecam"]) if r["facecam"] else None
            r["ai_enabled"] = bool(r["ai_enabled"])
        return rows

    def add_streamer(self, name: str, url: str, slot: int | None = None, priority: int = 2) -> dict:
        cfg = self.ctx.config
        src = resolve_source(url, allow_local=cfg.allow_local_sources, fixtures_dir=self.ctx.fixtures_dir)
        if self.ctx.db.scalar("SELECT COUNT(*) FROM streamers WHERE priority < 99") >= 4:
            raise SourceError("Up to four streamers can be monitored. Remove one first.")
        base = slugify(name or src.channel)
        slug, i = base, 2
        while self.ctx.db.scalar("SELECT 1 FROM streamers WHERE slug=?", (slug,)):
            slug, i = f"{base}-{i}", i + 1
        used = {r["slot"] for r in self.ctx.db.all("SELECT slot FROM streamers")}
        if slot is None or slot in used:
            slot = next(s for s in range(4) if s not in used)
        sid = new_id("str_")
        self.ctx.db.insert("streamers", {
            "id": sid, "slug": slug, "name": name or src.channel, "platform": src.platform, "url": src.url, "slot": slot,
            "priority": priority, "color": PALETTE[slot % 4], "ai_enabled": 1, "created_at": time.time()})
        return self.get_streamer(sid)

    def get_streamer(self, sid: str) -> dict | None:
        r = self.ctx.db.one("SELECT * FROM streamers WHERE id=?", (sid,))
        if r:
            r["facecam"] = json.loads(r["facecam"]) if r["facecam"] else None
            r["ai_enabled"] = bool(r["ai_enabled"])
        return r

    def update_streamer(self, sid: str, fields: dict) -> dict:
        allowed = {"name", "priority", "color", "facecam", "default_layout", "usage_notes", "slot", "language", "caption_mode"}
        f = {k: v for k, v in fields.items() if k in allowed}
        if "facecam" in f and f["facecam"] is not None:
            fc = f["facecam"]
            if not all(0 <= float(fc[k]) <= 1 for k in ("x", "y", "w", "h")) or fc["w"] <= 0.02 or fc["h"] <= 0.02:
                raise ValueError("facecam must be a normalized rect {x,y,w,h} within 0..1")
            f["facecam"] = json.dumps({k: float(fc[k]) for k in ("x", "y", "w", "h")})
        self.ctx.db.update("streamers", "id", sid, f)
        return self.get_streamer(sid)

    def move_streamer(self, sid: str, slot: int) -> list[dict]:
        """Move a stream to dashboard slot 0-3; a stream already there swaps into the old slot.
        Only the on-screen position changes; capture, sessions and files are untouched."""
        if not 0 <= slot <= 3:
            raise ValueError("slot must be 0-3")
        me = self.get_streamer(sid)
        if not me or me["slot"] > 3:
            raise KeyError(f"unknown stream {sid}")
        old = me["slot"]
        with self.ctx.db.transaction() as db:
            other = db._conn.execute("SELECT id FROM streamers WHERE slot=? AND id<>?", (slot, sid)).fetchone()
            if other:
                db._conn.execute("UPDATE streamers SET slot=? WHERE id=?", (old, other[0]))
            db._conn.execute("UPDATE streamers SET slot=? WHERE id=?", (slot, sid))
        for live in self.sessions.values():          # keep cached rows in step (capture priority tiebreak)
            live.streamer["slot"] = self.get_streamer(live.streamer["id"])["slot"]
        return self.list_streamers()

    async def remove_streamer(self, sid: str) -> None:
        await self.stop(sid)
        # Clips are kept: re-parent is impossible (FK), so we keep the streamer row
        # if clips reference it; otherwise delete.
        if self.ctx.db.scalar("SELECT COUNT(*) FROM clips WHERE streamer_id=?", (sid,)):
            self.ctx.db.execute("UPDATE streamers SET slot=100+slot WHERE id=?", (sid,))
            self.ctx.db.execute("UPDATE streamers SET slug=slug||'-archived-'||? WHERE id=?", (int(time.time()), sid))
            self.ctx.db.execute("UPDATE streamers SET url=url, ai_enabled=0 WHERE id=?", (sid,))
            self.ctx.db.execute("UPDATE streamers SET priority=99 WHERE id=?", (sid,))
        else:
            self.ctx.db.execute("DELETE FROM streamers WHERE id=?", (sid,))

    # --- sessions -------------------------------------------------------
    def live_by_session(self, session_id: str) -> LiveSession | None:
        for s in self.sessions.values():
            if s.session_id == session_id:
                return s
        return None

    async def start(self, streamer_id: str) -> dict:
        if streamer_id in self.sessions:
            return self.sessions[streamer_id].status()
        st = self.get_streamer(streamer_id)
        if not st:
            raise KeyError(streamer_id)
        if st["priority"] >= 99:
            raise SourceError("This streamer was removed; add it again to monitor")
        cfg = self.ctx.config
        src = resolve_source(st["url"], allow_local=cfg.allow_local_sources, fixtures_dir=self.ctx.fixtures_dir)
        if not self.ctx.tools.ffmpeg:
            raise SourceError("FFmpeg not found. Run scripts/setup.ps1 or set LIVECLIP_FFMPEG.")
        disk = self.ctx.retention.disk_status()
        if disk["level"] == "critical":
            raise SourceError("Disk space critically low; free space before starting capture")
        session_id = new_id("ses_")
        sdir = self.ctx.paths.session_dir(st["slug"], session_id)
        seg_dir = self.ctx.paths.segments_dir(st["slug"], session_id)
        self.ctx.db.insert("stream_sessions", {
            "id": session_id, "streamer_id": streamer_id, "platform": src.platform, "url": src.url, "state": "starting",
            "started_at": time.time(), "dir": str(sdir)})
        live = LiveSession(self, st, src, session_id, seg_dir)
        self.ctx.db.execute("UPDATE stream_sessions SET quality_requested=? WHERE id=?", (",".join(live.quality), session_id))
        self.sessions[streamer_id] = live
        self.ctx.retention.active_sessions[session_id] = {"next_seq": lambda: live.worker.next_seq}
        live.worker.start()
        live.chat_client.start()
        if live.viewer_clips:
            live.viewer_clips.start()
        self.ctx.db.execute("UPDATE streamers SET monitoring=1 WHERE id=?", (streamer_id,))
        self.ctx.hub.publish("session_started", live.status(), notable=True)
        return live.status()

    async def resume_monitoring(self) -> dict[str, str]:
        """Start again every stream that was being monitored when the app last stopped.
        One stream failing to start never blocks the others."""
        out: dict[str, str] = {}
        rows = self.ctx.db.all("SELECT id FROM streamers WHERE monitoring=1 AND priority<99 ORDER BY slot")
        for r in rows:
            try:
                await self.start(r["id"])
                out[r["id"]] = "resumed"
            except Exception as e:  # noqa: BLE001 - keep going; the stream shows its own error
                out[r["id"]] = str(e)
                self.ctx.hub.warn(f"resume:{r['id']}", "warn", f"Couldn't resume monitoring: {e}")
        return out

    async def stop(self, streamer_id: str, *, user: bool = True) -> None:
        """Stop policy: an open hype candidate is finalized with the footage
        already captured; queued extractions continue from the existing
        segments (they stay protected); unprotected segments are then removed."""
        if user:  # an app shutdown keeps the flag so the stream resumes on next start
            self.ctx.db.execute("UPDATE streamers SET monitoring=0 WHERE id=?", (streamer_id,))
        live = self.sessions.pop(streamer_id, None)
        if not live:
            return
        live.detector.force_finalize(live.audio.latest_t())
        live.worker.stop()
        await live.chat_client.stop()
        if live.viewer_clips:
            await live.viewer_clips.stop()
        await asyncio.to_thread(live.worker.join, 10)
        live.flush_chat()
        self.ctx.db.execute("UPDATE stream_sessions SET state='stopped', ended_at=? WHERE id=?", (time.time(), live.session_id))
        self.ctx.retention.active_sessions.pop(live.session_id, None)
        self.meters.pop(streamer_id, None)
        self.ctx.hub.publish("session_stopped", {"streamer_id": streamer_id, "session_id": live.session_id}, notable=True)

    async def replace(self, streamer_id: str, url: str, name: str | None = None) -> dict:
        cfg = self.ctx.config
        src = resolve_source(url, allow_local=cfg.allow_local_sources, fixtures_dir=self.ctx.fixtures_dir)
        was_running = streamer_id in self.sessions
        await self.stop(streamer_id)
        fields = {"url": src.url, "platform": src.platform}
        if name:
            fields["name"] = name
        self.ctx.db.update("streamers", "id", streamer_id, fields)
        self.ctx.db.execute("UPDATE streamers SET facecam=NULL WHERE id=?", (streamer_id,))
        if was_running:
            return await self.start(streamer_id)
        return {"streamer_id": streamer_id, "state": "idle"}

    def set_ai(self, streamer_id: str, enabled: bool) -> None:
        self.ctx.db.execute("UPDATE streamers SET ai_enabled=? WHERE id=?", (1 if enabled else 0, streamer_id))
        live = self.sessions.get(streamer_id)
        if live:
            live.ai_enabled = enabled
            live.detector.enabled = enabled   # recording is unaffected

    # --- manual actions ---------------------------------------------------
    def save_last(self, streamer_id: str, seconds: float, latency_s: float = 0.0) -> str:
        live = self._live(streamer_id)
        end = live.media_now() - max(0.0, latency_s) + 1.0
        start = end - seconds
        oldest = self.ctx.db.scalar("SELECT MIN(t_start) FROM video_segments WHERE session_id=? AND deleted=0", (live.session_id,))
        if oldest is not None:
            start = max(start, oldest)
        ev_id = new_id("hype_")
        self.ctx.db.insert("hype_events", {
            "id": ev_id, "session_id": live.session_id, "streamer_id": streamer_id, "kind": "manual_save",
            "t_event": end, "t_start": start, "t_end": end, "state": "clipped", "reason": f"Manual save last {seconds:g}s",
            "created_at": time.time()})
        self.ctx.hub.publish("hype", self.ctx.db.one("SELECT * FROM hype_events WHERE id=?", (ev_id,)), notable=True)
        return self.ctx.clips.create_clip(live, start, end, "manual_save", ev_id, t_event=end - 3)

    def mark(self, streamer_id: str, latency_s: float = 0.0, analyze: bool = True) -> str:
        live = self._live(streamer_id)
        cfg = self.ctx.config.clips
        t = max(0.0, live.media_now() - max(0.0, latency_s))
        ev_id = new_id("hype_")
        self.ctx.db.insert("hype_events", {
            "id": ev_id, "session_id": live.session_id, "streamer_id": streamer_id, "kind": "mark", "t_event": t,
            "t_start": max(0, t - cfg.pre_context_s), "t_end": t + cfg.post_context_s, "state": "clipped",
            "reason": "Manual mark", "audio_score": live.audio.excitement(t, 3)[0],
            "chat_score": live.chat.score()["score"] if live.chat.available else None, "created_at": time.time()})
        self.ctx.hub.publish("hype", self.ctx.db.one("SELECT * FROM hype_events WHERE id=?", (ev_id,)), notable=True)
        return self.ctx.clips.create_clip(live, t - cfg.pre_context_s, t + cfg.post_context_s, "mark", ev_id, t_event=t)

    def delete_buffer(self, streamer_id: str) -> dict:
        live = self._live(streamer_id)
        return self.ctx.retention.delete_unprotected(live.session_id)

    def _live(self, streamer_id: str) -> LiveSession:
        live = self.sessions.get(streamer_id)
        if not live:
            raise KeyError("Stream is not being monitored")
        return live

    # --- loops ------------------------------------------------------------
    def start_loops(self) -> None:
        self._tasks.append(asyncio.create_task(self._detect_loop(), name="detect-loop"))
        self._tasks.append(asyncio.create_task(self._status_loop(), name="status-loop"))
        self._tasks.append(asyncio.create_task(self._speech_loop(), name="speech-loop"))
        self._tasks.append(asyncio.create_task(self._sound_loop(), name="sound-loop"))

    async def stop_all(self) -> None:
        for t in self._tasks:
            t.cancel()
        for sid in list(self.sessions):
            await self.stop(sid, user=False)

    async def _detect_loop(self) -> None:
        while True:
            await asyncio.sleep(0.5)
            for sid, live in list(self.sessions.items()):
                if live.state != "live":
                    continue
                try:
                    m = live.detection_tick()
                    self.meters[sid] = m
                except Exception:
                    log.exception("detection tick failed for %s", sid)
            if self.meters:
                self.ctx.hub.publish("meters", self.meters)

    async def _sound_loop(self) -> None:
        """Every second, classify the last 2.5 s of each stream's own audio with YAMNet
        (CPU, a few ms per stream). Missing model => silently skipped."""
        while True:
            await asyncio.sleep(1.0)
            if not self.ctx.config.detection.sound_events_enabled or self.ctx.sound.status == "unavailable":
                continue
            for live in list(self.sessions.values()):
                if live.state != "live" or not live.ai_enabled:
                    continue
                t1 = live.audio.latest_t()
                t0 = t1 - 2.5
                pcm = live.audio.get_pcm(t0, t1)
                try:
                    res = await asyncio.to_thread(self.ctx.sound.classify, pcm)
                except Exception:
                    log.exception("sound classification failed for %s", live.streamer["name"])
                    continue
                if res is None or self.sessions.get(live.streamer_id) is not live:
                    continue
                live.sound = {**res, "t_end": t1, "peak_t": t0 + res["peak_offset"]}
                if res["reaction"] >= 0.5:
                    log.info("[%s] sound reaction %.2f (%s, laugh %.2f, scream %.2f, game %.2f)", live.streamer["name"],
                             res["reaction"], res["top"], res["laugh"], res["scream"], res["game"])

    async def _speech_loop(self) -> None:
        """Round-robin live speech windows across streams, one at a time, on the shared
        Whisper model. Each window is cut from that stream's own PCM ring and the result
        is only applied to the session that requested it."""
        unavailable_until = 0.0
        while True:
            await asyncio.sleep(1.0)
            d = self.ctx.config.detection
            if not d.live_speech_enabled or time.time() < unavailable_until:
                continue
            now = time.time()
            due = [l for l in self.sessions.values()
                   if l.state == "live" and l.ai_enabled and now - l.speech_last_run >= d.live_speech_interval_s]
            if not due:
                continue
            live = min(due, key=lambda l: l.speech_last_run)
            live.speech_last_run = now
            t1 = live.audio.latest_t()
            t0 = t1 - d.live_speech_window_s
            if live.audio.speech_ratio(t0, t1) < 0.15:
                continue                                    # nobody talking: skip the GPU
            pcm = live.audio.get_pcm(t0, t1)
            if pcm is None or len(pcm) < 16000 * 3:
                continue
            try:
                lang = (self.get_streamer(live.streamer_id) or {}).get("language") or self.ctx.config.ai.language
                res = await asyncio.to_thread(self.ctx.transcriber.transcribe_pcm, pcm,
                                              streamer_id=live.streamer_id, session_id=live.session_id, language=lang)
            except TranscriptionUnavailable as e:
                self.ctx.hub.warn("live_speech", "warn", f"Live speech detection paused: {e}")
                unavailable_until = time.time() + 300
                continue
            except Exception:
                log.exception("live speech window failed for %s", live.streamer["name"])
                continue
            if res["session_id"] != live.session_id or self.sessions.get(live.streamer_id) is not live:
                continue                                    # stream stopped/replaced meanwhile
            if is_tamil(res["text"]):
                res["words"], res["text"] = romanize_words(res["words"]), romanize_text(res["text"])
            raw, peak, hits = live_speech_raw(res["words"])
            val = live.speech_norm(raw)
            live.speech = {"score": val, "raw": raw, "t_end": t1, "peak_t": (t0 + peak) if peak is not None else None,
                           "text": res["text"][-200:], "hits": hits}
            if val >= 0.5:
                log.info("[%s] excited speech %.2f (raw %.2f) %s: %s", live.streamer["name"], val, raw, hits,
                         res["text"][-120:])

    async def _status_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(1.0)
            n += 1
            try:
                self.ctx.hub.publish("streams", {sid: live.status() for sid, live in self.sessions.items()})
                for live in self.sessions.values():
                    live.flush_chat()
                bw = self.bandwidth.status()
                if bw["over_budget"]:
                    self.ctx.hub.warn("bandwidth", "warn", f"Capture uses ~{bw['used_mbps']} Mbps, above the "
                                      f"{bw['budget_mbps']} Mbps budget. Lower-priority streams will use reduced quality on reconnect.")
                else:
                    self.ctx.hub.clear_warning("bandwidth")
                if n % 30 == 0:
                    self._prune()
            except Exception:
                log.exception("status loop failed")

    def _prune(self) -> None:
        db = self.ctx.db
        cutoff = time.time() - 1800
        # chat/audio rows are only needed around clips; keep 30 min unless referenced by a clip range
        db.execute("DELETE FROM chat_events WHERE wall_t < ? AND NOT EXISTS (SELECT 1 FROM clips c WHERE c.session_id=chat_events.session_id "
                   "AND chat_events.media_t BETWEEN c.t_start - 10 AND c.t_end + 15)", (cutoff,))
        for live in self.sessions.values():
            db.execute("DELETE FROM audio_analysis WHERE session_id=? AND media_t < ?", (live.session_id, live.audio.latest_t() - 1800))
