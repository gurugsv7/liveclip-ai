"""Sound-event classification with Google's YAMNet (AudioSet, 521 classes;
Apache-2.0; ~4M params; ONNX on CPU — a 2 s window takes a few ms).

Two jobs in the hype pipeline:
  * reaction  — laughter / screaming / shouting / cheering probability. Unlike raw
                loudness this is about *what* the sound is, so it works for quiet
                streamers and doesn't care how loud the game is.
  * game noise — gunfire / explosions / engines / music. When a loudness spike is
                just the game (GTA shootouts, car chases) and nobody is reacting,
                the loudness signal is discounted instead of firing a clip.

One shared session for all streams; every call is given that stream's own PCM.
Missing model or onnxruntime => disabled, everything else keeps working.
"""
from __future__ import annotations

import csv
import logging
import threading
from pathlib import Path

import numpy as np

log = logging.getLogger("liveclip.sound")

# Human vocal reactions only. Applause/cheering/crowd are left out on purpose: in games they
# are usually in-game sounds (real case: R6 gunfire tagged "Applause 0.80" while the streamer
# sat silent), not the streamer reacting.
REACTION = ["Shout", "Bellow", "Whoop", "Yell", "Screaming", "Laughter", "Giggle", "Snicker", "Belly laugh",
            "Chuckle, chortle", "Groan", "Wail, moan", "Crying, sobbing"]
# anything that means "a human is voicing" (talking, yelling, raging, laughing)
VOICE = ["Speech", "Conversation", "Narration, monologue", "Shout", "Bellow", "Whoop", "Yell", "Screaming",
         "Laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle", "Groan", "Wail, moan", "Crying, sobbing"]
LAUGH = ["Laughter", "Giggle", "Snicker", "Belly laugh", "Chuckle, chortle"]
SCREAM = ["Shout", "Bellow", "Yell", "Screaming", "Whoop"]
GAME_NOISE = ["Explosion", "Gunshot, gunfire", "Machine gun", "Fusillade", "Artillery fire", "Burst, pop", "Boom",
              "Vehicle", "Motor vehicle (road)", "Car", "Engine", "Accelerating, revving, vroom", "Skidding",
              "Tire squeal", "Race car, auto racing", "Siren", "Police car (siren)", "Helicopter", "Glass", "Shatter",
              "Music", "Video game music", "Musical instrument"]

MODEL_URL = "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet.onnx"
MAP_URL = "https://huggingface.co/audiomagic/yamnet-onnx/resolve/main/yamnet_class_map.csv"


def reaction_score(p: float) -> float:
    """Map a class probability to a 0..1 hype score (0.15 -> 0, 0.65+ -> 1)."""
    return float(min(1.0, max(0.0, (p - 0.15) / 0.5)))


class SoundEventClassifier:
    def __init__(self, models_dir: Path):
        self.dir = models_dir / "yamnet"
        self._lock = threading.Lock()
        self.sess = None
        self.status = "not_loaded"
        self.detail = ""
        self.idx: dict[str, list[int]] = {}
        self.names: list[str] = []
        self.calls = 0

    def _load(self) -> bool:
        if self.sess is not None:
            return True
        if self.status == "unavailable":
            return False
        model, cmap = self.dir / "yamnet.onnx", self.dir / "yamnet_class_map.csv"
        if not model.exists() or not cmap.exists():
            self.status, self.detail = "unavailable", f"YAMNet model missing in {self.dir} (run scripts\\setup.ps1)"
            return False
        try:
            import onnxruntime as ort

            so = ort.SessionOptions()
            so.intra_op_num_threads = 1          # tiny model; don't compete with capture
            self.sess = ort.InferenceSession(str(model), sess_options=so, providers=["CPUExecutionProvider"])
            with open(cmap, encoding="utf-8") as f:
                self.names = [r["display_name"] for r in csv.DictReader(f)]
            self.idx = {k: [self.names.index(n) for n in group if n in self.names]
                        for k, group in (("reaction", REACTION), ("laugh", LAUGH), ("scream", SCREAM), ("game", GAME_NOISE),
                                     ("voice", VOICE))}
            self.status, self.detail = "ready", "YAMNet (CPU)"
            return True
        except Exception as e:
            self.status, self.detail = "unavailable", f"{type(e).__name__}: {e}"[:200]
            log.warning("sound classifier unavailable: %s", self.detail)
            return False

    def classify(self, pcm: np.ndarray) -> dict | None:
        """pcm: int16 mono 16 kHz. Returns per-window maxima and the time (s from
        window start) of the strongest reaction frame; None if unavailable."""
        if pcm is None or len(pcm) < 16000:
            return None
        with self._lock:
            if not self._load():
                return None
            wave = np.asarray(pcm, dtype=np.float32) / 32768.0
            scores = self.sess.run(None, {"waveform": wave})[0]          # [frames, 521], 0.48 s hop
            self.calls += 1
        r = scores[:, self.idx["reaction"]].max(axis=1)
        f = int(np.argmax(r))
        top = int(np.argmax(scores[f]))
        return {
            "reaction": float(r[f]),
            "laugh": float(scores[:, self.idx["laugh"]].max()),
            "scream": float(scores[:, self.idx["scream"]].max()),
            "game": float(scores[:, self.idx["game"]].max()),
            "voice": float(scores[:, self.idx["voice"]].max(axis=1).mean()),
            "peak_offset": f * 0.48 + 0.48,
            "top": self.names[top],
        }

    def timeline(self, pcm: np.ndarray) -> list[dict] | None:
        """Per-frame (0.48 s hop) view of a whole clip: t, voice, reaction, game, rms_db.
        Used by the reel cutter so garbled yelling (no Whisper words) isn't mistaken for silence."""
        if pcm is None or len(pcm) < 16000:
            return None
        with self._lock:
            if not self._load():
                return None
            wave = np.asarray(pcm, dtype=np.float32) / 32768.0
            scores = self.sess.run(None, {"waveform": wave})[0]
            self.calls += 1
        out = []
        for f in range(scores.shape[0]):
            t = f * 0.48
            seg = wave[int(t * 16000): int((t + 0.96) * 16000)]
            rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
            out.append({"t": round(t, 2), "voice": float(scores[f, self.idx["voice"]].max()),
                        "reaction": float(scores[f, self.idx["reaction"]].max()),
                        "game": float(scores[f, self.idx["game"]].max()),
                        "rms_db": round(20 * float(np.log10(rms + 1e-9)), 1)})
        return out

    def info(self) -> dict:
        return {"status": self.status, "detail": self.detail, "calls": self.calls}


def apply_sound(audio: float, audio_peak_t: float, snd: dict | None, now_media: float,
                game_threshold: float, game_discount: float, max_age: float = 3.5) -> tuple[float, float, float]:
    """Combine loudness excitement with the latest YAMNet window for this stream.
    Returns (audio_signal, its_peak_time, reaction_score)."""
    if not snd or now_media - snd["t_end"] > max_age:
        return audio, audio_peak_t, 0.0
    react = reaction_score(snd["reaction"])
    voice = snd.get("voice", 1.0)
    if snd["reaction"] < 0.15 and (snd["game"] >= game_threshold or voice < 0.35):
        # loud, but it's the game (gunfire/explosions/footsteps) or nobody is voicing at all:
        # loudness only means hype when a human is the one being loud
        audio *= game_discount
    if react > audio:
        return react, snd["peak_t"], react     # laughter / scream / cheer drives the signal
    return audio, audio_peak_t, react
