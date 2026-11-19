"""SQLite persistence.

A single connection guarded by an RLock is used from worker threads and the
asyncio loop (calls are short). WAL mode keeps readers from blocking writers.
Every media row carries both ``streamer_id`` and ``session_id`` with foreign
keys so a query can never accidentally join footage across sessions.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS platforms (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS streamers (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL,
  platform TEXT NOT NULL REFERENCES platforms(id),
  url TEXT NOT NULL,
  slot INTEGER NOT NULL DEFAULT 0,
  priority INTEGER NOT NULL DEFAULT 2,
  color TEXT NOT NULL DEFAULT '#8b5cf6',
  ai_enabled INTEGER NOT NULL DEFAULT 1,
  monitoring INTEGER NOT NULL DEFAULT 0,  -- user wants it captured; resumed after an app restart
  language TEXT NOT NULL DEFAULT 'en',    -- en | ta | auto (Whisper language)
  caption_mode TEXT NOT NULL DEFAULT 'tanglish', -- for Tamil: tanglish (English letters) | native (Tamil script)
  facecam TEXT,              -- JSON {x,y,w,h} normalized 0..1
  default_layout TEXT NOT NULL DEFAULT 'smart_crop',
  usage_notes TEXT NOT NULL DEFAULT '',
  vod_after_stream INTEGER NOT NULL DEFAULT 0,  -- analyse the past broadcast after the stream ends
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS stream_sessions (
  id TEXT PRIMARY KEY,
  streamer_id TEXT NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
  platform TEXT NOT NULL,
  url TEXT NOT NULL,
  state TEXT NOT NULL,
  started_at REAL NOT NULL,
  ended_at REAL,
  dir TEXT NOT NULL,
  source_width INTEGER, source_height INTEGER, fps REAL,
  video_codec TEXT, audio_codec TEXT, audio_rate INTEGER, audio_channels INTEGER,
  measured_kbps REAL,
  quality_requested TEXT, quality_selected TEXT,
  reconnects INTEGER NOT NULL DEFAULT 0,
  kind TEXT NOT NULL DEFAULT 'live',   -- live | vod
  error TEXT
);
CREATE INDEX IF NOT EXISTS ix_sessions_streamer ON stream_sessions(streamer_id);

CREATE TABLE IF NOT EXISTS video_segments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
  streamer_id TEXT NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL,
  run INTEGER NOT NULL,
  path TEXT NOT NULL,
  t_start REAL NOT NULL,     -- session media time (s)
  t_end REAL NOT NULL,
  duration REAL NOT NULL,
  size INTEGER NOT NULL,
  created_at REAL NOT NULL,
  deleted INTEGER NOT NULL DEFAULT 0,
  deleted_at REAL,
  corrupt INTEGER NOT NULL DEFAULT 0,
  UNIQUE(session_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_segments_session ON video_segments(session_id, deleted, t_end);

CREATE TABLE IF NOT EXISTS protected_ranges (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
  streamer_id TEXT NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
  t_start REAL NOT NULL,
  t_end REAL NOT NULL,
  reason TEXT NOT NULL,
  ref_type TEXT, ref_id TEXT,
  created_at REAL NOT NULL,
  released_at REAL
);
CREATE INDEX IF NOT EXISTS ix_prot_session ON protected_ranges(session_id, released_at);

CREATE TABLE IF NOT EXISTS audio_analysis (
  session_id TEXT NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
  streamer_id TEXT NOT NULL,
  media_t REAL NOT NULL,
  rms_db REAL, peak_db REAL, excitement REAL, speech REAL,
  PRIMARY KEY(session_id, media_t)
);

CREATE TABLE IF NOT EXISTS chat_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
  streamer_id TEXT NOT NULL,
  wall_t REAL NOT NULL,
  media_t REAL NOT NULL,
  author TEXT, text TEXT NOT NULL,
  emote_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_chat_session ON chat_events(session_id, media_t);

CREATE TABLE IF NOT EXISTS hype_events (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES stream_sessions(id) ON DELETE CASCADE,
  streamer_id TEXT NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,          -- auto | manual_save | mark
  t_event REAL NOT NULL,
  t_start REAL NOT NULL,
  t_end REAL NOT NULL,
  peak_score REAL NOT NULL DEFAULT 0,
  audio_score REAL, chat_score REAL, transcript_score REAL, laughter_score REAL,
  novelty_score REAL, semantic_score REAL, final_score REAL,
  state TEXT NOT NULL,         -- open | finalizing | clipped | discarded | failed
  reason TEXT,
  signals TEXT,                -- JSON
  merged_count INTEGER NOT NULL DEFAULT 1,
  clip_id TEXT,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_hype_session ON hype_events(session_id, t_event);

CREATE TABLE IF NOT EXISTS clips (
  id TEXT PRIMARY KEY,
  streamer_id TEXT NOT NULL REFERENCES streamers(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  hype_event_id TEXT,
  platform TEXT NOT NULL,
  source_url TEXT NOT NULL,
  t_start REAL NOT NULL, t_end REAL NOT NULL,
  wall_start REAL,
  detection_method TEXT NOT NULL,
  ai_score REAL, ai_reason TEXT,
  duration REAL, width INTEGER, height INTEGER, fps REAL,
  file_path TEXT, file_size INTEGER, thumb_path TEXT,
  status TEXT NOT NULL,         -- detected|protected|processing|ready_for_review|approved|rejected|exported|failed|discarded
  export_status TEXT NOT NULL DEFAULT 'none',  -- none|queued|rendering|done|failed
  title TEXT, suggested_titles TEXT, caption_text TEXT, tags TEXT,
  layout TEXT NOT NULL DEFAULT 'smart_crop',
  layout_params TEXT,
  caption_words TEXT,           -- JSON edited words [{text,start,end}] relative to clip
  caption_style TEXT,
  trim_start REAL NOT NULL DEFAULT 0, trim_end REAL,
  preview_path TEXT, export_path TEXT,
  cover_path TEXT,          -- best frame of the exported reel + hook
  post_caption TEXT,        -- ready-to-paste caption + hashtags
  publish_status TEXT,
  error TEXT,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_clips_streamer ON clips(streamer_id, created_at);

CREATE TABLE IF NOT EXISTS transcriptions (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  streamer_id TEXT NOT NULL,
  clip_id TEXT REFERENCES clips(id) ON DELETE CASCADE,
  t_start REAL NOT NULL, t_end REAL NOT NULL,
  language TEXT, text TEXT NOT NULL,
  words TEXT NOT NULL,           -- JSON [{text,start,end,prob}] relative to clip start
  model TEXT, avg_logprob REAL,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,             -- extract|transcribe|score|render_preview|render_export|publish
  ref_id TEXT NOT NULL,
  session_id TEXT, streamer_id TEXT,
  priority INTEGER NOT NULL,
  state TEXT NOT NULL,            -- queued|running|done|failed|cancelled
  attempts INTEGER NOT NULL DEFAULT 0,
  payload TEXT,
  error TEXT,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_jobs_state ON processing_jobs(state, priority, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_ref ON processing_jobs(kind, ref_id);

CREATE TABLE IF NOT EXISTS export_jobs (
  id TEXT PRIMARY KEY,
  clip_id TEXT NOT NULL REFERENCES clips(id) ON DELETE CASCADE,
  target TEXT NOT NULL,           -- local|youtube|instagram
  state TEXT NOT NULL,
  output_path TEXT, remote_id TEXT, error TEXT,
  created_at REAL NOT NULL, finished_at REAL
);

CREATE TABLE IF NOT EXISTS vod_jobs (
  id TEXT PRIMARY KEY,
  streamer_id TEXT REFERENCES streamers(id) ON DELETE SET NULL,
  session_id TEXT,
  source TEXT NOT NULL,           -- manual | after_stream
  platform TEXT NOT NULL,         -- twitch | youtube | kick | local
  url TEXT, path TEXT, title TEXT,
  duration REAL,
  state TEXT NOT NULL,            -- queued|waiting_vod|downloading|analyzing|cutting|done|failed|cancelled
  progress REAL NOT NULL DEFAULT 0,
  detail TEXT,
  moments INTEGER NOT NULL DEFAULT 0,
  clips INTEGER NOT NULL DEFAULT 0,
  not_before REAL,
  payload TEXT,                   -- JSON (after_stream: live window to look for)
  error TEXT,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL
);
CREATE INDEX IF NOT EXISTS ix_vod_state ON vod_jobs(state, created_at);

CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

INSERT OR IGNORE INTO platforms(id, name) VALUES
  ('twitch','Twitch'), ('youtube','YouTube Live'), ('kick','Kick'), ('local','Local file');
"""


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (CREATE TABLE IF NOT EXISTS skips them)."""
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(streamers)")}
        if "monitoring" not in cols:
            self._conn.execute("ALTER TABLE streamers ADD COLUMN monitoring INTEGER NOT NULL DEFAULT 0")
        if cols and "language" not in cols:
            self._conn.execute("ALTER TABLE streamers ADD COLUMN language TEXT NOT NULL DEFAULT 'en'")
        if cols and "caption_mode" not in cols:
            self._conn.execute("ALTER TABLE streamers ADD COLUMN caption_mode TEXT NOT NULL DEFAULT 'tanglish'")
        if cols and "vod_after_stream" not in cols:
            # analyse the whole past broadcast once the stream ends (moments missed live)
            self._conn.execute("ALTER TABLE streamers ADD COLUMN vod_after_stream INTEGER NOT NULL DEFAULT 0")
        scols = {r[1] for r in self._conn.execute("PRAGMA table_info(stream_sessions)")}
        if scols and "kind" not in scols:
            self._conn.execute("ALTER TABLE stream_sessions ADD COLUMN kind TEXT NOT NULL DEFAULT 'live'")
        ccols = {r[1] for r in self._conn.execute("PRAGMA table_info(clips)")}
        for col in ("cover_path", "post_caption"):
            if ccols and col not in ccols:
                self._conn.execute(f"ALTER TABLE clips ADD COLUMN {col} TEXT")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- helpers -------------------------------------------------------
    def execute(self, sql: str, params: Iterable[Any] | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def transaction(self):
        db = self

        class _Tx:
            def __enter__(self_inner):
                db._lock.acquire()
                db._conn.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(self_inner, exc_type, exc, tb):
                try:
                    db._conn.execute("ROLLBACK" if exc_type else "COMMIT")
                finally:
                    db._lock.release()
                return False

        return _Tx()

    def one(self, sql: str, params: Iterable[Any] | dict = ()) -> dict | None:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
        return dict(r) if r else None

    def all(self, sql: str, params: Iterable[Any] | dict = ()) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def scalar(self, sql: str, params: Iterable[Any] | dict = ()) -> Any:
        with self._lock:
            r = self._conn.execute(sql, params).fetchone()
        return r[0] if r else None

    def insert(self, table: str, row: dict) -> None:
        cols = ",".join(row)
        qs = ",".join("?" for _ in row)
        self.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", list(row.values()))

    def update(self, table: str, key: str, key_val: Any, fields: dict) -> None:
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE {table} SET {sets} WHERE {key}=?", [*fields.values(), key_val])

    # --- settings ------------------------------------------------------
    def get_setting(self, key: str, default: Any = None) -> Any:
        v = self.scalar("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(v) if v is not None else default

    def set_setting(self, key: str, value: Any) -> None:
        self.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (key, json.dumps(value)))


def now() -> float:
    return time.time()
