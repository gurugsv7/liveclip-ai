"""DB-backed background job queue.

Jobs are persisted before they run, so an app crash leaves a record that is
re-queued on restart (handlers are idempotent). Each job kind has its own
concurrency limit, which is how heavy GPU work is kept away from capture:
  extract 2 · transcribe 1 (one shared Whisper) · score 1 · render 1 · publish 1
Lower `priority` numbers run first.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import traceback
from typing import Awaitable, Callable

from .db import Database, new_id
from .events import EventHub

log = logging.getLogger("liveclip.jobs")

PRIORITY = {"extract_manual": 0, "extract": 1, "transcribe_manual": 2, "transcribe": 3, "score": 4,
            "render_preview": 5, "render_export": 6, "publish": 7}

Handler = Callable[[dict], Awaitable[None]]


class RetryableError(RuntimeError):
    """Raise from a handler to re-queue the job after a delay."""

    def __init__(self, msg: str, delay: float = 2.0):
        super().__init__(msg)
        self.delay = delay


class JobRunner:
    def __init__(self, db: Database, hub: EventHub, limits: dict[str, int] | None = None, max_attempts: int = 3):
        self.db = db
        self.hub = hub
        self.handlers: dict[str, Handler] = {}
        self.limits = limits or {"extract": 2, "transcribe": 1, "score": 1, "render_preview": 1,
                                 "render_export": 1, "publish": 1}
        self.running: dict[str, int] = {k: 0 for k in self.limits}
        self.max_attempts = max_attempts
        self._wake = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._inflight: set[asyncio.Task] = set()
        self.paused_kinds: set[str] = set()
        self.not_before: dict[str, float] = {}

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler
        self.limits.setdefault(kind, 1)
        self.running.setdefault(kind, 0)

    def enqueue(self, kind: str, ref_id: str, *, priority: int | None = None, session_id: str | None = None,
                streamer_id: str | None = None, payload: dict | None = None, dedupe: bool = True) -> str:
        if dedupe:
            existing = self.db.one("SELECT id FROM processing_jobs WHERE kind=? AND ref_id=? AND state IN ('queued','running')",
                                   (kind, ref_id))
            if existing:
                return existing["id"]
        jid = new_id("job_")
        self.db.insert("processing_jobs", {
            "id": jid, "kind": kind, "ref_id": ref_id, "session_id": session_id, "streamer_id": streamer_id,
            "priority": PRIORITY.get(kind, 5) if priority is None else priority, "state": "queued",
            "payload": json.dumps(payload or {}), "created_at": time.time(),
        })
        self.hub.publish("job", {"id": jid, "kind": kind, "ref_id": ref_id, "state": "queued", "streamer_id": streamer_id})
        self._poke()
        return jid

    def _poke(self) -> None:
        """Wake the dispatcher; safe to call from worker threads (FastAPI sync endpoints)."""
        loop = self._loop
        if loop is None:
            return
        try:
            if asyncio.get_running_loop() is loop:
                self._wake.set()
                return
        except RuntimeError:
            pass
        try:
            loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:
            pass

    def recover(self) -> int:
        """Re-queue jobs that were running when the app stopped."""
        return self.db.execute("UPDATE processing_jobs SET state='queued', started_at=NULL WHERE state='running'").rowcount

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.create_task(self._run_loop(), name="job-runner")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        for t in list(self._inflight):
            t.cancel()

    async def _run_loop(self) -> None:
        while True:
            try:
                self._dispatch()
            except Exception:
                log.exception("dispatch failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass

    def _dispatch(self) -> None:
        now = time.time()
        rows = self.db.all("SELECT * FROM processing_jobs WHERE state='queued' ORDER BY priority, created_at LIMIT 100")
        for job in rows:
            kind = job["kind"]
            if kind not in self.handlers or kind in self.paused_kinds:
                continue
            if self.not_before.get(job["id"], 0) > now:
                continue
            if self.running.get(kind, 0) >= self.limits.get(kind, 1):
                continue
            self.running[kind] = self.running.get(kind, 0) + 1
            self.db.execute("UPDATE processing_jobs SET state='running', started_at=?, attempts=attempts+1 WHERE id=?",
                            (now, job["id"]))
            job["attempts"] += 1
            t = asyncio.create_task(self._run(job))
            self._inflight.add(t)
            t.add_done_callback(self._inflight.discard)

    async def _run(self, job: dict) -> None:
        kind = job["kind"]
        job["payload"] = json.loads(job["payload"] or "{}")
        self.hub.publish("job", {"id": job["id"], "kind": kind, "ref_id": job["ref_id"], "state": "running",
                                 "streamer_id": job["streamer_id"]})
        try:
            await self.handlers[kind](job)
            self.db.execute("UPDATE processing_jobs SET state='done', finished_at=?, error=NULL WHERE id=?",
                            (time.time(), job["id"]))
            state = "done"
        except RetryableError as e:
            self.db.execute("UPDATE processing_jobs SET state='queued', attempts=attempts-1, error=? WHERE id=?",
                            (str(e)[:500], job["id"]))
            self.not_before[job["id"]] = time.time() + e.delay
            state = "queued"
        except asyncio.CancelledError:
            self.db.execute("UPDATE processing_jobs SET state='queued' WHERE id=?", (job["id"],))
            raise
        except Exception as e:
            log.error("job %s %s failed: %s\n%s", kind, job["ref_id"], e, traceback.format_exc())
            if job["attempts"] < self.max_attempts and not getattr(e, "permanent", False):
                self.db.execute("UPDATE processing_jobs SET state='queued', error=? WHERE id=?", (str(e)[:500], job["id"]))
                self.not_before[job["id"]] = time.time() + 3 * job["attempts"]
                state = "queued"
            else:
                self.db.execute("UPDATE processing_jobs SET state='failed', finished_at=?, error=? WHERE id=?",
                                (time.time(), str(e)[:500], job["id"]))
                state = "failed"
                fail = self.handlers.get(kind + ":failed")
                if fail:
                    try:
                        await fail(job | {"error": str(e)})
                    except Exception:
                        log.exception("failure hook failed")
        finally:
            self.running[kind] -= 1
            self._wake.set()
        self.hub.publish("job", {"id": job["id"], "kind": kind, "ref_id": job["ref_id"], "state": state,
                                 "streamer_id": job["streamer_id"]})

    def queue_snapshot(self) -> list[dict]:
        return self.db.all("SELECT id, kind, ref_id, streamer_id, state, priority, attempts, error, created_at, started_at "
                           "FROM processing_jobs WHERE state IN ('queued','running') ORDER BY priority, created_at LIMIT 50")


class PermanentError(RuntimeError):
    permanent = True
