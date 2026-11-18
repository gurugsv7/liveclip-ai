"""Shared faster-whisper transcription service.

Exactly one model instance is loaded (lazily) and shared by all streams; jobs
are serialized by the job runner (concurrency 1), and each call carries the
clip/session/streamer ids of its caller so results return to the right place.
CUDA is preferred; if CUDA libraries are missing or initialization fails the
service falls back to CPU int8 and reports why.
"""
from __future__ import annotations

import glob
import logging
import os
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("liveclip.whisper")


def _add_nvidia_dll_dirs() -> None:
    """pip-installed nvidia-cublas/cudnn wheels put DLLs in site-packages/nvidia/*/bin."""
    if os.name != "nt":
        return
    for sp in sys.path:
        for d in glob.glob(os.path.join(sp, "nvidia", "*", "bin")):
            try:
                os.add_dll_directory(d)
                os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            except (OSError, AttributeError):
                pass


class TranscriptionUnavailable(RuntimeError):
    pass


class Transcriber:
    def __init__(self, cfg_getter, models_dir: Path):
        self.cfg_getter = cfg_getter
        self.models_dir = models_dir
        self.model = None
        self.device = None
        self.compute_type = None
        self.status = "not_loaded"
        self.detail = ""
        self.loaded_name = None
        self._lock = threading.Lock()
        self.jobs_done = 0
        self.last_ms = None
        self.live_windows = 0
        self.live_last_ms = None

    @staticmethod
    def installed() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except Exception:
            return False

    def _load(self) -> None:
        cfg = self.cfg_getter().ai
        if self.model is not None and self.loaded_name == cfg.whisper_model:
            return
        if not self.installed():
            self.status = "unavailable"
            self.detail = "faster-whisper not installed (pip install -e .[ai])"
            raise TranscriptionUnavailable(self.detail)
        _add_nvidia_dll_dirs()
        from faster_whisper import WhisperModel

        self.status = "loading"
        attempts: list[tuple[str, str]] = []
        want = cfg.whisper_device
        if want in ("auto", "cuda"):
            attempts.append(("cuda", "float16" if cfg.whisper_compute_type == "auto" else cfg.whisper_compute_type))
        if want in ("auto", "cpu"):
            attempts.append(("cpu", "int8" if cfg.whisper_compute_type == "auto" else cfg.whisper_compute_type))
        errors = []
        for device, ctype in attempts:
            try:
                m = WhisperModel(cfg.whisper_model, device=device, compute_type=ctype,
                                 download_root=str(self.models_dir), cpu_threads=4)
                # Force a tiny inference so CUDA DLL problems surface now, not mid-job.
                import numpy as np

                list(m.transcribe(np.zeros(16000, dtype=np.float32), language=cfg.language)[0])
                self.model, self.device, self.compute_type = m, device, ctype
                self.loaded_name = cfg.whisper_model
                self.status = "ready"
                self.detail = f"{cfg.whisper_model} on {device} ({ctype})"
                if errors:
                    self.detail += f"; GPU unavailable: {errors[0][:160]}"
                log.info("whisper loaded: %s", self.detail)
                return
            except Exception as e:  # CUDA init / DLL / download failures
                errors.append(f"{device}: {type(e).__name__}: {e}")
                log.warning("whisper load failed on %s: %s", device, e)
        self.status = "error"
        self.detail = "; ".join(errors)[:400]
        raise TranscriptionUnavailable(self.detail)

    def transcribe(self, wav_path: str, *, streamer_id: str, session_id: str, clip_id: str) -> dict:
        """Returns {text, words:[{text,start,end,prob}], language, avg_logprob, model, ids...}.
        Segments that look like silence hallucinations are dropped, never padded."""
        with self._lock:
            self._load()
            cfg = self.cfg_getter().ai
            t0 = time.time()
            segments, info = self.model.transcribe(
                wav_path, language=cfg.language or None, beam_size=5, word_timestamps=True,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 400},
                condition_on_previous_text=False, no_speech_threshold=0.6,
            )
            words, texts, logprobs = [], [], []
            for seg in segments:
                if seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0:
                    continue
                if seg.avg_logprob < -1.4:
                    continue
                texts.append(seg.text.strip())
                logprobs.append(seg.avg_logprob)
                for w in seg.words or []:
                    txt = w.word.strip()
                    if txt:
                        words.append({"text": txt, "start": round(w.start, 3), "end": round(w.end, 3),
                                      "prob": round(w.probability, 3)})
            self.jobs_done += 1
            self.last_ms = int((time.time() - t0) * 1000)
            return {
                "streamer_id": streamer_id, "session_id": session_id, "clip_id": clip_id,
                "text": " ".join(texts).strip(), "words": words, "language": info.language,
                "avg_logprob": (sum(logprobs) / len(logprobs)) if logprobs else None,
                "model": f"faster-whisper/{self.loaded_name}/{self.device}",
            }

    def transcribe_pcm(self, pcm, *, streamer_id: str, session_id: str) -> dict:
        """Fast pass over a short live window (16 kHz int16 PCM from that stream's own
        ring buffer). Same shared model; greedy decoding to keep it ~0.2-0.5 s on GPU.
        Word times are relative to the start of the window."""
        import numpy as np

        with self._lock:
            self._load()
            cfg = self.cfg_getter().ai
            t0 = time.time()
            audio = np.asarray(pcm, dtype=np.float32) / 32768.0
            segments, _ = self.model.transcribe(
                audio, language=cfg.language or None, beam_size=1, best_of=1, word_timestamps=True,
                vad_filter=True, vad_parameters={"min_silence_duration_ms": 300},
                condition_on_previous_text=False, no_speech_threshold=0.6, temperature=0.0,
            )
            words, texts = [], []
            for seg in segments:
                if (seg.no_speech_prob > 0.6 and seg.avg_logprob < -1.0) or seg.avg_logprob < -1.4:
                    continue
                texts.append(seg.text.strip())
                for w in seg.words or []:
                    if w.word.strip():
                        words.append({"text": w.word.strip(), "start": round(w.start, 3), "end": round(w.end, 3)})
            self.live_windows += 1
            self.live_last_ms = int((time.time() - t0) * 1000)
            return {"streamer_id": streamer_id, "session_id": session_id, "text": " ".join(texts).strip(), "words": words}

    def info(self) -> dict:
        return {"status": self.status if self.installed() else "unavailable",
                "detail": self.detail or ("faster-whisper not installed" if not self.installed() else ""),
                "device": self.device, "model": self.loaded_name, "jobs_done": self.jobs_done, "last_ms": self.last_ms,
                "live_windows": self.live_windows, "live_last_ms": self.live_last_ms}
