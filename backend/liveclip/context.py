"""Application context: wires together every service."""
from __future__ import annotations

import logging
import logging.handlers
import os
from pathlib import Path

from .ai.semantic import SemanticScorer
from .ai.transcriber import Transcriber
from .config import AppConfig, Paths, Tools, default_data_dir
from .db import Database
from .events import EventHub
from .jobs import JobRunner
from .storage.retention import RetentionManager


def deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class AppContext:
    def __init__(self, data_dir: Path | None = None, fixtures_dir: Path | None = None, config_overrides: dict | None = None):
        self.paths = Paths(Path(data_dir) if data_dir else default_data_dir())
        self.paths.ensure()
        self._setup_logging()
        self.db = Database(self.paths.db_file)
        stored = self.db.get_setting("config", {}) or {}
        if config_overrides:
            stored = deep_merge(stored, config_overrides)
        self.config = AppConfig.model_validate(stored)
        if os.environ.get("LIVECLIP_ALLOW_LOCAL") == "1":
            self.config.allow_local_sources = True
        self.db.set_setting("config", self.config.model_dump())
        self.tools = Tools()
        self.fixtures_dir = Path(fixtures_dir) if fixtures_dir else Path(
            os.environ.get("LIVECLIP_FIXTURES_DIR", Path(__file__).resolve().parents[2] / "fixtures" / "media"))
        self.hub = EventHub()
        self.retention = RetentionManager(self.db, lambda: self.config, self.paths, self.hub)
        self.jobs = JobRunner(self.db, self.hub, limits={
            "extract": 2, "transcribe": 1, "score": 1, "render_preview": 1,
            "render_export": self.config.render.max_concurrent_renders, "publish": 1})
        self.transcriber = Transcriber(lambda: self.config, self.paths.models)
        self.semantic = SemanticScorer(lambda: self.config)
        from .detection.sound_events import SoundEventClassifier
        self.sound = SoundEventClassifier(self.paths.models)
        # late imports to avoid cycles
        from .capture.manager import StreamManager
        from .clips.service import ClipService
        from .publishing import YouTubePublisher
        from .resources import ResourceMonitor

        self.manager = StreamManager(self)
        self.clips = ClipService(self)
        self.resources = ResourceMonitor(self)
        self.youtube = YouTubePublisher(self)
        from .vod.service import VodService
        self.vod = VodService(self)

    def _setup_logging(self) -> None:
        root = logging.getLogger()
        if any(getattr(h, "_liveclip", False) for h in root.handlers):
            return
        root.setLevel(logging.INFO)
        fh = logging.handlers.RotatingFileHandler(self.paths.logs / "liveclip.log", maxBytes=5_000_000, backupCount=5,
                                                  encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
        fh._liveclip = True  # type: ignore[attr-defined]
        root.addHandler(fh)

    def update_config(self, patch: dict) -> AppConfig:
        merged = deep_merge(self.config.model_dump(), patch)
        cfg = AppConfig.model_validate(merged)
        self.config = cfg
        self.db.set_setting("config", cfg.model_dump())
        self.jobs.limits["render_export"] = cfg.render.max_concurrent_renders
        # apply detection thresholds to running sessions immediately
        for live in self.manager.sessions.values():
            p = live.detector.p
            d, c = cfg.detection, cfg.clips
            p.trigger, p.release, p.merge_window_s, p.cooldown_s = d.trigger_threshold, d.release_threshold, d.merge_window_s, d.cooldown_s
            p.pre_s, p.post_s, p.max_wait_s, p.max_clip_s = c.pre_context_s, c.post_context_s, c.max_reaction_wait_s, c.max_clip_s
            p.w_audio, p.w_chat, p.min_baseline_s = d.lightweight_audio_weight, d.lightweight_chat_weight, d.min_baseline_s
            p.single_signal_factor = d.single_signal_factor
        return cfg
