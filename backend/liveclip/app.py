"""FastAPI application: REST control API, WebSocket events, local media.

Security: binds to 127.0.0.1 by default; every request must carry a localhost
Host header (blocks DNS-rebinding) and state-changing requests must come from a
localhost Origin (blocks cross-site requests from web pages)."""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .context import AppContext
from .platforms import SourceError
from .publishing import instagram_status

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class AddStream(BaseModel):
    name: str = Field("", max_length=60)
    url: str = Field(..., max_length=400)
    slot: int | None = Field(None, ge=0, le=3)
    priority: int = Field(2, ge=1, le=4)
    start: bool = False


class PatchStream(BaseModel):
    name: str | None = Field(None, max_length=60)
    priority: int | None = Field(None, ge=1, le=4)
    color: str | None = Field(None, pattern=r"^#[0-9a-fA-F]{6}$")
    facecam: dict | None = None
    default_layout: Literal["smart_crop", "facecam", "letterbox"] | None = None
    usage_notes: str | None = Field(None, max_length=1000)
    language: Literal["en", "ta", "auto"] | None = None
    caption_mode: Literal["tanglish", "native"] | None = None
    clear_facecam: bool = False


class MoveStream(BaseModel):
    slot: int = Field(..., ge=0, le=3)


class ReplaceStream(BaseModel):
    url: str = Field(..., max_length=400)
    name: str | None = Field(None, max_length=60)


class SaveReq(BaseModel):
    seconds: float = Field(60, ge=5, le=120)
    latency_s: float = Field(0, ge=0, le=60)


class MarkReq(BaseModel):
    latency_s: float = Field(0, ge=0, le=60)
    analyze: bool = True


class ConfirmReq(BaseModel):
    confirm: bool = False


class ToggleReq(BaseModel):
    enabled: bool


class PatchClip(BaseModel):
    title: str | None = Field(None, max_length=150)
    tags: list[str] | None = None
    caption_text: str | None = Field(None, max_length=5000)
    trim_start: float | None = Field(None, ge=0)
    trim_end: float | None = Field(None, ge=0)
    layout: Literal["smart_crop", "facecam", "letterbox"] | None = None
    layout_params: dict | None = None
    caption_words: list[dict] | None = None
    caption_style: dict | None = None


class PublishReq(BaseModel):
    target: Literal["youtube", "instagram"]
    description: str = Field("", max_length=4900)


def create_app(ctx: AppContext | None = None, serve_frontend: bool = True) -> FastAPI:
    holder: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        c = ctx or AppContext()
        holder["ctx"] = c
        app.state.ctx = c
        c.hub.bind(asyncio.get_running_loop())
        _quiet_windows_resets(asyncio.get_running_loop())
        rec = c.retention.recover_on_startup()
        c.jobs.recover()
        c.hub.publish("startup", {"recovery": rec}, notable=True)
        c.jobs.start()
        c.manager.start_loops()
        c.resources.start()
        await c.clips.resume_pending()
        if c.config.resume_monitoring:
            resumed = await c.manager.resume_monitoring()
            if resumed:
                c.hub.publish("monitoring_resumed", resumed, notable=True)
        await c.semantic.check()
        sweeper = asyncio.create_task(_sweep_loop(c))
        if not c.tools.ffmpeg:
            c.hub.warn("ffmpeg", "error", "FFmpeg not found. Run scripts/setup.ps1 or set LIVECLIP_FFMPEG.")
        if not c.tools.streamlink:
            c.hub.warn("streamlink", "warn", "Streamlink not found; live platforms can't be captured.")
        try:
            yield
        finally:
            sweeper.cancel()
            await c.manager.stop_all()
            await c.jobs.stop()
            await c.resources.stop()
            c.retention.sweep()

    async def _sweep_loop(c: AppContext) -> None:
        n = 0
        while True:
            await asyncio.sleep(c.config.buffer.cleanup_interval_s)
            n += 1
            try:
                await asyncio.to_thread(c.retention.sweep)
                if n % 30 == 0:
                    await asyncio.to_thread(c.retention.cleanup_orphans)
            except Exception:
                import logging
                logging.getLogger("liveclip").exception("sweep failed")

    app = FastAPI(title="LiveClip AI", version="0.1.0", lifespan=lifespan,
                  description="Local-first multi-stream clipping command center. All endpoints are localhost-only.")

    def C() -> AppContext:
        return holder["ctx"]

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0] if not (request.headers.get("host") or "").startswith("[") \
            else (request.headers.get("host") or "").split("]")[0] + "]"
        if host and host not in LOCAL_HOSTS and host != "testserver":
            return JSONResponse({"detail": "Host not allowed"}, status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                oh = re.sub(r"^https?://", "", origin).rsplit(":", 1)[0]
                if oh not in LOCAL_HOSTS:
                    return JSONResponse({"detail": "Cross-origin request rejected"}, status_code=403)
        resp = await call_next(request)
        return resp

    def err(e: Exception, code: int = 400):
        raise HTTPException(code, str(e))

    # ------------------------------------------------------------ system
    @app.get("/api/system/status", tags=["system"])
    def system_status():
        c = C()
        return {"tools": c.tools.status(), "resources": c.resources.snapshot, "whisper": c.transcriber.info(),
                "semantic": c.semantic.info(), "warnings": list(c.hub.warnings.values()),
                "bandwidth": c.manager.bandwidth.status(), "jobs": c.jobs.queue_snapshot(),
                "active_streams": len(c.manager.sessions), "data_dir": str(c.paths.data),
                "allow_local_sources": c.config.allow_local_sources}

    @app.post("/api/system/shutdown", tags=["system"])
    async def shutdown():
        """Graceful stop: finalizes open candidates, stops capture workers, flushes the DB."""
        import signal

        asyncio.get_running_loop().call_later(0.3, signal.raise_signal, signal.SIGINT)
        return {"ok": True}

    @app.get("/api/config", tags=["system"])
    def get_config():
        return C().config.model_dump()

    @app.put("/api/config", tags=["system"])
    def put_config(patch: dict = Body(...)):
        try:
            return C().update_config(patch).model_dump()
        except Exception as e:
            err(e, 422)

    @app.post("/api/ai/check", tags=["system"])
    async def ai_check():
        return {"semantic": await C().semantic.check(), "whisper": C().transcriber.info()}

    @app.post("/api/ai/warmup", tags=["system"])
    async def ai_warmup():
        c = C()
        try:
            await asyncio.to_thread(c.transcriber._load)
        except Exception as e:
            return {"ok": False, "whisper": c.transcriber.info(), "error": str(e)}
        return {"ok": True, "whisper": c.transcriber.info()}

    # ------------------------------------------------------------ streams
    def stream_view(s: dict) -> dict:
        live = C().manager.sessions.get(s["id"])
        return {**s, "live": live.status() if live else None, "meters": C().manager.meters.get(s["id"])}

    @app.get("/api/streams", tags=["streams"])
    def list_streams():
        return [stream_view(s) for s in C().manager.list_streamers() if s["priority"] < 99]

    @app.post("/api/streams", tags=["streams"])
    async def add_stream(req: AddStream):
        try:
            s = C().manager.add_streamer(req.name, req.url, req.slot, req.priority)
            if req.start:
                await C().manager.start(s["id"])
        except SourceError as e:
            err(e)
        return stream_view(C().manager.get_streamer(s["id"]))

    @app.patch("/api/streams/{sid}", tags=["streams"])
    def patch_stream(sid: str, req: PatchStream):
        f = req.model_dump(exclude_none=True)
        clear = f.pop("clear_facecam", False)
        try:
            s = C().manager.update_streamer(sid, f)
            if clear:
                C().db.execute("UPDATE streamers SET facecam=NULL WHERE id=?", (sid,))
                s = C().manager.get_streamer(sid)
        except (ValueError, KeyError) as e:
            err(e)
        return stream_view(s)

    @app.post("/api/streams/{sid}/move", tags=["streams"])
    def move_stream(sid: str, req: MoveStream):
        try:
            rows = C().manager.move_streamer(sid, req.slot)
        except (ValueError, KeyError) as e:
            err(e)
        return [stream_view(r) for r in rows if r["priority"] < 99]

    @app.delete("/api/streams/{sid}", tags=["streams"])
    async def delete_stream(sid: str):
        await C().manager.remove_streamer(sid)
        return {"ok": True}

    @app.post("/api/streams/{sid}/start", tags=["streams"])
    async def start_stream(sid: str):
        try:
            return await C().manager.start(sid)
        except (SourceError, KeyError) as e:
            err(e)

    @app.post("/api/streams/{sid}/stop", tags=["streams"])
    async def stop_stream(sid: str):
        await C().manager.stop(sid)
        return {"ok": True}

    @app.post("/api/streams/start-all", tags=["streams"])
    async def start_all():
        out = {}
        for s in C().manager.list_streamers():
            if s["priority"] >= 99:
                continue
            try:
                await C().manager.start(s["id"])
                out[s["id"]] = "started"
            except Exception as e:
                out[s["id"]] = str(e)
        return out

    @app.post("/api/streams/stop-all", tags=["streams"])
    async def stop_all():
        for sid in list(C().manager.sessions):
            await C().manager.stop(sid)
        return {"ok": True}

    @app.post("/api/streams/{sid}/replace", tags=["streams"])
    async def replace_stream(sid: str, req: ReplaceStream):
        try:
            return await C().manager.replace(sid, req.url, req.name)
        except SourceError as e:
            err(e)

    @app.get("/api/streams/{sid}/status", tags=["streams"])
    def stream_status(sid: str):
        s = C().manager.get_streamer(sid)
        if not s:
            raise HTTPException(404)
        return stream_view(s)

    @app.get("/api/streams/{sid}/buffer", tags=["streams"])
    def stream_buffer(sid: str):
        live = C().manager.sessions.get(sid)
        if not live:
            raise HTTPException(404, "not monitoring")
        return C().retention.buffer_status(live.session_id)

    @app.post("/api/streams/{sid}/save", tags=["streams"])
    def save_last(sid: str, req: SaveReq):
        try:
            return {"clip_id": C().manager.save_last(sid, req.seconds, req.latency_s)}
        except KeyError as e:
            err(e, 409)

    @app.post("/api/streams/{sid}/mark", tags=["streams"])
    def mark(sid: str, req: MarkReq):
        try:
            return {"clip_id": C().manager.mark(sid, req.latency_s, req.analyze)}
        except KeyError as e:
            err(e, 409)

    @app.post("/api/streams/{sid}/delete-buffer", tags=["streams"])
    def delete_buffer(sid: str, req: ConfirmReq):
        if not req.confirm:
            raise HTTPException(400, "Destructive operation: send {\"confirm\": true}")
        try:
            return C().manager.delete_buffer(sid)
        except KeyError as e:
            err(e, 409)

    @app.post("/api/streams/{sid}/ai", tags=["streams"])
    def toggle_ai(sid: str, req: ToggleReq):
        C().manager.set_ai(sid, req.enabled)
        return {"ai_enabled": req.enabled}

    # ------------------------------------------------------------ events
    @app.get("/api/hype", tags=["events"])
    def list_hype(streamer_id: str | None = None, limit: int = 100):
        q = "SELECT * FROM hype_events"
        args: list = []
        if streamer_id:
            q += " WHERE streamer_id=?"
            args.append(streamer_id)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(min(limit, 500))
        return C().db.all(q, args)

    @app.get("/api/timeline", tags=["events"])
    def timeline(window_s: float = 600):
        """Per-stream segment availability, protections, events and clips in wall-clock time."""
        c = C()
        out = {}
        since = time.time() - window_s
        for sid, live in c.manager.sessions.items():
            o = live.origin_wall or time.time()
            segs = c.db.all("SELECT t_start, t_end FROM video_segments WHERE session_id=? AND deleted=0 ORDER BY seq", (live.session_id,))
            out[sid] = {
                "origin_wall": o,
                "available": [[o + s["t_start"], o + s["t_end"]] for s in segs],
                "protected": [[o + p["t_start"], o + p["t_end"]] for p in c.retention.active_protections(live.session_id)],
                "events": [{"id": e["id"], "t": o + e["t_event"], "kind": e["kind"], "score": e["final_score"] or e["peak_score"],
                            "state": e["state"]} for e in c.db.all("SELECT * FROM hype_events WHERE session_id=? AND created_at>?",
                                                                   (live.session_id, since))],
                "clips": [{"id": k["id"], "t0": o + k["t_start"], "t1": o + k["t_end"], "status": k["status"]}
                          for k in c.db.all("SELECT id, t_start, t_end, status FROM clips WHERE session_id=? AND created_at>?",
                                            (live.session_id, since))],
            }
        return out

    # ------------------------------------------------------------ clips
    @app.get("/api/clips", tags=["clips"])
    def list_clips(streamer_id: str | None = None, status: str | None = None, sort: Literal["date", "score"] = "date",
                   limit: int = 200):
        q = ("SELECT c.id FROM clips c WHERE 1=1")
        args: list = []
        if streamer_id:
            q += " AND c.streamer_id=?"
            args.append(streamer_id)
        if status:
            sts = status.split(",")
            q += f" AND c.status IN ({','.join('?' * len(sts))})"
            args += sts
        else:
            q += " AND c.status <> 'discarded'"
        q += " ORDER BY " + ("COALESCE(c.ai_score,-1) DESC, c.created_at DESC" if sort == "score" else "c.created_at DESC")
        q += " LIMIT ?"
        args.append(min(limit, 1000))
        return [C().clips.get(r["id"]) for r in C().db.all(q, args)]

    @app.get("/api/clips/{cid}", tags=["clips"])
    def get_clip(cid: str):
        c = C().clips.get(cid)
        if not c:
            raise HTTPException(404)
        return c

    @app.patch("/api/clips/{cid}", tags=["clips"])
    def patch_clip(cid: str, req: PatchClip):
        c = C().clips.get(cid)
        if not c:
            raise HTTPException(404)
        f = req.model_dump(exclude_none=True)
        dur = c.get("duration") or 0
        if "trim_start" in f or "trim_end" in f:
            ts = f.get("trim_start", c["trim_start"] or 0)
            te = f.get("trim_end", c["trim_end"] or dur)
            if te - ts < 1 or (dur and te > dur + 0.05):
                raise HTTPException(422, "Trim must be ≥1s and inside the clip")
        if "caption_words" in f:
            for w in f["caption_words"]:
                if not isinstance(w.get("text"), str) or not (0 <= float(w["start"]) <= float(w["end"])):
                    raise HTTPException(422, "caption words need text/start/end")
        if "layout" in f:      # an explicit choice in the editor is kept (no automatic facecam switch)
            f["layout_params"] = {**f.get("layout_params", {}), "layout_locked": True}
        if f.get("layout_params", {}).get("manual_window"):
            ts = f.get("trim_start", c["trim_start"] or 0)
            te = f.get("trim_end", c["trim_end"] or dur)
            if te - ts > C().config.render.max_reel_s + 0.05:
                raise HTTPException(422, f"Reel window must be ≤ {C().config.render.max_reel_s:.0f}s")
        if "layout_params" in f:
            f["layout_params"] = {**(c.get("layout_params") or {}), **f["layout_params"]}
        C().clips.update(cid, **f)
        return C().clips.get(cid)

    @app.post("/api/clips/{cid}/approve", tags=["clips"])
    def approve(cid: str):
        """Approving a clip also exports it (reel + cover + post caption)."""
        C().clips.update(cid, status="approved")
        c = C().clips.get(cid)
        if c and c["files"]["file_path"] and c.get("export_status") not in ("queued", "rendering"):
            C().clips.update(cid, export_status="queued")
            C().jobs.enqueue("render_export", cid, session_id=c["session_id"], streamer_id=c["streamer_id"])
            c = C().clips.get(cid)
        return c

    @app.post("/api/clips/{cid}/reject", tags=["clips"])
    def reject(cid: str):
        C().clips.update(cid, status="rejected")
        return C().clips.get(cid)

    @app.delete("/api/clips/{cid}", tags=["clips"])
    def delete_clip(cid: str, confirm: bool = False):
        if not confirm:
            raise HTTPException(400, "Destructive operation: pass ?confirm=true")
        C().clips.delete(cid)
        return {"ok": True}

    def _need_file(cid: str) -> dict:
        c = C().clips.get(cid)
        if not c:
            raise HTTPException(404)
        if not c["files"]["file_path"]:
            raise HTTPException(409, "Clip source file not available yet")
        return c

    @app.post("/api/clips/{cid}/render-preview", tags=["clips"])
    def render_preview(cid: str):
        c = _need_file(cid)
        return {"job_id": C().jobs.enqueue("render_preview", cid, session_id=c["session_id"], streamer_id=c["streamer_id"])}

    @app.post("/api/clips/{cid}/export", tags=["clips"])
    def export(cid: str):
        c = _need_file(cid)
        C().clips.update(cid, export_status="queued")
        return {"job_id": C().jobs.enqueue("render_export", cid, session_id=c["session_id"], streamer_id=c["streamer_id"])}

    @app.post("/api/clips/{cid}/retranscribe", tags=["clips"])
    def retranscribe(cid: str):
        c = _need_file(cid)
        C().clips.update(cid, caption_words=None)
        return {"job_id": C().jobs.enqueue("transcribe", cid, priority=2, session_id=c["session_id"], streamer_id=c["streamer_id"])}

    @app.post("/api/clips/{cid}/detect-facecam", tags=["clips"])
    async def detect_fc(cid: str):
        from .editing.framing import detect_facecam
        c = _need_file(cid)
        rect = await asyncio.to_thread(detect_facecam, C().tools.ffmpeg, c["file_path"], c["duration"] or 0)
        if not rect:
            return {"facecam": None, "detail": "No stable facecam found (or opencv-python-headless not installed). Draw it manually."}
        return {"facecam": rect}

    @app.post("/api/clips/{cid}/open-folder", tags=["clips"])
    def open_folder(cid: str, which: Literal["export", "source"] = "export"):
        c = C().clips.get(cid)
        p = c and c.get("export_path" if which == "export" else "file_path")
        if not p or not Path(p).exists():
            raise HTTPException(404, "File not found")
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", str(Path(p))])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", str(Path(p))])         # reveal in Finder
        else:
            subprocess.Popen(["xdg-open", str(Path(p).parent)])
        return {"ok": True, "path": p}

    @app.post("/api/clips/{cid}/publish", tags=["clips"])
    async def publish(cid: str, req: PublishReq):
        c = C().clips.get(cid)
        if not c or not c["files"]["export_path"]:
            raise HTTPException(409, "Export the clip first")
        if req.target == "instagram":
            raise HTTPException(501, instagram_status()["detail"])
        st = C().youtube.status()
        if not st.get("authorized"):
            raise HTTPException(409, st["detail"])
        try:
            vid = await C().youtube.upload(c["export_path"], c.get("title") or "Clip", req.description,
                                           c.get("tags") or [])
        except Exception as e:
            C().clips.update(cid, publish_status=f"failed: {str(e)[:200]}")
            raise HTTPException(502, f"Upload failed: {e}")
        C().clips.update(cid, publish_status=f"youtube:{vid}")
        return {"youtube_id": vid, "url": f"https://youtube.com/shorts/{vid}"}

    @app.get("/api/publishing", tags=["clips"])
    def publishing_status():
        return {"local": {"available": True, "dir": str(C().paths.exports)}, "youtube": C().youtube.status(),
                "instagram": instagram_status()}

    @app.post("/api/publishing/youtube/authorize", tags=["clips"])
    async def yt_auth():
        try:
            return await C().youtube.authorize()
        except Exception as e:
            err(e)

    # ------------------------------------------------------------ jobs/storage
    @app.get("/api/jobs", tags=["system"])
    def jobs(limit: int = 50):
        return C().db.all("SELECT * FROM processing_jobs ORDER BY created_at DESC LIMIT ?", (min(limit, 500),))

    @app.get("/api/storage", tags=["system"])
    def storage():
        c = C()
        by_streamer = c.db.all(
            "SELECT s.id, s.name, s.slug, "
            "(SELECT COALESCE(SUM(v.size),0) FROM video_segments v WHERE v.streamer_id=s.id AND v.deleted=0) AS temp_bytes, "
            "(SELECT COALESCE(SUM(k.file_size),0) FROM clips k WHERE k.streamer_id=s.id AND k.file_path IS NOT NULL) AS clip_bytes, "
            "(SELECT COUNT(*) FROM clips k WHERE k.streamer_id=s.id AND k.status<>'discarded') AS clips "
            "FROM streamers s ORDER BY s.slot")
        return {"disk": c.retention.disk_status(), "by_streamer": by_streamer, "paths": {
            "data": str(c.paths.data), "recordings": str(c.paths.recordings), "clips": str(c.paths.clips),
            "exports": str(c.paths.exports)}, "segments_deleted_total": c.retention.deleted_total}

    @app.post("/api/storage/cleanup", tags=["system"])
    async def storage_cleanup(req: ConfirmReq):
        if not req.confirm:
            raise HTTPException(400, "Send {\"confirm\": true}")
        c = C()
        stats = await asyncio.to_thread(c.retention.sweep)
        stats["orphans"] = await asyncio.to_thread(c.retention.cleanup_orphans, 0)
        prev = c.paths.temp / "previews"
        n = 0
        if prev.exists():
            for f in prev.glob("*.mp4"):
                try:
                    f.unlink()
                    n += 1
                except OSError:
                    pass
        stats["previews_removed"] = n
        for k in c.db.all("SELECT id FROM clips WHERE status='discarded'"):
            c.clips.delete(k["id"])
        return stats

    # ------------------------------------------------------------ media
    @app.get("/media/live/{session_id}/index.m3u8", tags=["media"])
    def live_playlist(session_id: str):
        """HLS playlist over the *captured* segments: zero extra downloads, zero transcoding."""
        if not re.fullmatch(r"ses_[0-9a-f]{12}", session_id):
            raise HTTPException(404)
        segs = C().db.all("SELECT seq, run, duration, path FROM video_segments WHERE session_id=? AND deleted=0 "
                          "ORDER BY seq DESC LIMIT 6", (session_id,))
        if len(segs) < 2:
            raise HTTPException(404, "buffering")
        segs.reverse()
        first_run = C().db.scalar("SELECT MIN(run) FROM video_segments WHERE session_id=?", (session_id,)) or 1
        target = math.ceil(max(s["duration"] for s in segs))
        lines = ["#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{target}",
                 f"#EXT-X-MEDIA-SEQUENCE:{segs[0]['seq']}", f"#EXT-X-DISCONTINUITY-SEQUENCE:{segs[0]['run'] - first_run}"]
        prev_run = segs[0]["run"]
        for s in segs:
            if s["run"] != prev_run:
                lines.append("#EXT-X-DISCONTINUITY")
                prev_run = s["run"]
            lines.append(f"#EXTINF:{s['duration']:.3f},")
            lines.append(Path(s["path"]).name)
        return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl",
                        headers={"Cache-Control": "no-store"})

    @app.get("/media/live/{session_id}/{name}", tags=["media"])
    def live_segment(session_id: str, name: str):
        if not re.fullmatch(r"ses_[0-9a-f]{12}", session_id) or not re.fullmatch(r"seg_\d{6}\.ts", name):
            raise HTTPException(404)
        row = C().db.one("SELECT path FROM video_segments WHERE session_id=? AND deleted=0 AND path LIKE ?",
                         (session_id, f"%{name}"))
        if not row or not Path(row["path"]).exists():
            raise HTTPException(404)
        return FileResponse(row["path"], media_type="video/mp2t", headers={"Cache-Control": "no-store"})

    @app.get("/media/clips/{cid}/{kind}", tags=["media"])
    def clip_media(cid: str, kind: Literal["source", "preview", "export", "thumb", "srt", "cover"]):
        c = C().clips.get(cid)
        if not c:
            raise HTTPException(404)
        key = {"source": "file_path", "preview": "preview_path", "export": "export_path", "thumb": "thumb_path",
               "srt": "export_path", "cover": "cover_path"}[kind]
        p = c.get(key)
        if kind == "srt" and p:
            p = str(Path(p).with_suffix(".srt"))
        if not p or not Path(p).exists():
            raise HTTPException(404)
        root = C().paths.data.resolve()
        if root not in Path(p).resolve().parents:
            raise HTTPException(403)
        mt = {"thumb": "image/jpeg", "cover": "image/jpeg", "srt": "text/plain"}.get(kind, "video/mp4")
        return FileResponse(p, media_type=mt, filename=Path(p).name if kind in ("export", "srt", "cover") else None)

    # ------------------------------------------------------------ websocket
    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        host = (sock.headers.get("host") or "").rsplit(":", 1)[0]
        origin = sock.headers.get("origin") or ""
        oh = re.sub(r"^https?://", "", origin).rsplit(":", 1)[0] if origin else ""
        if (host and host not in LOCAL_HOSTS and host != "testserver") or (oh and oh not in LOCAL_HOSTS):
            await sock.close(code=1008)
            return
        await sock.accept()
        c = C()
        q = c.hub.subscribe()
        try:
            await sock.send_text(json.dumps({"type": "hello", "data": {
                "recent": list(c.hub.recent)[-50:], "warnings": list(c.hub.warnings.values())}}, default=str))
            while True:
                msg = await q.get()
                await sock.send_text(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            c.hub.unsubscribe(q)

    # ------------------------------------------------------------ frontend
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if serve_frontend and dist.exists():
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "media/", "ws", "docs", "openapi")):
                raise HTTPException(404)
            f = dist / path
            if path and f.is_file() and dist in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app


def _quiet_windows_resets(loop: asyncio.AbstractEventLoop) -> None:
    """On Windows the Proactor loop logs a full traceback (WinError 10054) every
    time a browser drops a keep-alive connection, e.g. on page reload. It is
    harmless; keep it out of the log and pass everything else through."""
    default = loop.get_exception_handler()

    def handler(lp, context):
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)) and \
                "_call_connection_lost" in str(context.get("handle", "")):
            return
        (default or lp.default_exception_handler)(context) if default else lp.default_exception_handler(context)

    loop.set_exception_handler(handler)
