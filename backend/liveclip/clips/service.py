"""Clip lifecycle: protect → extract → verify → release → transcribe → score →
review → render/export.

Every clip row is bound to exactly one (streamer_id, session_id). Extraction
selects segments with `WHERE session_id = ?` and from a single capture run, so
footage from different streamers or different sessions can never be merged.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import re
import shutil
import time
from pathlib import Path

from ..ai import scoring
from ..ai.semantic import SemanticScorer
from ..ai.transcriber import Transcriber, TranscriptionUnavailable
from ..db import Database, new_id
from ..editing.framing import detect_facecam
from ..editing.render import RenderSpec, render
from ..events import EventHub
from ..jobs import JobRunner, PermanentError, RetryableError
from ..media import MediaError, run, stream_summary, ffprobe, thumbnail, verify_playable

log = logging.getLogger("liveclip.clips")

REVIEW_STATES = {"ready_for_review", "approved", "rejected", "exported"}


def _safe_name(s: str, n: int = 50) -> str:
    s = re.sub(r"[^A-Za-z0-9 _-]+", "", s or "").strip().replace(" ", "_")
    return s[:n] or "clip"


class ClipService:
    def __init__(self, ctx):
        self.ctx = ctx
        self.db: Database = ctx.db
        self.hub: EventHub = ctx.hub
        self.jobs: JobRunner = ctx.jobs
        self.transcriber: Transcriber = ctx.transcriber
        self.semantic: SemanticScorer = ctx.semantic
        self._render_lock: asyncio.Lock | None = None
        jr = self.jobs
        jr.register("extract", self.handle_extract)
        jr.register("extract:failed", self.on_extract_failed)
        jr.register("transcribe", self.handle_transcribe)
        jr.register("transcribe:failed", self.on_transcribe_failed)
        jr.register("score", self.handle_score)
        jr.register("render_preview", self.handle_render)
        jr.register("render_export", self.handle_render)
        jr.register("render_preview:failed", self.on_render_failed)
        jr.register("render_export:failed", self.on_render_failed)

    @property
    def cfg(self):
        return self.ctx.config

    # ------------------------------------------------------------------
    def _publish_clip(self, clip_id: str) -> None:
        c = self.get(clip_id)
        if c:
            self.hub.publish("clip", c, notable=True)

    def get(self, clip_id: str) -> dict | None:
        c = self.db.one("SELECT c.*, s.name AS streamer_name, s.slug AS streamer_slug, s.color AS streamer_color "
                        "FROM clips c JOIN streamers s ON s.id=c.streamer_id WHERE c.id=?", (clip_id,))
        if not c:
            return None
        for k in ("suggested_titles", "tags", "layout_params", "caption_words", "caption_style"):
            if c.get(k):
                try:
                    c[k] = json.loads(c[k])
                except ValueError:
                    pass
        tr = self.db.one("SELECT text, words, model, language FROM transcriptions WHERE clip_id=? ORDER BY created_at DESC LIMIT 1",
                         (clip_id,))
        if tr:
            tr["words"] = json.loads(tr["words"])
        c["transcript"] = tr
        c["files"] = {k: bool(c.get(k) and Path(c[k]).exists())
                      for k in ("file_path", "thumb_path", "preview_path", "export_path", "cover_path")}
        return c

    def update(self, clip_id: str, **fields) -> None:
        for k in ("suggested_titles", "tags", "layout_params", "caption_words", "caption_style"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        fields["updated_at"] = time.time()
        self.db.update("clips", "id", clip_id, fields)
        self._publish_clip(clip_id)

    # --- creation -------------------------------------------------------
    def create_clip(self, live, t0: float, t1: float, method: str, hype_event_id: str | None,
                    protection_id: str | None = None, t_event: float | None = None) -> str:
        """Create a clip record for a session range, protect it, and queue extraction."""
        cid = new_id("clip_")
        streamer = live.streamer
        t0 = max(0.0, t0)
        if method == "auto":
            # never store the same footage twice: a new AI moment that falls inside the previous
            # clip is merged into it; otherwise its lead-in starts where the previous clip ended
            prev = self.db.one("SELECT id, t_start, t_end FROM clips WHERE session_id=? AND t_end > ? "
                               "AND status NOT IN ('discarded','failed') ORDER BY t_end DESC LIMIT 1",
                               (live.session_id, t0))
            ev_t = t_event if t_event is not None else (t0 + t1) / 2
            if prev and prev["t_start"] <= ev_t <= prev["t_end"] - 2:
                if protection_id:
                    self.ctx.retention.release(protection_id)
                if hype_event_id:
                    self.db.execute("UPDATE hype_events SET clip_id=?, state='merged' WHERE id=?", (prev["id"], hype_event_id))
                log.info("AI moment at %.1fs is inside clip %s; merged instead of duplicating footage", ev_t, prev["id"])
                return prev["id"]
            if prev:
                t0 = max(t0, prev["t_end"] - 2.0)
        if t1 - t0 > self.cfg.clips.max_clip_s + 30:
            t0 = t1 - self.cfg.clips.max_clip_s - 30
        now = time.time()
        self.db.insert("clips", {
            "id": cid, "streamer_id": streamer["id"], "session_id": live.session_id, "hype_event_id": hype_event_id,
            "platform": streamer["platform"], "source_url": streamer["url"], "t_start": t0, "t_end": t1,
            "wall_start": live.origin_wall + t0 if live.origin_wall else None, "detection_method": method,
            "status": "protected", "layout": streamer.get("default_layout") or "smart_crop",
            "layout_params": json.dumps({"peak": (t_event - t0) if t_event is not None else None}),
            "created_at": now, "updated_at": now,
        })
        if protection_id:
            self.ctx.retention.update_protection(protection_id, t0, t1)
            self.db.execute("UPDATE protected_ranges SET ref_type='clip', ref_id=? WHERE id=?", (cid, protection_id))
        else:
            self.ctx.retention.protect(live.session_id, streamer["id"], t0, t1, f"clip:{method}", "clip", cid)
        if hype_event_id:
            self.db.execute("UPDATE hype_events SET clip_id=? WHERE id=?", (cid, hype_event_id))
        manual = method in ("manual_save", "mark", "viewer_clip")
        self.jobs.enqueue("extract", cid, priority=0 if manual else 1, session_id=live.session_id,
                          streamer_id=streamer["id"], payload={"deadline": now + (t1 - live.media_now()) + 90})
        self._publish_clip(cid)
        return cid

    # --- extraction -----------------------------------------------------
    async def handle_extract(self, job: dict) -> None:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (job["ref_id"],))
        if not clip or clip["file_path"] or clip["status"] not in ("protected", "detected", "processing", "failed"):
            return
        sid = clip["session_id"]
        live = self.ctx.manager.live_by_session(sid)
        newest = self.db.scalar("SELECT MAX(t_end) FROM video_segments WHERE session_id=?", (sid,)) or 0
        if live is not None and live.is_capturing() and newest < clip["t_end"]:
            if time.time() < job["payload"].get("deadline", 0):
                raise RetryableError("waiting for post-event footage", delay=2.0)
        t0, t1 = clip["t_start"], min(clip["t_end"], newest)
        segs = self.db.all("SELECT * FROM video_segments WHERE session_id=? AND deleted=0 AND t_end>? AND t_start<? "
                           "ORDER BY seq", (sid, t0, t1))
        if not segs:
            raise PermanentError("No buffered footage available for this range (it was deleted or never captured)")
        # one capture run only: choose the run holding most of the range (never splice across reconnects)
        by_run: dict[int, list] = {}
        for s in segs:
            by_run.setdefault(s["run"], []).append(s)
        run_id = max(by_run, key=lambda r: sum(min(t1, s["t_end"]) - max(t0, s["t_start"]) for s in by_run[r]))
        segs = by_run[run_id]
        for s in segs:
            if not Path(s["path"]).exists():
                raise PermanentError(f"Segment {s['seq']} missing on disk")
        self.update(clip["id"], status="processing")
        streamer = self.db.one("SELECT * FROM streamers WHERE id=?", (clip["streamer_id"],))
        out_dir = self.ctx.paths.clips / streamer["slug"]
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{clip['id']}.mp4"
        tmp = self.ctx.paths.temp / f"{clip['id']}.concat.txt"
        tmp.write_text("".join(f"file '{Path(s['path']).as_posix()}'\n" for s in segs), encoding="utf-8")
        seek = max(0.0, t0 - segs[0]["t_start"])
        dur = min(t1, segs[-1]["t_end"]) - max(t0, segs[0]["t_start"])
        ff = self.ctx.tools.ffmpeg
        cmd = [ff, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-ss", f"{seek:.3f}", "-i", str(tmp),
               "-t", f"{dur:.3f}", "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-avoid_negative_ts", "make_zero",
               "-movflags", "+faststart", str(out)]
        try:
            await asyncio.to_thread(run, cmd, 300)
            summ = await asyncio.to_thread(verify_playable, ff, self.ctx.tools.ffprobe, out)
        except MediaError as e:
            # stream copy can fail on a corrupt segment: retry with re-encode as a fallback
            log.warning("copy extract failed for %s (%s); re-encoding", clip["id"], e)
            cmd2 = cmd[:cmd.index("-c")] + ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-c:a", "aac",
                                             "-b:a", "192k", "-movflags", "+faststart", str(out)]
            await asyncio.to_thread(run, cmd2, 900, "low")
            summ = await asyncio.to_thread(verify_playable, ff, self.ctx.tools.ffprobe, out)
        finally:
            tmp.unlink(missing_ok=True)
        thumb = self.ctx.paths.thumbnails / f"{clip['id']}.jpg"
        lp = json.loads(clip["layout_params"] or "{}")
        try:
            await asyncio.to_thread(thumbnail, ff, out, thumb, min(summ["duration"] - 0.5, lp.get("peak") or summ["duration"] / 2))
        except MediaError:
            thumb = None
        actual_t0 = max(t0, segs[0]["t_start"])
        if lp.get("peak") is not None:
            lp["peak"] = max(0.0, (clip["t_start"] + lp["peak"]) - actual_t0)
        self.update(clip["id"], file_path=str(out), file_size=out.stat().st_size, thumb_path=str(thumb) if thumb else None,
                    duration=summ["duration"], width=summ.get("width"), height=summ.get("height"), fps=summ.get("fps"),
                    t_start=actual_t0, t_end=actual_t0 + summ["duration"], layout_params=lp, status="processing")
        # permanent copy verified -> release the temporary segments
        log.info("clip %s extracted: %.1fs %sx%s %.1f MB", clip["id"], summ["duration"], summ.get("width"), summ.get("height"), out.stat().st_size / 1e6)
        self.ctx.retention.release_ref("clip", clip["id"])
        manual = clip["detection_method"] in ("manual_save", "mark", "viewer_clip")
        self.jobs.enqueue("transcribe", clip["id"], priority=2 if manual else 3, session_id=sid,
                          streamer_id=clip["streamer_id"])

    async def on_extract_failed(self, job: dict) -> None:
        self.ctx.retention.release_ref("clip", job["ref_id"])
        self.update(job["ref_id"], status="failed", error=f"extract: {job['error'][:300]}")

    # --- transcription ----------------------------------------------------
    async def handle_transcribe(self, job: dict) -> None:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (job["ref_id"],))
        if not clip or not clip["file_path"]:
            return
        wav = self.ctx.paths.temp / f"{clip['id']}.wav"
        await asyncio.to_thread(run, [self.ctx.tools.ffmpeg, "-y", "-v", "error", "-i", clip["file_path"], "-vn", "-ac", "1",
                                      "-ar", "16000", "-c:a", "pcm_s16le", str(wav)], 120)
        try:
            res = await asyncio.to_thread(self.transcriber.transcribe, str(wav), streamer_id=clip["streamer_id"],
                                          session_id=clip["session_id"], clip_id=clip["id"])
        except TranscriptionUnavailable as e:
            self.hub.warn("whisper", "warn", f"Transcription unavailable: {e}")
            res = None
        finally:
            wav.unlink(missing_ok=True)
        if res is not None:
            # results must come back to the same clip/session/streamer they were submitted for
            assert res["clip_id"] == clip["id"] and res["session_id"] == clip["session_id"] \
                and res["streamer_id"] == clip["streamer_id"], "transcription routed to wrong clip"
            self.db.execute("DELETE FROM transcriptions WHERE clip_id=?", (clip["id"],))
            self.db.insert("transcriptions", {
                "id": new_id("tr_"), "session_id": clip["session_id"], "streamer_id": clip["streamer_id"],
                "clip_id": clip["id"], "t_start": clip["t_start"], "t_end": clip["t_end"], "language": res["language"],
                "text": res["text"], "words": json.dumps(res["words"]), "model": res["model"],
                "avg_logprob": res["avg_logprob"], "created_at": time.time(),
            })
            self.update(clip["id"], caption_text=res["text"], caption_words=None)
        self.jobs.enqueue("score", clip["id"], session_id=clip["session_id"], streamer_id=clip["streamer_id"])

    async def on_transcribe_failed(self, job: dict) -> None:
        # transcription failure must not lose the clip: continue to scoring without transcript
        self.jobs.enqueue("score", job["ref_id"], session_id=job["session_id"], streamer_id=job["streamer_id"])

    # --- scoring --------------------------------------------------------
    async def handle_score(self, job: dict) -> None:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (job["ref_id"],))
        if not clip:
            return
        streamer = self.db.one("SELECT * FROM streamers WHERE id=?", (clip["streamer_id"],))
        ev = self.db.one("SELECT * FROM hype_events WHERE id=?", (clip["hype_event_id"],)) if clip["hype_event_id"] else None
        tr = self.db.one("SELECT * FROM transcriptions WHERE clip_id=?", (clip["id"],))
        words = json.loads(tr["words"]) if tr else []
        text = tr["text"] if tr else ""
        dur = clip["duration"] or (clip["t_end"] - clip["t_start"])
        lp = json.loads(clip["layout_params"] or "{}")
        peak = lp.get("peak")
        feats = scoring.transcript_features(text, words, dur)
        chat_rows = self.db.all("SELECT text FROM chat_events WHERE session_id=? AND media_t BETWEEN ? AND ? ORDER BY media_t",
                                (clip["session_id"], clip["t_start"], clip["t_end"] + self.cfg.detection.chat_reaction_delay_s + 5))
        chat_texts = [r["text"] for r in chat_rows]
        laugh_tokens = self.cfg.detection.laugh_tokens
        chat_laugh = (sum(any(k in t.lower() for k in laugh_tokens) for t in chat_texts) / len(chat_texts)) if chat_texts else None
        laughter = max(feats["laugh_score"], chat_laugh or 0.0) if (words or chat_texts) else None
        prev = self.db.all("SELECT c.caption_text, c.created_at FROM clips c WHERE c.streamer_id=? AND c.id<>? "
                           "AND c.status NOT IN ('discarded','failed') ORDER BY c.created_at DESC LIMIT 10",
                           (clip["streamer_id"], clip["id"]))
        last_gap = (clip["created_at"] - prev[0]["created_at"]) if prev else None
        nov = scoring.novelty(text, [p["caption_text"] or "" for p in prev], last_gap)
        chat_available = bool(chat_rows) or (ev and json.loads(ev["signals"] or "{}").get("chat_available"))
        ev_audio = ev["audio_score"] if ev else None
        ev_chat = ev["chat_score"] if ev else None
        if ev_audio is None or (ev_chat is None and chat_available):
            # manual saves/marks: derive the signals from what was recorded for this range
            st_audio, st_chat = await asyncio.to_thread(self._stored_signals, clip)
            ev_audio = ev_audio if ev_audio is not None else st_audio
            ev_chat = ev_chat if ev_chat is not None else st_chat
        signals = json.loads(ev["signals"] or "{}") if ev else {}
        ev_speech = (signals.get("speech") or {}).get("score")
        chat_info = signals.get("chat") if isinstance(signals.get("chat"), dict) else None
        if ev and chat_available and clip["detection_method"] == "auto":
            # chat reacts a few seconds late: use its peak over the whole clip, not just the trigger instant
            _, st_chat = await asyncio.to_thread(self._stored_signals, clip)
            if st_chat is not None and st_chat > (ev_chat or 0.0):
                ev_chat = st_chat
                if st_chat >= 0.3:
                    chat_info = None      # chat really reacted; the trigger-time "quiet chat" no longer applies
        snd = await asyncio.to_thread(self._clip_sound_events, clip)
        if snd:
            from ..detection.sound_events import reaction_score
            laughter = max(laughter or 0.0, reaction_score(snd["laugh"]))
            ev_audio = max(ev_audio or 0.0, reaction_score(snd["scream"]) * 0.9)
        scores = {
            "chat": ev_chat if chat_available else None,
            "audio": ev_audio,
            "speech": ev_speech,
            "transcript": feats["score"] if words else None,
            "laughter": laughter,
            "novelty": nov,
        }
        scoring.drop_quiet_partner(scores, chat_info=chat_info)
        sem = None
        if self.cfg.ai.ollama_enabled:
            sem = await self.semantic.score(streamer=streamer["name"], duration=dur, peak=peak or dur / 2, words=words,
                                            chat=chat_texts, audio=scores["audio"] or 0, chat_score=scores["chat"] or 0)
            if sem:
                scores["semantic"] = sem.score
        final = scoring.weighted(scores, self.cfg.detection.weights.model_dump())
        reasons = []
        if scores.get("chat") is not None and scores["chat"] > 0.5:
            reasons.append(f"chat spike (score {scores['chat']:.2f})")
        if scores.get("audio") is not None and scores["audio"] > 0.5:
            reasons.append(f"audio excitement {scores['audio']:.2f} above this streamer's baseline")
        if feats["shock"] or feats["exclaim"]:
            reasons.append(f"{feats['shock']} reaction phrases in speech")
        if laughter and laughter > 0.3:
            reasons.append("laughter" + (f" (heard {snd['laugh']:.2f})" if snd and snd["laugh"] > 0.3 else ""))
        if ev_speech and ev_speech > 0.5:
            hits = (signals.get("speech") or {}).get("hits") or []
            reasons.append("excited speech" + (f": {', '.join(hits[:3])}" if hits else ""))
        if sem and sem.reason:
            reasons.append(f"LLM ({sem.category}): {sem.reason}")
        cut = None
        try:
            from ..editing.highlight import best_reel_cut
            cut = await asyncio.to_thread(best_reel_cut, words, dur, peak, self.cfg.render.max_reel_s, 0.0, None, 15.0,
                                          await asyncio.to_thread(self.clip_timeline, clip))
        except Exception:
            log.exception("reel cut preview failed for %s", clip["id"])
        titles = (sem.titles if sem and sem.titles else []) + scoring.title_suggestions(streamer["name"], words, text, peak, cut)
        seen, uniq = set(), []
        for t in titles:
            if t.lower() not in seen:
                seen.add(t.lower())
                uniq.append(t)
        method = clip["detection_method"]
        status = "ready_for_review"
        if method == "viewer_clip":
            reasons.insert(0, "a viewer clipped this moment on Twitch")
            final = max(final, 0.75)                 # human-confirmed highlight
        elif method == "auto" and final < self.cfg.detection.save_threshold:
            # near-misses go to a storage-capped Maybe pool instead of being deleted
            status = "maybe" if final >= self.cfg.clips.maybe_threshold else "discarded"
        fields = dict(ai_score=final, ai_reason="; ".join(reasons) or "Lightweight signals only",
                      suggested_titles=uniq[:5], status=status)
        if not clip["title"] and uniq:
            fields["title"] = uniq[0]
        if sem and sem.start is not None and sem.end is not None and method == "auto":
            fields["trim_start"], fields["trim_end"] = sem.start, sem.end
        log.info("clip %s scored %.2f -> %s (%s)", clip["id"], final, status, scores)
        self.update(clip["id"], **fields)
        if ev:
            self.db.update("hype_events", "id", ev["id"], {
                "transcript_score": scores.get("transcript"), "laughter_score": laughter, "novelty_score": nov,
                "semantic_score": scores.get("semantic"), "final_score": final,
                "state": "discarded" if status == "discarded" else "clipped"})
            self.hub.publish("hype", self.db.one("SELECT * FROM hype_events WHERE id=?", (ev["id"],)), notable=True)
        if status == "maybe":
            await asyncio.to_thread(self.enforce_maybe_budget)
        if status == "discarded" and not self.cfg.clips.keep_discarded_auto_clips:
            for k in ("file_path", "thumb_path"):
                v = self.db.scalar(f"SELECT {k} FROM clips WHERE id=?", (clip["id"],))
                if v:
                    Path(v).unlink(missing_ok=True)
            self.update(clip["id"], file_path=None, thumb_path=None, file_size=0)
        if streamer["facecam"] is None and status != "discarded" and self.cfg.ai.vision_enabled and clip["file_path"]:
            rect = await asyncio.to_thread(detect_facecam, self.ctx.tools.ffmpeg, clip["file_path"], dur)
            if rect:
                self.db.update("streamers", "id", streamer["id"], {"facecam": json.dumps(rect)})

    def enforce_maybe_budget(self) -> int:
        """Keep the Maybe pool under its byte budget: lowest-scoring (then oldest)
        near-miss clips are deleted first. Approved/review/exported clips are never touched."""
        budget = self.cfg.clips.maybe_budget_bytes
        rows = self.db.all("SELECT id, file_path, thumb_path, file_size, ai_score FROM clips WHERE status='maybe' "
                           "AND file_path IS NOT NULL ORDER BY COALESCE(ai_score,0) ASC, created_at ASC")
        total = sum(r["file_size"] or 0 for r in rows)
        removed = 0
        for r in rows:
            if total <= budget:
                break
            for k in ("file_path", "thumb_path"):
                if r[k]:
                    Path(r[k]).unlink(missing_ok=True)
            self.update(r["id"], status="discarded", file_path=None, thumb_path=None, file_size=0,
                        error="removed from Maybe pool (storage budget)")
            total -= r["file_size"] or 0
            removed += 1
        if removed:
            log.info("maybe pool over budget: removed %d lowest-scoring near-miss clips", removed)
        return removed

    def confirm_by_viewers(self, clip_id: str, vc: dict) -> None:
        """Viewers clipped a moment we already have: lift it to review and note why."""
        c = self.db.one("SELECT status, ai_reason FROM clips WHERE id=?", (clip_id,))
        if not c:
            return
        reason = "a viewer clipped this moment on Twitch; " + (c["ai_reason"] or "")
        fields = {"ai_reason": reason[:500]}
        if c["status"] in ("maybe", "processing", "protected"):
            fields["status"] = "ready_for_review" if c["status"] == "maybe" else c["status"]
        self.update(clip_id, **fields)
        log.info("clip %s confirmed by viewer clip %s (%s)", clip_id, vc["id"], vc["creator"])

    def clip_timeline(self, clip: dict) -> list[dict] | None:
        """YAMNet per-frame timeline of the clip file (voice / reaction / game / loudness)."""
        if not clip.get("file_path") or self.ctx.sound.status == "unavailable":
            return None
        try:
            import numpy as np
            import subprocess

            from ..media import popen_flags
            p = subprocess.run([self.ctx.tools.ffmpeg, "-v", "error", "-i", clip["file_path"], "-vn", "-ac", "1", "-ar", "16000",
                                "-f", "s16le", "-"], capture_output=True, timeout=60, **popen_flags("low"))
            return self.ctx.sound.timeline(np.frombuffer(p.stdout, dtype=np.int16))
        except Exception:
            log.exception("clip timeline failed for %s", clip.get("id"))
            return None

    def _clip_sound_events(self, clip: dict) -> dict | None:
        """YAMNet over the whole extracted clip (~0.1 s on CPU): max laughter / scream."""
        if not clip.get("file_path") or self.ctx.sound.status == "unavailable":
            return None
        try:
            import numpy as np
            import subprocess

            from ..media import popen_flags
            p = subprocess.run([self.ctx.tools.ffmpeg, "-v", "error", "-i", clip["file_path"], "-vn", "-ac", "1", "-ar", "16000",
                                "-f", "s16le", "-"], capture_output=True, timeout=60, **popen_flags("low"))
            pcm = np.frombuffer(p.stdout, dtype=np.int16)
            return self.ctx.sound.classify(pcm)
        except Exception:
            log.exception("clip sound events failed for %s", clip.get("id"))
            return None

    def _stored_signals(self, clip: dict) -> tuple[float | None, float | None]:
        """Peak audio excitement and peak chat excitement over a clip's range,
        recomputed from the stored per-second audio analysis and chat rows (the
        chat baseline is that session's own preceding 10 minutes)."""
        from ..detection.chat import ChatAnalyzer

        sid, t0, t1 = clip["session_id"], clip["t_start"], clip["t_end"]
        audio = self.db.scalar("SELECT MAX(excitement) FROM audio_analysis WHERE session_id=? AND media_t BETWEEN ? AND ?",
                               (sid, t0, t1))
        delay = self.cfg.detection.chat_reaction_delay_s
        rows = self.db.all("SELECT wall_t, media_t, author, text, emote_count FROM chat_events WHERE session_id=? "
                           "AND media_t BETWEEN ? AND ? ORDER BY wall_t", (sid, t0 - 600, t1 + delay + 5))
        if not rows:
            return audio, None
        d = self.cfg.detection
        ca = ChatAnalyzer(d.keywords, d.laugh_tokens)
        origin = rows[0]["wall_t"] - rows[0]["media_t"]
        best, i = 0.0, 0
        t = rows[0]["wall_t"]
        end = origin + t1 + delay + 5
        while t <= end:
            while i < len(rows) and rows[i]["wall_t"] <= t:
                r = rows[i]
                ca.add(r["author"] or "", r["text"], r["emote_count"], r["wall_t"])
                i += 1
            if t >= origin + t0:
                best = max(best, ca.score(t)["score"])
            t += 1.0
        return audio, round(best, 3)

    # --- rendering ------------------------------------------------------
    def render_spec(self, clip: dict, preview: bool, out: Path) -> RenderSpec:
        rc = self.cfg.render
        streamer = self.db.one("SELECT * FROM streamers WHERE id=?", (clip["streamer_id"],))
        lp = clip.get("layout_params") or {}
        if isinstance(lp, str):
            lp = json.loads(lp)
        cs = clip.get("caption_style") or {}
        if isinstance(cs, str):
            cs = json.loads(cs)
        words = clip.get("caption_words")
        if isinstance(words, str):
            words = json.loads(words)
        if not words:
            tr = self.db.one("SELECT words FROM transcriptions WHERE clip_id=?", (clip["id"],))
            words = json.loads(tr["words"]) if tr else []
        facecam = lp.get("facecam") or (json.loads(streamer["facecam"]) if streamer["facecam"] else None)
        # reel length rule: explicit segments > a user trim that already fits > auto best-moment cut
        ts, te = clip["trim_start"] or 0.0, clip["trim_end"] or clip["duration"]
        segments = lp.get("segments")
        if not segments and te - ts > rc.max_reel_s:
            from ..editing.highlight import best_reel_cut
            segments = best_reel_cut(words, clip["duration"], lp.get("peak"), rc.max_reel_s, lo=ts, hi=te,
                                     timeline=self.clip_timeline(clip))
        return RenderSpec(
            src=clip["file_path"], src_w=clip["width"] or 1920, src_h=clip["height"] or 1080, src_fps=clip["fps"] or 60,
            duration=clip["duration"], out=str(out), layout=clip["layout"], trim_start=clip["trim_start"] or 0.0,
            trim_end=clip["trim_end"], peak=lp.get("peak"), words=words, facecam=facecam, gameplay=lp.get("gameplay"),
            segments=segments, title=clip.get("title"),
            caption_style=cs.get("style", rc.caption_style), caption_size=int(cs.get("size", rc.caption_size)),
            caption_y=float(cs.get("y", rc.caption_y)), caption_uppercase=bool(cs.get("uppercase", rc.caption_uppercase)),
            captions=bool(cs.get("enabled", True)), hook=bool(cs.get("hook", rc.hook_text_enabled)),
            hook_seconds=rc.hook_seconds, font=cs.get("font", rc.caption_font), fonts_dir=rc.caption_fonts_dir,
            cold_open=bool(lp.get("cold_open", False)), punch_zoom=bool(lp.get("punch_zoom", True)),
            fps=int(cs.get("fps", rc.fps)), preview=preview, encoder=rc.encoder, nvenc_cq=rc.nvenc_cq, x264_crf=rc.x264_crf,
            max_bitrate_mbps=rc.max_bitrate_mbps, audio_bitrate_k=rc.audio_bitrate_k,
            normalize_audio=bool(lp.get("normalize_audio", False)),
        )

    async def handle_render(self, job: dict) -> None:
        preview = job["kind"] == "render_preview"
        clip = self.get(job["ref_id"])
        if not clip or not clip["file_path"] or not Path(clip["file_path"]).exists():
            raise PermanentError("Source clip file is missing")
        if self.ctx.resources.thermal_hold():
            raise RetryableError("GPU hot; render deferred", delay=15)
        slug = clip["streamer_slug"]
        if preview:
            out = self.ctx.paths.temp / "previews" / f"{clip['id']}.mp4"
        else:
            name = f"{time.strftime('%Y%m%d')}_{slug}_{_safe_name(clip.get('title') or clip['id'])}_{clip['id'][-6:]}.mp4"
            out = self.ctx.paths.exports / slug / name
        spec = self.render_spec(clip, preview, out)
        self.update(clip["id"], export_status="rendering" if not preview else clip["export_status"])
        last = [0.0]

        def prog(p: float) -> None:
            if p - last[0] > 0.04:
                last[0] = p
                self.hub.publish("render_progress", {"clip_id": clip["id"], "preview": preview, "progress": round(p, 3)})

        # previews and exports share one GPU encode slot so renders never pile up on the GPU
        if self._render_lock is None:
            self._render_lock = asyncio.Lock()
        async with self._render_lock:
            res = await asyncio.to_thread(render, spec, self.ctx.tools.ffmpeg, self.ctx.paths.temp, prog)
        summ = await asyncio.to_thread(verify_playable, self.ctx.tools.ffmpeg, self.ctx.tools.ffprobe, res["path"])
        if preview:
            self.update(clip["id"], preview_path=res["path"])
        else:
            if clip.get("export_path") and clip["export_path"] != res["path"]:
                old = Path(clip["export_path"])
                for p in (old, old.with_suffix(".srt"), old.with_suffix(".transcript.json"),
                          old.with_suffix(".cover.jpg"), old.with_suffix(".caption.txt")):
                    p.unlink(missing_ok=True)
            cover, caption = await self._post_kit(clip, spec, res)
            self.update(clip["id"], export_path=res["path"], export_status="done", status="exported", error=None,
                        cover_path=cover, post_caption=caption)
            self.db.insert("export_jobs", {"id": new_id("exp_"), "clip_id": clip["id"], "target": "local", "state": "done",
                                           "output_path": res["path"], "created_at": time.time(), "finished_at": time.time()})
        self.hub.publish("render_done", {"clip_id": clip["id"], "preview": preview, "path": res["path"],
                                         "width": summ.get("width"), "height": summ.get("height"), "fps": summ.get("fps")})

    async def _post_kit(self, clip: dict, spec: RenderSpec, res: dict) -> tuple[str | None, str | None]:
        """Cover image + post caption next to the exported reel. Never fails the export."""
        from ..editing.cover import make_cover, pick_cover_time, pick_face_time, post_caption

        out = Path(res["path"])
        words = res.get("words") or []
        streamer = self.db.one("SELECT name, platform, url FROM streamers WHERE id=?", (clip["streamer_id"],)) or {}
        caption = None
        try:
            caption = post_caption(streamer=streamer.get("name") or clip.get("streamer_name") or "streamer",
                                   platform=streamer.get("platform") or clip.get("platform") or "",
                                   url=streamer.get("url"), hook=clip.get("title"), words=words)
            out.with_suffix(".caption.txt").write_text(caption, encoding="utf-8")
        except Exception:
            log.exception("post caption failed for %s", clip["id"])
        cover = None
        work = self.ctx.paths.temp / f"cover_{clip['id']}"
        ff = self.ctx.tools.ffmpeg
        style = dict(style=spec.caption_style, font=spec.font, fonts_dir=spec.fonts_dir, uppercase=spec.caption_uppercase)
        hook = spec.title if spec.hook else None
        try:
            pieces = res.get("pieces") or []
            t_face = None
            if spec.facecam:
                # the streamer's own reaction face (inside their facecam), near the peak
                t_face = await asyncio.to_thread(pick_face_time, ff, spec.src, pieces, spec.facecam,
                                                 spec.src_w, spec.src_h, spec.peak)
            if t_face is not None:
                # cover in the facecam layout: face on top, content below, hook on the seam
                one = dataclasses.replace(spec, layout="facecam", segments=[(max(0.0, t_face - 0.05), t_face + 0.55)],
                                          words=[], captions=False, hook=False, punch_zoom=False, cold_open=False,
                                          preview=False, out=str(work / "cover_src.mp4"))
                work.mkdir(parents=True, exist_ok=True)
                async with self._render_lock:
                    r1 = await asyncio.to_thread(render, one, ff, self.ctx.paths.temp)
                p = await asyncio.to_thread(make_cover, ff, r1["path"], out.with_suffix(".cover.jpg"), 0.1, hook=hook,
                                            work=work / "c", hook_y=int(1920 * 0.40) // 2 * 2, **style)
                log.info("cover for %s: streamer face at %.1fs of the source", clip["id"], t_face)
            else:
                cuts, off, peak = [], 0.0, None
                for a, b in pieces:                   # map the source-time peak onto the reel timeline
                    if spec.peak is not None and a <= spec.peak <= b:
                        peak = off + spec.peak - a
                    off += b - a
                    cuts.append(off)
                hook_until = spec.hook_seconds if hook else 0.0
                # no known facecam: faces in frame may be content (thumbnails, a watched video) -> low weight
                at = await asyncio.to_thread(pick_cover_time, ff, str(out), res["duration"], peak=peak, words=words,
                                             cuts=cuts[:-1], hook_until=hook_until, face_weight=0.1)
                p = await asyncio.to_thread(make_cover, ff, str(out), out.with_suffix(".cover.jpg"), at, hook=hook,
                                            work=work, **style)
                log.info("cover for %s at %.1fs of the reel", clip["id"], at)
            cover = str(p)
        except Exception:
            log.exception("cover failed for %s", clip["id"])
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return cover, caption

    async def on_render_failed(self, job: dict) -> None:
        if job["kind"] == "render_export":
            self.update(job["ref_id"], export_status="failed", error=f"render: {job['error'][:300]}")
        self.hub.publish("render_failed", {"clip_id": job["ref_id"], "error": job["error"][:500]}, notable=True)

    # --- user actions ---------------------------------------------------
    def delete(self, clip_id: str) -> None:
        clip = self.db.one("SELECT * FROM clips WHERE id=?", (clip_id,))
        if not clip:
            return
        self.ctx.retention.release_ref("clip", clip_id)
        for k in ("file_path", "thumb_path", "preview_path", "export_path", "cover_path"):
            if clip.get(k):
                p = Path(clip[k])
                p.unlink(missing_ok=True)
                if k == "export_path":
                    for ext in (".srt", ".transcript.json", ".cover.jpg", ".caption.txt"):
                        p.with_suffix(ext).unlink(missing_ok=True)
        self.db.execute("DELETE FROM clips WHERE id=?", (clip_id,))
        self.hub.publish("clip_deleted", {"id": clip_id}, notable=True)

    async def resume_pending(self) -> None:
        """Startup recovery: clips stuck in protected/processing get their next job re-queued."""
        for c in self.db.all("SELECT id, status, file_path, session_id, streamer_id FROM clips WHERE status IN ('protected','detected','processing')"):
            if c["file_path"] and Path(c["file_path"]).exists():
                self.jobs.enqueue("transcribe", c["id"], session_id=c["session_id"], streamer_id=c["streamer_id"])
            else:
                self.jobs.enqueue("extract", c["id"], session_id=c["session_id"], streamer_id=c["streamer_id"],
                                  payload={"deadline": 0})
