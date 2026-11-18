"""Application configuration.

Configuration is a validated Pydantic model persisted in the SQLite `settings`
table (key ``config``). Paths and secrets can be overridden through environment
variables (see ``.env.example``) so nothing sensitive is hard-coded.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# Same heavy black-sans caption look on both platforms (Segoe UI Black is Windows-only;
# Arial Black ships with macOS in /System/Library/Fonts/Supplemental).
DEFAULT_CAPTION_FONT = "Arial Black" if sys.platform == "darwin" else "Segoe UI Black"


class BufferConfig(BaseModel):
    segment_seconds: float = Field(5.0, ge=2.0, le=10.0)
    # 180 s: room for viewer clips to appear on Twitch's API and for a late manual save
    retention_seconds: float = Field(180.0, ge=30.0, le=600.0)
    max_temp_bytes: int = Field(5 * 1024**3, ge=256 * 1024**2)
    # Warn when free disk space on the data volume falls below this.
    disk_warn_free_bytes: int = Field(10 * 1024**3, ge=0)
    # Below this free space: aggressive cleanup + lowest-priority captures stop.
    disk_critical_free_bytes: int = Field(3 * 1024**3, ge=0)
    cleanup_interval_s: float = Field(2.0, ge=0.5, le=30)


class ClipConfig(BaseModel):
    pre_context_s: float = Field(30.0, ge=0, le=120)
    post_context_s: float = Field(20.0, ge=0, le=60)
    max_clip_s: float = Field(90.0, ge=10, le=180)
    # Wait at most this long past the event for the reaction to end.
    max_reaction_wait_s: float = Field(30.0, ge=0, le=60)
    keep_discarded_auto_clips: bool = False
    # Near-miss AI clips (score between maybe_threshold and save_threshold) are kept in a
    # "Maybe" pool capped at this many bytes; lowest scores are evicted first.
    maybe_threshold: float = Field(0.25, ge=0.0, le=1.0)
    maybe_budget_bytes: int = Field(3 * 1024**3, ge=0)


class DetectionWeights(BaseModel):
    chat: float = 0.30
    audio: float = 0.25
    transcript: float = 0.15
    laughter: float = 0.10
    novelty: float = 0.10
    semantic: float = 0.10
    speech: float = 0.15        # excited live speech that triggered/accompanied the moment


class DetectionConfig(BaseModel):
    # Layer A trigger threshold on the 0..1 lightweight score.
    trigger_threshold: float = Field(0.62, ge=0.05, le=1.0)
    release_threshold: float = Field(0.40, ge=0.0, le=1.0)
    # Layer B: candidates scoring below this are discarded (auto only).
    save_threshold: float = Field(0.50, ge=0.0, le=1.0)
    merge_window_s: float = Field(25.0, ge=0, le=120)
    cooldown_s: float = Field(20.0, ge=0, le=300)
    min_baseline_s: float = Field(45.0, ge=5, le=600)
    lightweight_audio_weight: float = 0.5
    lightweight_chat_weight: float = 0.5
    # A lone signal counts at this fraction (0.85 * a 0.75 chat spike = 0.64 ≥ trigger)
    single_signal_factor: float = Field(0.85, ge=0.0, le=1.0)
    # Live speech: transcribe a short rolling window of each stream with the shared
    # Whisper model so excited *words* count even when volume and chat stay normal.
    live_speech_enabled: bool = True
    live_speech_interval_s: float = Field(6.0, ge=2.0, le=60.0)
    live_speech_window_s: float = Field(12.0, ge=4.0, le=30.0)
    # YAMNet sound events: laughter/screams count as hype; pure game noise is discounted
    sound_events_enabled: bool = True
    game_noise_threshold: float = Field(0.35, ge=0.0, le=1.0)
    game_noise_discount: float = Field(0.4, ge=0.0, le=1.0)
    # this many distinct viewers typing "clip it"/"!clip" within 15 s forces chat hype
    crowd_clip_calls: int = Field(3, ge=1, le=50)
    # Viewers react in chat a few seconds after the moment they saw.
    chat_reaction_delay_s: float = Field(3.0, ge=0, le=15)
    weights: DetectionWeights = DetectionWeights()
    keywords: list[str] = [
        "lmao", "lol", "lmfao", "wtf", "no way", "bro", "crazy", "clip it", "clip that",
        "omg", "insane", "holy", "w ", "kekw", "omegalul", "lul", "💀", "😂", "🤣", "pog",
        "poggers", "monkas", "sheesh", "what", "nah", "dead", "help",
    ]
    laugh_tokens: list[str] = [
        "lmao", "lmfao", "lol", "kekw", "omegalul", "lul", "haha", "hahaha", "😂", "🤣",
        "💀", "icant", "pepelaugh", "kekl",
    ]


class AIConfig(BaseModel):
    whisper_model: str = "small"
    whisper_device: Literal["auto", "cuda", "cpu"] = "auto"
    whisper_compute_type: str = "auto"  # auto -> float16 on cuda, int8 on cpu
    language: str = "en"
    ollama_enabled: bool = False
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2:3b"
    ollama_timeout_s: float = 45.0
    vision_enabled: bool = False

    @field_validator("ollama_url")
    @classmethod
    def _local_only(cls, v: str) -> str:
        from urllib.parse import urlparse

        host = urlparse(v).hostname or ""
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Ollama URL must point at localhost")
        return v.rstrip("/")


class NetworkConfig(BaseModel):
    link_capacity_mbps: float = Field(30.0, ge=1)
    # Fraction of capacity capture may use. 0.8 * 30 = 24 Mbps.
    safety_fraction: float = Field(0.8, ge=0.1, le=1.0)
    preferred_quality: list[str] = ["1080p60", "1080p", "936p60", "900p60", "720p60", "720p", "best"]
    reduced_quality: list[str] = ["720p60", "720p", "480p", "best"]
    # Rough planning estimates in Mbps until a real measurement exists.
    quality_estimates_mbps: dict[str, float] = {
        "1080p60": 8.0, "1080p": 6.0, "936p60": 6.5, "900p60": 6.0, "720p60": 4.5, "720p": 3.0, "480p": 1.5, "best": 8.0,
    }
    reconnect_base_s: float = 2.0
    reconnect_max_s: float = 60.0
    reconnect_max_attempts: int = 25


class RenderConfig(BaseModel):
    fps: Literal[30, 60] = 60
    encoder: Literal["auto", "nvenc", "videotoolbox", "x264"] = "auto"
    nvenc_cq: int = Field(19, ge=10, le=35)
    x264_crf: int = Field(18, ge=10, le=30)
    max_bitrate_mbps: float = 20.0
    audio_bitrate_k: int = 192
    caption_font: str = DEFAULT_CAPTION_FONT
    caption_fonts_dir: str = ""  # optional extra fonts directory (ttf/otf)
    caption_style: Literal["bold_pop", "clean", "box", "neon"] = "bold_pop"
    caption_size: int = Field(80, ge=40, le=140)
    caption_y: float = Field(0.66, ge=0.2, le=0.82)  # fraction of frame height
    caption_uppercase: bool = True
    hook_text_enabled: bool = True
    hook_seconds: float = 2.6
    max_concurrent_renders: int = Field(1, ge=1, le=2)
    # House rule: reels are 30 s max — the best moment only (people swipe away from 1-min clips).
    # Longer source clips are auto-cut to the strongest <= max_reel_s stretch (+ setup line).
    max_reel_s: float = Field(30.0, ge=10.0, le=90.0)


class PublishingConfig(BaseModel):
    youtube_client_secrets_path: str = ""
    youtube_privacy: Literal["private", "unlisted", "public"] = "private"


class AudioMonitorConfig(BaseModel):
    # hear several streams at once (each has its own volume bar); off = clicking one mutes the rest
    allow_multi: bool = True


class AppConfig(BaseModel):
    buffer: BufferConfig = BufferConfig()
    clips: ClipConfig = ClipConfig()
    detection: DetectionConfig = DetectionConfig()
    ai: AIConfig = AIConfig()
    network: NetworkConfig = NetworkConfig()
    render: RenderConfig = RenderConfig()
    publishing: PublishingConfig = PublishingConfig()
    audio_monitor: AudioMonitorConfig = AudioMonitorConfig()
    # after a restart/crash/reboot, start again every stream that was being monitored when the app stopped
    resume_monitoring: bool = True
    # Allows `local:` fixture sources. Off by default; tests and demos enable it.
    allow_local_sources: bool = False
    shortcuts: dict[str, str] = {
        "select1": "1", "select2": "2", "select3": "3", "select4": "4",
        "mute": "m", "save60": "c", "bookmark": "b", "expand": "e", "toggleAI": "a",
    }


class Paths:
    """Filesystem layout. Every per-stream directory is keyed by streamer slug
    and session id so files from different streamers can never collide."""

    def __init__(self, data_dir: Path):
        self.data = data_dir
        self.database = data_dir / "database"
        self.recordings = data_dir / "recordings"
        self.clips = data_dir / "clips"
        self.exports = data_dir / "exports"
        self.thumbnails = data_dir / "thumbnails"
        self.models = Path(os.environ["LIVECLIP_MODELS_DIR"]) if os.environ.get("LIVECLIP_MODELS_DIR") else data_dir / "models"
        self.logs = data_dir / "logs"
        self.temp = data_dir / "temp"

    def ensure(self) -> None:
        for p in (self.database, self.recordings, self.clips, self.exports, self.thumbnails,
                  self.models, self.logs, self.temp):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def db_file(self) -> Path:
        return self.database / "liveclip.sqlite3"

    def session_dir(self, slug: str, session_id: str) -> Path:
        return self.recordings / slug / session_id

    def segments_dir(self, slug: str, session_id: str) -> Path:
        return self.session_dir(slug, session_id) / "segments"


def _which(env_name: str, exe: str) -> str | None:
    v = os.environ.get(env_name)
    if v and Path(v).exists():
        return v
    found = shutil.which(exe)
    if found:
        return found
    if os.name == "nt":
        # Location used by scripts/setup.ps1
        if os.environ.get("LOCALAPPDATA"):
            local = Path(os.environ["LOCALAPPDATA"]) / "LiveClipTools"
            if local.exists():
                for cand in local.rglob(exe + ".exe"):
                    return str(cand)
    else:
        # Homebrew (Apple Silicon / Intel) — apps launched from Finder don't inherit the shell PATH
        for d in ("/opt/homebrew/bin", "/usr/local/bin"):
            cand = Path(d) / exe
            if cand.exists():
                return str(cand)
    return None


class Tools:
    def __init__(self) -> None:
        self.ffmpeg = _which("LIVECLIP_FFMPEG", "ffmpeg")
        self.ffprobe = _which("LIVECLIP_FFPROBE", "ffprobe")
        self.streamlink = _which("LIVECLIP_STREAMLINK", "streamlink")
        if not self.streamlink:
            # streamlink installed into the same venv as this backend
            import sys

            cand = Path(sys.executable).parent / ("streamlink.exe" if os.name == "nt" else "streamlink")
            if cand.exists():
                self.streamlink = str(cand)

    def status(self) -> dict:
        return {"ffmpeg": self.ffmpeg, "ffprobe": self.ffprobe, "streamlink": self.streamlink}


def default_data_dir() -> Path:
    env = os.environ.get("LIVECLIP_DATA_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return (Path(__file__).resolve().parents[2] / "data").resolve()
