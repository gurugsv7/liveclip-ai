"""Word-timed caption generation (ASS for burn-in, SRT + HyperFrames-style
transcript.json sidecars).

Follows the house short-form rules (caption-animation / short-form-video skills):
  * real word-level timestamps only — never an even split of a line
  * 1-3 word "pages", active word highlighted in a single accent colour
  * bold sans, heavy outline + shadow, centre band (~66% down), inside the
    1080x1920 safe box (90px side margins, clear of the bottom ~320px UI zone)
  * filler words (um/uh) dropped; nothing is added that wasn't said
  * hook line on screen from frame 1 (title), top safe area
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

FILLERS = {"um", "uh", "uhm", "umm", "erm", "er", "hmm", "mm", "mhm"}

# ASS colours are &HAABBGGRR
WHITE = "&H00FFFFFF"
BLACK = "&H00000000"
YELLOW = "&H005EE4FF"      # #FFE45E
PURPLE = "&H00F65C8B"      # #8B5CF6
BLUE = "&H00FFB000"        # #00B0FF
MAGENTA = "&H00D946EF"     # #EF46D9


@dataclass
class StyleSpec:
    uppercase: bool
    max_words: int
    max_chars: int
    active: str | None       # override tags for the active word
    base_extra: str          # tags for the whole line
    size_mult: float
    outline: int
    shadow: int
    outline_colour: str = BLACK


STYLES = {
    "bold_pop": StyleSpec(True, 3, 16, f"{{\\c{YELLOW}}}", "", 1.0, 6, 3),
    "clean": StyleSpec(False, 6, 30, None, "", 0.78, 4, 2),
    "box": StyleSpec(True, 3, 16, f"{{\\3c{PURPLE}\\bord16\\shad0}}", "", 1.0, 6, 3),
    "neon": StyleSpec(True, 3, 16, f"{{\\c{MAGENTA}}}", f"{{\\3c{BLUE}\\blur2}}", 1.0, 5, 0, BLUE),
}


def clean_words(words: list[dict]) -> list[dict]:
    out = []
    for w in words:
        t = (w.get("text") or "").strip()
        bare = re.sub(r"[^\w']", "", t.lower())
        if not t or bare in FILLERS:
            continue
        if w["end"] <= w["start"]:
            w = {**w, "end": w["start"] + 0.08}
        out.append({**w, "text": t})
    return out


def paginate(words: list[dict], spec: StyleSpec, gap: float = 0.45) -> list[list[dict]]:
    pages: list[list[dict]] = []
    cur: list[dict] = []
    for w in words:
        if cur:
            chars = sum(len(x["text"]) + 1 for x in cur) + len(w["text"])
            prev = cur[-1]
            if (len(cur) >= spec.max_words or chars > spec.max_chars or w["start"] - prev["end"] > gap
                    or re.search(r"[.!?,;:]$", prev["text"])):
                pages.append(cur)
                cur = []
        cur.append(w)
    if cur:
        pages.append(cur)
    return pages


def _esc(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")").replace("\n", " ")


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(words: list[dict], *, style: str = "bold_pop", font: str = "", size: int = 80,
              y_frac: float = 0.66, width: int = 1080, height: int = 1920, hook: str | None = None,
              hook_seconds: float = 2.6, duration: float | None = None, uppercase: bool | None = None) -> str:
    if not font:
        from ..config import DEFAULT_CAPTION_FONT
        font = DEFAULT_CAPTION_FONT
    spec = STYLES.get(style, STYLES["bold_pop"])
    upper = spec.uppercase if uppercase is None else uppercase
    k = width / 1080
    fs = int(size * spec.size_mult * k)
    y = int(height * y_frac)
    side = int(90 * k)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 0", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Cap,{font},{fs},{WHITE},{WHITE},{spec.outline_colour},&H96000000,-1,0,0,0,100,100,1,0,1,"
        f"{int(spec.outline * k)},{int(spec.shadow * k)},5,{side},{side},0,1",
        f"Style: Hook,{font},{int(62 * k)},{WHITE},{WHITE},&H00F65C8B,&H00F65C8B,-1,0,0,0,100,100,0,0,3,"
        f"{int(18 * k)},0,8,{side},{side},{int(250 * k)},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    if hook:
        h = _esc(hook.strip())
        if len(h) > 60:
            h = h[:57].rsplit(" ", 1)[0] + "…"
        end = hook_seconds if duration is None else min(hook_seconds, duration)
        # visible from frame 1 (no fade-in), quick fade out
        lines.append(f"Dialogue: 2,{_ts(0)},{_ts(end)},Hook,,0,0,0,,{{\\fad(0,180)}}{h}")
    pages = paginate(clean_words(words), spec)
    pos = f"{{\\an5\\pos({width // 2},{y})}}"
    for pi, page in enumerate(pages):
        nxt = pages[pi + 1][0]["start"] if pi + 1 < len(pages) else None
        page_end = page[-1]["end"] + 0.25
        if nxt is not None:
            page_end = min(page_end, nxt)
        if duration is not None:
            page_end = min(page_end, duration)
        texts = [(_esc(w["text"].upper() if upper else w["text"])) for w in page]
        if spec.active is None:
            st = page[0]["start"]
            lines.append(f"Dialogue: 1,{_ts(st)},{_ts(page_end)},Cap,,0,0,0,,{pos}{spec.base_extra}"
                         f"{{\\fscx92\\fscy92\\t(0,80,\\fscx100\\fscy100)}}{' '.join(texts)}")
            continue
        for wi, w in enumerate(page):
            st = page[0]["start"] if wi == 0 else w["start"]
            en = page[wi + 1]["start"] if wi + 1 < len(page) else page_end
            if en <= st:
                continue
            parts = []
            for j, t in enumerate(texts):
                if j == wi:
                    parts.append(f"{spec.active}{t}{{\\r}}{spec.base_extra}")
                else:
                    parts.append(t)
            pop = "{\\fscx80\\fscy80\\t(0,90,\\fscx100\\fscy100)}" if wi == 0 else ""
            lines.append(f"Dialogue: 1,{_ts(st)},{_ts(en)},Cap,,0,0,0,,{pos}{spec.base_extra}{pop}{' '.join(parts)}")
    return "\n".join(lines) + "\n"


def build_srt(words: list[dict], max_words: int = 7) -> str:
    spec = StyleSpec(False, max_words, 42, None, "", 1, 0, 0)
    out = []
    for i, page in enumerate(paginate(clean_words(words), spec), 1):
        def f(t: float) -> str:
            ms = int(round(max(0.0, t) * 1000))
            return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
        out.append(f"{i}\n{f(page[0]['start'])} --> {f(page[-1]['end'])}\n{' '.join(w['text'] for w in page)}\n")
    return "\n".join(out)


def hyperframes_transcript(words: list[dict]) -> str:
    """Flat word array [{text,start,end}] — the format the HyperFrames
    embedded-captions / talking-head workflows consume."""
    return json.dumps([{"text": w["text"], "start": w["start"], "end": w["end"]} for w in clean_words(words)], indent=1)


def map_words(words: list[dict], pieces: list[tuple[float, float]]) -> list[dict]:
    """Map source-relative word times onto the output timeline built from
    `pieces` (ordered list of (src_start, src_end) segments)."""
    out = []
    offset = 0.0
    for a, b in pieces:
        for w in words:
            if w["start"] >= a - 0.05 and w["end"] <= b + 0.05:
                out.append({**w, "start": round(max(0.0, w["start"] - a) + offset, 3),
                            "end": round(min(b, w["end"]) - a + offset, 3)})
        offset += b - a
    return out
