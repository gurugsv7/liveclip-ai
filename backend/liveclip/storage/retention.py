"""Rolling-buffer retention and footage protection.

Rules (enforced under one lock so protect() and sweep() can't race):
  * Only *completed* segments (listed by FFmpeg in the segment CSV and indexed
    in the DB) are ever deleted. The segment being written is not in the
    index, so it is never touched.
  * A segment overlapping any unreleased protected range is never deleted.
  * Unprotected segments older than `retention_seconds` behind that session's
    newest segment are deleted.
  * If temp usage exceeds the byte budget, the oldest unprotected segments are
    deleted early (still never protected/in-progress ones) and a warning is raised.
  * Permanent clips live under data/clips and are never touched here.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from ..config import AppConfig, Paths
from ..db import Database, new_id
from ..events import EventHub

log = logging.getLogger("liveclip.retention")


class RetentionManager:
    def __init__(self, db: Database, cfg_getter, paths: Paths, hub: EventHub):
        self.db = db
        self.cfg_getter = cfg_getter
        self.paths = paths
        self.hub = hub
        self.lock = threading.RLock()
        self.active_sessions: dict[str, dict] = {}   # session_id -> {"next_seq": callable}
        self.deleted_total = 0
        self.last_sweep = 0.0

    @property
    def cfg(self) -> AppConfig:
        return self.cfg_getter()

    # --- protection ---------------------------------------------------
    def protect(self, session_id: str, streamer_id: str, t0: float, t1: float, reason: str,
                ref_type: str | None = None, ref_id: str | None = None) -> str:
        pid = new_id("prot_")
        with self.lock:
            self.db.insert("protected_ranges", {
                "id": pid, "session_id": session_id, "streamer_id": streamer_id, "t_start": max(0.0, t0),
                "t_end": t1, "reason": reason, "ref_type": ref_type, "ref_id": ref_id, "created_at": time.time(),
            })
        return pid

    def update_protection(self, pid: str, t0: float, t1: float) -> None:
        with self.lock:
            self.db.execute("UPDATE protected_ranges SET t_start=?, t_end=? WHERE id=? AND released_at IS NULL",
                            (max(0.0, t0), t1, pid))

    def release(self, pid: str) -> None:
        with self.lock:
            self.db.execute("UPDATE protected_ranges SET released_at=? WHERE id=? AND released_at IS NULL",
                            (time.time(), pid))

    def release_ref(self, ref_type: str, ref_id: str) -> None:
        with self.lock:
            self.db.execute("UPDATE protected_ranges SET released_at=? WHERE ref_type=? AND ref_id=? AND released_at IS NULL",
                            (time.time(), ref_type, ref_id))

    def active_protections(self, session_id: str) -> list[dict]:
        return self.db.all("SELECT * FROM protected_ranges WHERE session_id=? AND released_at IS NULL", (session_id,))

    @staticmethod
    def _overlaps(seg: dict, prots: list[dict]) -> bool:
        return any(seg["t_end"] > p["t_start"] and seg["t_start"] < p["t_end"] for p in prots)

    # --- deletion -----------------------------------------------------
    def _delete_segment(self, seg: dict) -> bool:
        try:
            os.remove(seg["path"])
        except FileNotFoundError:
            pass
        except PermissionError:
            return False  # file open by a reader (e.g. preview/extraction); retry next sweep
        self.db.execute("UPDATE video_segments SET deleted=1, deleted_at=? WHERE id=?", (time.time(), seg["id"]))
        self.deleted_total += 1
        return True

    def sweep(self) -> dict:
        """One retention pass over every session. Returns stats."""
        cfg = self.cfg
        stats = {"deleted": 0, "budget_deleted": 0}
        with self.lock:
            sessions = self.db.all("SELECT DISTINCT session_id FROM video_segments WHERE deleted=0")
            for row in sessions:
                sid = row["session_id"]
                active = sid in self.active_sessions
                prots = self.active_protections(sid)
                segs = self.db.all("SELECT * FROM video_segments WHERE session_id=? AND deleted=0 ORDER BY seq", (sid,))
                if not segs:
                    continue
                newest = max(s["t_end"] for s in segs)
                cutoff = newest - cfg.buffer.retention_seconds if active else float("inf")
                for s in segs:
                    if s["t_end"] <= cutoff and not self._overlaps(s, prots):
                        if self._delete_segment(s):
                            stats["deleted"] += 1
            stats.update(self._enforce_budget())
            self._cleanup_finished_sessions()
        self.last_sweep = time.time()
        return stats

    def _enforce_budget(self) -> dict:
        cfg = self.cfg
        total = self.db.scalar("SELECT COALESCE(SUM(size),0) FROM video_segments WHERE deleted=0") or 0
        out = {"budget_deleted": 0, "temp_bytes": total}
        if total <= cfg.buffer.max_temp_bytes:
            self.hub.clear_warning("temp_budget")
            return out
        # Delete oldest unprotected segments across all sessions until under budget,
        # but keep each active session's newest 3 segments (preview continuity).
        segs = self.db.all("SELECT * FROM video_segments WHERE deleted=0 ORDER BY created_at")
        prots_by_session: dict[str, list] = {}
        newest_keep: dict[str, set] = {}
        for sid in {s["session_id"] for s in segs}:
            prots_by_session[sid] = self.active_protections(sid)
            keep = self.db.all("SELECT id FROM video_segments WHERE session_id=? AND deleted=0 ORDER BY seq DESC LIMIT 3", (sid,))
            newest_keep[sid] = {k["id"] for k in keep}
        for s in segs:
            if total <= cfg.buffer.max_temp_bytes * 0.9:
                break
            if s["id"] in newest_keep[s["session_id"]] or self._overlaps(s, prots_by_session[s["session_id"]]):
                continue
            if self._delete_segment(s):
                total -= s["size"]
                out["budget_deleted"] += 1
        out["temp_bytes"] = total
        if total > cfg.buffer.max_temp_bytes:
            self.hub.warn("temp_budget", "error",
                          f"Temporary storage {total/1e9:.2f} GB exceeds budget {cfg.buffer.max_temp_bytes/1e9:.2f} GB; "
                          "remaining footage is protected by pending clips")
        else:
            self.hub.warn("temp_budget", "warn", "Temporary storage budget reached; oldest unprotected footage deleted early")
        return out

    def _cleanup_finished_sessions(self) -> None:
        """Remove directories of ended sessions with no remaining segments."""
        ended = self.db.all("SELECT id, dir FROM stream_sessions WHERE state IN ('stopped','failed','ended') AND dir IS NOT NULL")
        for s in ended:
            if s["id"] in self.active_sessions:
                continue
            remaining = self.db.scalar("SELECT COUNT(*) FROM video_segments WHERE session_id=? AND deleted=0", (s["id"],))
            if remaining == 0 and not self.active_protections(s["id"]):
                d = Path(s["dir"])
                if d.exists() and self.paths.recordings in d.resolve().parents:
                    shutil.rmtree(d, ignore_errors=True)

    def delete_unprotected(self, session_id: str) -> dict:
        """Manual 'Delete Buffer': remove all completed, unprotected segments
        except the newest one (keeps the preview playlist valid)."""
        n = 0
        kept = 0
        with self.lock:
            prots = self.active_protections(session_id)
            segs = self.db.all("SELECT * FROM video_segments WHERE session_id=? AND deleted=0 ORDER BY seq", (session_id,))
            for s in segs[:-1]:
                if self._overlaps(s, prots):
                    kept += 1
                    continue
                if self._delete_segment(s):
                    n += 1
        return {"deleted": n, "protected_kept": kept}

    # --- orphan / crash recovery -------------------------------------
    def cleanup_orphans(self, min_age_s: float = 60.0) -> int:
        """Delete .ts files on disk that the index doesn't know about (left by a
        crash or a killed FFmpeg), skipping anything recent or possibly being written."""
        removed = 0
        known = {r["path"] for r in self.db.all("SELECT path FROM video_segments WHERE deleted=0")}
        now = time.time()
        for f in self.paths.recordings.rglob("seg_*.ts"):
            p = str(f)
            if p in known:
                continue
            try:
                # min_age_s <= 0 means "no age check" (startup recovery); file mtimes on
                # Windows can be a hair ahead of time.time(), so don't compare against 0
                if min_age_s > 0 and now - f.stat().st_mtime < min_age_s:
                    continue
                sid = f.parent.parent.name
                if sid in self.active_sessions:
                    m = re.search(r"(\d+)", f.stem)
                    nxt = self.active_sessions[sid].get("next_seq", lambda: 0)()
                    if m and int(m.group(1)) >= nxt - 1:
                        continue  # current/just-finished segment
                f.unlink()
                removed += 1
            except (OSError, PermissionError):
                continue
        for f in self.paths.temp.glob("*"):
            try:
                if now - f.stat().st_mtime > 3600:
                    f.unlink() if f.is_file() else shutil.rmtree(f, ignore_errors=True)
            except OSError:
                continue
        return removed

    def recover_on_startup(self) -> dict:
        """After a crash: sessions marked live are closed; segments whose files
        vanished are marked deleted; protections whose clip is finished are released."""
        n_sessions = self.db.execute(
            "UPDATE stream_sessions SET state='ended', ended_at=?, error=COALESCE(error,'app restarted') "
            "WHERE state NOT IN ('stopped','failed','ended')", (time.time(),)).rowcount
        missing = 0
        for s in self.db.all("SELECT id, path FROM video_segments WHERE deleted=0"):
            if not Path(s["path"]).exists():
                self.db.execute("UPDATE video_segments SET deleted=1, deleted_at=? WHERE id=?", (time.time(), s["id"]))
                missing += 1
        # clips whose permanent file exists no longer need their segments; clips still
        # waiting for extraction keep their protection (the job is re-queued)
        released = self.db.execute(
            "UPDATE protected_ranges SET released_at=? WHERE released_at IS NULL AND ((ref_type='clip' AND ref_id IN "
            "(SELECT id FROM clips WHERE file_path IS NOT NULL OR status IN ('failed','discarded','rejected'))) "
            "OR (ref_type='clip' AND ref_id NOT IN (SELECT id FROM clips)) "
            "OR ref_type='hype' OR ref_type IS NULL)", (time.time(),)).rowcount
        orphans = self.cleanup_orphans(min_age_s=0)
        return {"sessions_closed": n_sessions, "missing_segments": missing, "released": released, "orphans": orphans}

    # --- reporting ----------------------------------------------------
    def buffer_status(self, session_id: str) -> dict:
        segs = self.db.all("SELECT seq, t_start, t_end, size, run FROM video_segments WHERE session_id=? AND deleted=0 ORDER BY seq",
                           (session_id,))
        prots = self.active_protections(session_id)
        return {
            "segments": len(segs),
            "bytes": sum(s["size"] for s in segs),
            "t_first": segs[0]["t_start"] if segs else None,
            "t_last": segs[-1]["t_end"] if segs else None,
            "seconds": (segs[-1]["t_end"] - segs[0]["t_start"]) if segs else 0,
            "protected": [{"id": p["id"], "t_start": p["t_start"], "t_end": p["t_end"], "reason": p["reason"]} for p in prots],
            "protected_segments": sum(1 for s in segs if self._overlaps(s, prots)),
        }

    def disk_status(self) -> dict:
        du = shutil.disk_usage(self.paths.data)
        temp = self.db.scalar("SELECT COALESCE(SUM(size),0) FROM video_segments WHERE deleted=0") or 0
        clips = self.db.scalar("SELECT COALESCE(SUM(file_size),0) FROM clips WHERE file_path IS NOT NULL") or 0
        exports = 0
        for f in self.paths.exports.rglob("*.mp4"):
            try:
                exports += f.stat().st_size
            except OSError:
                pass
        cfg = self.cfg
        level = "ok"
        if du.free < cfg.buffer.disk_critical_free_bytes:
            level = "critical"
        elif du.free < cfg.buffer.disk_warn_free_bytes:
            level = "warn"
        return {"total": du.total, "free": du.free, "temp_bytes": temp, "clip_bytes": clips, "export_bytes": exports,
                "temp_budget": cfg.buffer.max_temp_bytes, "level": level}
