from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from liveclip.config import Tools  # noqa: E402

TOOLS = Tools()
HAVE_FFMPEG = bool(TOOLS.ffmpeg and TOOLS.ffprobe)
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg/ffprobe not found (run scripts/setup.ps1)")

# reuse the Whisper model downloaded by setup.ps1 instead of re-downloading per test data dir
_models = Path(__file__).resolve().parents[2] / "data" / "models"
os.environ.setdefault("LIVECLIP_MODELS_DIR", str(_models))

if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


@pytest.fixture(scope="session")
def fixture_media(tmp_path_factory) -> dict:
    """Short deterministic fixtures: 40 s, loud burst at 22 s."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg not available")
    from liveclip.fixtures import generate

    cache = Path(__file__).resolve().parents[2] / "fixtures" / "test-cache"
    return generate(cache, duration=40, burst=22, names=["fixture_a", "fixture_b"])


def wait_until(pred, timeout: float = 30, interval: float = 0.25, msg: str = "condition"):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = pred()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {msg} (last={last!r})")
