"""Vertical 9:16 renderer (1080x1920 H.264/AAC) built on a single FFmpeg
filter graph.

Layouts
  smart_crop  stable motion/detail-following 9:16 crop, optional punch-in at the peak
  facecam     facecam rect on top, gameplay below (vstack), no AI upscaling
  letterbox   full original frame over a blurred, darkened fill of itself

Timeline
  pieces = ordered [(src_start, src_end)] built from the trim; with
  `cold_open` the first ~1.6 s of the peak is flashed up front as the hook,
  then the clip plays from its setup (a common retention technique). Caption
  word times are remapped through the same pieces, so they stay in sync.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..media import MediaError, nvenc_available, popen_flags, videotoolbox_available
from ..config import DEFAULT_CAPTION_FONT
from .captions import build_ass, build_srt, hyperframes_transcript, map_words
from .framing import crop_path, sample_frames, x_expression

log = logging.getLogger("liveclip.render")


@dataclass
class RenderSpec:
    src: str
    src_w: int
    src_h: int
    src_fps: float
    duration: float
    out: str
    layout: str = "smart_crop"
    trim_start: float = 0.0
    trim_end: float | None = None
    peak: float | None = None              # seconds into the source clip
    words: list[dict] = field(default_factory=list)
    facecam: dict | None = None            # normalized rect
    gameplay: dict | None = None           # optional pinned gameplay rect (facecam layout)
    segments: list | None = None           # ordered source pieces [(a, b)] (best-moment cut)
    title: str | None = None
    caption_style: str = "bold_pop"
    caption_size: int = 80
    caption_y: float = 0.66
    caption_uppercase: bool = True
    captions: bool = True
    hook: bool = True
    hook_seconds: float = 2.6
    font: str = DEFAULT_CAPTION_FONT
    fonts_dir: str = ""
    cold_open: bool = False
    punch_zoom: bool = True
    fps: int = 60
    preview: bool = False
    encoder: str = "auto"
    nvenc_cq: int = 19
    x264_crf: int = 18
    max_bitrate_mbps: float = 20.0
    audio_bitrate_k: int = 192
    normalize_audio: bool = False


def build_pieces(spec: RenderSpec) -> list[tuple[float, float]]:
    if spec.segments:
        pieces = [(max(0.0, float(a)), min(spec.duration, float(b))) for a, b in spec.segments]
        pieces = [(a, b) for a, b in pieces if b - a > 0.3]
        if not pieces:
            raise MediaError("No usable segments")
        return pieces
    a = max(0.0, spec.trim_start)
    b = min(spec.duration, spec.trim_end if spec.trim_end else spec.duration)
    if b - a < 1.0:
        raise MediaError("Trim range is shorter than 1 second")
    pieces = [(a, b)]
    if spec.cold_open and spec.peak is not None and a + 6 < spec.peak < b - 1.0:
        hs = max(a, spec.peak - 0.4)
        pieces = [(round(hs, 3), round(min(b, hs + 1.6), 3)), (a, b)]
    return pieces


def pick_encoder(choice: str, ffmpeg: str) -> str:
    """'nvenc' (NVIDIA, Windows), 'videotoolbox' (Apple, macOS) or 'x264' (CPU fallback)."""
    if choice in ("nvenc", "videotoolbox", "x264"):
        return choice
    if nvenc_available(ffmpeg):
        return "nvenc"
    if videotoolbox_available(ffmpeg):
        return "videotoolbox"
    return "x264"


def _encoder_args(spec: RenderSpec, ffmpeg: str) -> list[str]:
    enc = pick_encoder(spec.encoder, ffmpeg)
    use_nvenc = enc == "nvenc"
    mr = f"{spec.max_bitrate_mbps:g}M"
    buf = f"{spec.max_bitrate_mbps * 2:g}M"
    if spec.preview:
        if use_nvenc:
            return ["-c:v", "h264_nvenc", "-preset", "p2", "-cq", "28", "-b:v", "0", "-pix_fmt", "yuv420p"]
        if enc == "videotoolbox":
            return ["-c:v", "h264_videotoolbox", "-b:v", "3M", "-allow_sw", "1", "-pix_fmt", "yuv420p"]
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p"]
    if enc == "videotoolbox":
        # Apple hardware encoder: bitrate-driven (constant-quality mode isn't available on every Mac);
        # 12 Mbps average for 1080x1920 matches what NVENC CQ 19 produced on real reels (~7-12 Mbps)
        return ["-c:v", "h264_videotoolbox", "-b:v", "12M", "-maxrate", mr, "-bufsize", buf, "-profile:v", "high",
                "-allow_sw", "1", "-pix_fmt", "yuv420p"]
    if use_nvenc:
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-tune", "hq", "-rc", "vbr", "-cq", str(spec.nvenc_cq),
                "-b:v", "0", "-maxrate", mr, "-bufsize", buf, "-profile:v", "high", "-spatial-aq", "1",
                "-temporal-aq", "1", "-rc-lookahead", "32", "-bf", "3", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "slow", "-crf", str(spec.x264_crf), "-maxrate", mr, "-bufsize", buf,
            "-profile:v", "high", "-pix_fmt", "yuv420p"]


def build_filtergraph(spec: RenderSpec, pieces: list[tuple[float, float]], crop_keys: list[tuple[float, float]] | None,
                      ass_name: str | None) -> tuple[str, str, str]:
    W, H = (540, 960) if spec.preview else (1080, 1920)
    parts = []
    n = len(pieces)
    for i, (a, b) in enumerate(pieces):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
    if n > 1:
        if spec.cold_open and not spec.segments:
            # 2-frame white flash on the cut back from the cold open
            parts.append(f"[v0]fade=t=out:st={pieces[0][1] - pieces[0][0] - 0.07:.3f}:d=0.07:color=white[v0f]")
            first = "[v0f]"
        else:
            first = "[v0]"                                  # plain jump cuts
        ins = f"{first}[a0]" + "".join(f"[v{i}][a{i}]" for i in range(1, n))
        parts.append(f"{ins}concat=n={n}:v=1:a=1[vc][ac]")
    else:
        parts.append("[v0]null[vc]")
        parts.append("[a0]anull[ac]")

    sw, sh = spec.src_w, spec.src_h
    out_fps = min(spec.fps, round(spec.src_fps or spec.fps))  # never fabricate frames
    if spec.preview:
        out_fps = min(out_fps, 30)
    layout = spec.layout
    if layout == "facecam" and not spec.facecam:
        layout = "smart_crop"
    if layout == "smart_crop":
        cw = min(sw, int(round(sh * 9 / 16 / 2)) * 2)
        x = x_expression(crop_keys or [(0.0, (sw - cw) / 2)])
        chain = f"[vc]crop=w={cw}:h={sh}:x='{x}':y=0,scale={W}:{H}:flags=lanczos"
        if spec.punch_zoom and spec.peak is not None:
            # peak time on the output timeline (inside the main piece)
            a, _ = pieces[-1]
            pk = (pieces[0][1] - pieces[0][0] if n > 1 else 0) + (spec.peak - a)
            z = (f"(1+0.07*if(between(t,{pk - 0.15:.2f},{pk + 2.4:.2f}),"
                 f"if(lt(t,{pk + 0.1:.2f}),(t-{pk - 0.15:.2f})/0.25,max(0,1-(t-{pk + 0.1:.2f})/2.3)),0))")
            chain += f",scale=w='trunc({W}*{z}/2)*2':h='trunc({H}*{z}/2)*2':eval=frame:flags=bicubic,crop={W}:{H}"
        parts.append(chain + "[vl]")
    elif layout == "facecam":
        fc = spec.facecam
        fx, fy, fw, fh = (int(fc["x"] * sw), int(fc["y"] * sh), int(fc["w"] * sw), int(fc["h"] * sh))
        top_h = int(H * 0.40) // 2 * 2
        bot_h = H - top_h
        # fit the facecam rect to the top panel aspect by expanding/cropping around its centre
        target = W / top_h
        if fw / max(fh, 1) > target:
            nw = int(fh * target)
            fx += (fw - nw) // 2
            fw = nw
        else:
            nh = int(fw / target)
            fy += (fh - nh) // 2
            fh = nh
        fx, fy = max(0, min(sw - fw, fx)), max(0, min(sh - fh, fy))
        parts.append("[vc]split=2[fcin][gpin]")
        parts.append(f"[fcin]crop={fw // 2 * 2}:{fh // 2 * 2}:{fx}:{fy},scale={W}:{top_h}:flags=lanczos[fct]")
        if spec.gameplay:
            # pinned gameplay region (e.g. a chess board): fit to the bottom panel's aspect
            # around the rect's centre, static — no chasing the streamer's moving head
            g = spec.gameplay
            cx, cy = (g["x"] + g["w"] / 2) * sw, (g["y"] + g["h"] / 2) * sh
            gw, gh = g["w"] * sw, g["h"] * sh
            aspect = W / bot_h
            if gw / gh > aspect:
                gh = gw / aspect
            else:
                gw = gh * aspect
            gw, gh = min(sw, int(gw) // 2 * 2), min(sh, int(gh) // 2 * 2)
            gx = int(max(0, min(sw - gw, cx - gw / 2)))
            gy = int(max(0, min(sh - gh, cy - gh / 2)))
            parts.append(f"[gpin]crop={gw}:{gh}:{gx}:{gy},scale={W}:{bot_h}:flags=lanczos[gpt]")
        else:
            gw = min(sw, int(sh * W / bot_h) // 2 * 2)
            gx = max(0, min(sw - gw, (sw - gw) // 2))
            if crop_keys:
                gx_expr = x_expression([(t, max(0, min(sw - gw, x + (int(round(sh * 9 / 16)) - gw) / 2))) for t, x in crop_keys])
            else:
                gx_expr = str(gx)
            parts.append(f"[gpin]crop=w={gw}:h={sh}:x='{gx_expr}':y=0,scale={W}:{bot_h}:flags=lanczos[gpt]")
        parts.append("[fct][gpt]vstack=inputs=2,setsar=1[vl]")
    else:  # letterbox
        fg_y = int(H * 0.30)
        parts.append("[vc]split=2[bgin][fgin]")
        parts.append(f"[bgin]scale={W // 4}:{H // 4}:force_original_aspect_ratio=increase,crop={W // 4}:{H // 4},"
                     f"boxblur=12:2,eq=brightness=-0.22:saturation=1.15,scale={W}:{H}[bg]")
        parts.append(f"[fgin]scale={W}:-2:flags=lanczos[fg]")
        parts.append(f"[bg][fg]overlay=x=0:y={fg_y}-h/2+{int(H * 0.06)},setsar=1[vl]")

    tail = f"[vl]fps={out_fps},format=yuv420p"
    if ass_name:
        tail += f",subtitles=filename={ass_name}"
        if spec.fonts_dir:
            tail += ":fontsdir=fonts"
    parts.append(tail + "[vout]")
    if spec.normalize_audio:
        parts.append("[ac]loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000[aout]")
    else:
        parts.append("[ac]aresample=48000[aout]")
    return ";".join(parts), "[vout]", "[aout]"


def render(spec: RenderSpec, ffmpeg: str, temp_root: Path, progress=None) -> dict:
    pieces = build_pieces(spec)
    out_dur = sum(b - a for a, b in pieces)
    work = Path(tempfile.mkdtemp(prefix="render_", dir=temp_root))
    try:
        crop_keys = None
        if spec.layout == "smart_crop" or (spec.layout == "facecam" and not spec.gameplay):
            # analyse each piece and lay keyframes onto the output timeline
            keys: list[tuple[float, float]] = []
            off = 0.0
            cw = min(spec.src_w, int(round(spec.src_h * 9 / 16 / 2)) * 2)
            for a, b in pieces:
                frames = sample_frames(ffmpeg, spec.src, a, b)
                for t, x in crop_path(frames, spec.src_w, spec.src_h, cw):
                    keys.append((round(off + t, 2), x))
                off += b - a
            crop_keys = keys or None
        words_out = map_words(spec.words, pieces) if spec.words else []
        ass_name = None
        if spec.captions or (spec.hook and spec.title):
            W, H = (540, 960) if spec.preview else (1080, 1920)
            ass = build_ass(words_out if spec.captions else [], style=spec.caption_style, font=spec.font,
                            size=spec.caption_size, y_frac=spec.caption_y, width=W, height=H,
                            hook=spec.title if spec.hook else None, hook_seconds=spec.hook_seconds,
                            duration=out_dur, uppercase=spec.caption_uppercase)
            (work / "caps.ass").write_text(ass, encoding="utf-8")
            ass_name = "caps.ass"
            if spec.fonts_dir and Path(spec.fonts_dir).is_dir():
                shutil.copytree(spec.fonts_dir, work / "fonts", dirs_exist_ok=True)
        fg, vout, aout = build_filtergraph(spec, pieces, crop_keys, ass_name)
        (work / "graph.txt").write_text(fg, encoding="utf-8")
        out = Path(spec.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp_out = work / ("out" + out.suffix)
        cmd = [ffmpeg, "-y", "-hide_banner", "-v", "error", "-progress", "pipe:1", "-nostats",
               "-i", spec.src, "-/filter_complex", "graph.txt", "-map", vout, "-map", aout,
               *_encoder_args(spec, ffmpeg), "-c:a", "aac", "-b:a", f"{spec.audio_bitrate_k}k", "-ar", "48000",
               "-movflags", "+faststart", "-t", f"{out_dur:.3f}", str(tmp_out)]
        proc = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **popen_flags("low"))
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("out_time_us=") and progress:
                try:
                    progress(min(1.0, int(line.split("=", 1)[1]) / 1e6 / max(out_dur, 0.1)))
                except ValueError:
                    pass
        err = proc.stderr.read().decode("utf-8", "replace")
        proc.wait()
        if proc.returncode != 0:
            raise MediaError(f"render failed ({proc.returncode}): {err[-1500:]}")
        shutil.move(str(tmp_out), out)
        side = {}
        if not spec.preview and words_out:
            srt = out.with_suffix(".srt")
            srt.write_text(build_srt(words_out), encoding="utf-8")
            hf = out.with_suffix(".transcript.json")
            hf.write_text(hyperframes_transcript(words_out), encoding="utf-8")
            side = {"srt": str(srt), "transcript": str(hf)}
        return {"path": str(out), "duration": out_dur, "pieces": pieces, "crop_keys": crop_keys, "words": words_out, **side}
    finally:
        shutil.rmtree(work, ignore_errors=True)
