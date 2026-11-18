"""Optional Layer C: local LLM scoring through Ollama (disabled by default).

The model must return JSON; it is validated with Pydantic and discarded if it
doesn't match. A slow or missing Ollama never blocks recording or the
lightweight pipeline — callers treat any failure as "no semantic score".
"""
from __future__ import annotations

import json
import logging

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

log = logging.getLogger("liveclip.semantic")

CATEGORIES = ["comedy", "punchline", "reveal", "reaction", "argument", "achievement", "failure", "discovery",
              "npc", "other"]


class SemanticResult(BaseModel):
    score: float = Field(ge=0, le=1)
    category: str = "other"
    reason: str = Field(default="", max_length=400)
    start: float | None = None
    end: float | None = None
    titles: list[str] = Field(default_factory=list, max_length=5)

    @field_validator("category")
    @classmethod
    def _cat(cls, v: str) -> str:
        v = (v or "other").lower().strip()
        return v if v in CATEGORIES else "other"

    @field_validator("titles", mode="before")
    @classmethod
    def _titles(cls, v) -> list[str]:
        if not isinstance(v, list):
            return []
        return [t.strip()[:100] for t in v if isinstance(t, str) and t.strip()][:5]


PROMPT = """You rate livestream moments for short-form vertical clips (YouTube Shorts / Reels).
Streamer: {streamer}
Clip length: {duration:.0f}s. The detected peak is at {peak:.1f}s.
Transcript with timestamps (seconds from clip start):
{transcript}

Chat messages during the moment (may be empty):
{chat}

Signals: audio excitement {audio:.2f}, chat excitement {chat_score:.2f}.

Return ONLY JSON: {{"score": 0..1 viral potential, "category": one of {cats}, "reason": short explanation,
"start": best clip start seconds, "end": best clip end seconds, "titles": up to 3 short title ideas grounded in the transcript}}.
Do not invent dialogue that is not in the transcript."""


class SemanticScorer:
    def __init__(self, cfg_getter):
        self.cfg_getter = cfg_getter
        self.status = "disabled"
        self.detail = ""

    async def check(self) -> dict:
        cfg = self.cfg_getter().ai
        if not cfg.ollama_enabled:
            self.status, self.detail = "disabled", "Enable in Settings → AI to use a local LLM"
            return self.info()
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(f"{cfg.ollama_url}/api/tags")
                r.raise_for_status()
                names = [m.get("name") for m in r.json().get("models", [])]
            if not any(n and n.split(":")[0] == cfg.ollama_model.split(":")[0] for n in names):
                self.status, self.detail = "unavailable", f"Model {cfg.ollama_model} not pulled (ollama pull {cfg.ollama_model})"
            else:
                self.status, self.detail = "ready", cfg.ollama_model
        except Exception as e:
            self.status, self.detail = "unavailable", f"Ollama not reachable: {type(e).__name__}"
        return self.info()

    def info(self) -> dict:
        return {"status": self.status, "detail": self.detail}

    async def score(self, *, streamer: str, duration: float, peak: float, words: list[dict], chat: list[str],
                    audio: float, chat_score: float) -> SemanticResult | None:
        cfg = self.cfg_getter().ai
        if not cfg.ollama_enabled:
            return None
        lines, cur, cur_t = [], [], None
        for w in words:
            if cur_t is None:
                cur_t = w["start"]
            cur.append(w["text"])
            if len(cur) >= 12:
                lines.append(f"[{cur_t:.1f}] {' '.join(cur)}")
                cur, cur_t = [], None
        if cur:
            lines.append(f"[{cur_t:.1f}] {' '.join(cur)}")
        prompt = PROMPT.format(streamer=streamer, duration=duration, peak=peak,
                               transcript="\n".join(lines) or "(no clear speech)",
                               chat="\n".join(chat[-30:]) or "(none)", audio=audio, chat_score=chat_score,
                               cats=", ".join(CATEGORIES))
        try:
            async with httpx.AsyncClient(timeout=cfg.ollama_timeout_s) as c:
                r = await c.post(f"{cfg.ollama_url}/api/generate", json={
                    "model": cfg.ollama_model, "prompt": prompt, "format": "json", "stream": False,
                    "options": {"temperature": 0.2, "num_ctx": 4096},
                })
                r.raise_for_status()
                raw = r.json().get("response", "")
            data = json.loads(raw)
            if isinstance(data.get("score"), (int, float)) and data["score"] > 1:
                data["score"] = min(1.0, data["score"] / 10)  # tolerate 0-10 scales
            res = SemanticResult.model_validate(data)
            if res.start is not None and res.end is not None:
                if not (0 <= res.start < res.end <= duration + 0.5):
                    res.start = res.end = None
            self.status = "ready"
            return res
        except (httpx.HTTPError, json.JSONDecodeError, ValidationError, KeyError, TypeError) as e:
            self.status, self.detail = "error", f"{type(e).__name__}: {str(e)[:160]}"
            log.warning("semantic scoring failed: %s", self.detail)
            return None
