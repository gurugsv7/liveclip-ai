"""Hand reel editing to Gemini (or any multimodal model) instead of Claude.

  list                      clips worth editing (id, score, length, title, file)
  prompt <clip_id>          writes data/gemini/<clip_id>.prompt.txt  (rules + real Whisper transcript)
  apply  <clip_id> <json>   validates Gemini's edit plan, snaps cuts to word gaps, enforces <= 30 s,
                            saves it on the clip and exports the 1080x1920 reel (app must be running)
         --dry-run          validate and show what would be rendered, don't export

Workflow: run `prompt`, upload the clip MP4 + paste the prompt into Gemini, save Gemini's JSON
reply to a file, run `apply`. Usage: backend\\.venv\\Scripts\\python scripts\\gemini_edit.py ...
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "database" / "liveclip.sqlite3"
API = "http://127.0.0.1:8765"
MAX_REEL = 30.0
LAYOUTS = {"smart_crop", "facecam", "letterbox"}
STYLES = {"bold_pop", "box", "neon", "clean"}


def db():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def clip_row(cid: str) -> dict:
    r = db().execute("SELECT c.*, s.name AS streamer FROM clips c JOIN streamers s ON s.id=c.streamer_id WHERE c.id=?", (cid,)).fetchone()
    if not r:
        sys.exit(f"clip {cid} not found (run: list)")
    return dict(r)


def words_of(cid: str) -> list[dict]:
    r = db().execute("SELECT words FROM transcriptions WHERE clip_id=?", (cid,)).fetchone()
    return json.loads(r["words"]) if r else []


# ------------------------------------------------------------------ list
def cmd_list() -> None:
    rows = db().execute("""SELECT c.id, c.status, c.ai_score, c.duration, c.title, c.file_path, s.name FROM clips c
        JOIN streamers s ON s.id=c.streamer_id WHERE c.file_path IS NOT NULL
        AND c.status IN ('ready_for_review','maybe','approved','exported') ORDER BY COALESCE(c.ai_score,0) DESC""").fetchall()
    for r in rows:
        print(f"{r['id']}  {r['status']:16s} score {r['ai_score'] or 0:.2f}  {r['duration'] or 0:5.1f}s  {r['name']}: {(r['title'] or '')[:60]}")
        print(f"    {r['file_path']}")


# ------------------------------------------------------------------ prompt
PROMPT = """You are a top short-form video editor (YouTube Shorts / Instagram Reels / TikTok). Watch the attached
livestream clip ({duration:.1f} s, {width}x{height}, streamer: {streamer}) and plan the single best vertical reel from it.

HARD RULES (never break):
1. The reel is 30 seconds MAX in total (aim 15-30 s). Only the best moment - people swipe away from long clips.
2. Use only real footage from this clip. You may jump-cut: drop dead air, boring setup and filler between segments.
   Segments may be re-ordered ONLY for a cold open (flash the peak first, then the setup) - keep it understandable.
3. Start strong: the first 2 seconds must grab attention (a reaction, a bold line, or the hook text over action).
4. End right after the payoff (the laugh, the "let's go", the reveal). Never trail off into filler.
5. Do not invent dialogue. Hook text must be the streamer's own words or a short factual tease (max 8 words, no clickbait lies).
6. Cuts must not split words. Use the transcript timestamps below; they are accurate to ~0.1 s.

LOOK AT (use your eyes, not just the transcript):
- The facecam: where is it (normalized x,y,w,h of the camera box, 0..1 of the full frame)? Big face reactions matter most.
- The gameplay: what region shows the action (e.g. a chess board, the player's character, the map)? Give it as a rect too.
- Visual moments the transcript misses (a crash, a kill, a glitch, a jump scare, a big win screen).
- Whether captions would cover something important; choose caption_y accordingly.

LAYOUTS (pick one):
- "facecam": facecam on top (40% of the 9:16 frame), gameplay rect below (60%). Best when there is a facecam and the face reacts.
  Captions usually look best on the seam: caption_y about 0.40-0.43.
- "smart_crop": a single 9:16 crop that follows the action (no facecam or the face is the whole frame). caption_y about 0.62-0.70.
- "letterbox": full original frame over a blurred fill (when cropping would lose essential info). caption_y about 0.70.
Caption styles: "bold_pop" (white, yellow active word - default), "box" (violet highlight box), "neon" (blue glow), "clean" (sentence case).

BE CREATIVE within the rules: pick the hook, the cut rhythm, a cold open if it helps, the style that fits the energy.

TRANSCRIPT (word timestamps in seconds from clip start):
{transcript}

Reply with ONLY this JSON (no prose, no markdown fences):
{{
  "segments": [[start_s, end_s], ...],          // in playback order, total <= 30 s
  "hook": "on-screen hook text, <= 8 words",
  "layout": "facecam" | "smart_crop" | "letterbox",
  "facecam": {{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}} or null,
  "gameplay": {{"x": 0.0, "y": 0.0, "w": 0.0, "h": 0.0}} or null,
  "caption_style": "bold_pop" | "box" | "neon" | "clean",
  "caption_y": 0.41,
  "uppercase": true,
  "peak_s": 0.0,                                 // the single best instant (used for a subtle zoom in smart_crop)
  "why": "one sentence on why this is the best reel"
}}
"""


def transcript_lines(words: list[dict]) -> str:
    out, cur, t0 = [], [], None
    for w in words:
        if t0 is None:
            t0 = w["start"]
        cur.append(f"{w['text']}")
        if len(cur) >= 10 or re.search(r"[.!?]$", w["text"]):
            out.append(f"[{t0:6.2f}-{w['end']:6.2f}] {' '.join(cur)}")
            cur, t0 = [], None
    if cur:
        out.append(f"[{t0:6.2f}-{words[-1]['end']:6.2f}] {' '.join(cur)}")
    return "\n".join(out) or "(no clear speech detected)"


def cmd_prompt(cid: str) -> None:
    c = clip_row(cid)
    text = PROMPT.format(duration=c["duration"] or 0, width=c["width"], height=c["height"], streamer=c["streamer"],
                         transcript=transcript_lines(words_of(cid)))
    out = ROOT / "data" / "gemini" / f"{cid}.prompt.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"prompt : {out}\nvideo  : {c['file_path']}\nNext   : upload the video to Gemini, paste the prompt, save the JSON reply as "
          f"data\\gemini\\{cid}.json, then run: apply {cid} data\\gemini\\{cid}.json")


# ------------------------------------------------------------------ apply
def _parse(text: str) -> dict:
    text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M).strip()
    text = re.sub(r"//[^\n]*", "", text)                     # tolerate the comments from the template
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        sys.exit("no JSON object found in Gemini's reply")
    return json.loads(m.group(0))


def _snap(t: float, words: list[dict], edge: str) -> float:
    """Move a cut out of the middle of a word (Gemini sees ~1 fps)."""
    for w in words:
        if w["start"] < t < w["end"]:
            return round(w["start"] - 0.08, 2) if edge == "start" else round(w["end"] + 0.12, 2)
    return round(t, 2)


def _rect(r) -> dict | None:
    if not isinstance(r, dict):
        return None
    try:
        x, y, w, h = (float(r[k]) for k in ("x", "y", "w", "h"))
    except (KeyError, TypeError, ValueError):
        return None
    if w > 1.5 or h > 1.5:          # pixels given instead of fractions
        return None
    x, y = max(0.0, min(0.98, x)), max(0.0, min(0.98, y))
    w, h = max(0.02, min(1 - x, w)), max(0.02, min(1 - y, h))
    return {"x": round(x, 4), "y": round(y, 4), "w": round(w, 4), "h": round(h, 4)}


def validate(plan: dict, c: dict, words: list[dict]) -> tuple[dict, list[str]]:
    notes, dur = [], float(c["duration"] or 0)
    segs = []
    for s in plan.get("segments") or []:
        a, b = max(0.0, float(s[0])), min(dur, float(s[1]))
        a, b = _snap(a, words, "start"), _snap(b, words, "end")
        a, b = max(0.0, a), min(dur, b)
        if b - a >= 0.5:
            segs.append([a, b])
    if not segs:
        sys.exit("plan has no usable segments")
    total = sum(b - a for a, b in segs)
    while total > MAX_REEL + 1e-6:                            # enforce the 30 s rule
        a, b = segs[-1]
        cut = total - MAX_REEL
        if b - a - cut >= 0.5:
            nb = b - cut
            for w in words:                                    # don't end mid-word: cut before it
                if w["start"] < nb < w["end"]:
                    nb = w["start"] - 0.05
            segs[-1] = [a, round(max(a + 0.5, nb), 2)]
        else:
            segs.pop()
        notes.append(f"trimmed to the {MAX_REEL:.0f} s limit")
        total = sum(b - a for a, b in segs)
    layout = plan.get("layout") if plan.get("layout") in LAYOUTS else "smart_crop"
    facecam, gameplay = _rect(plan.get("facecam")), _rect(plan.get("gameplay"))
    if layout == "facecam" and not facecam:
        notes.append("facecam layout without a valid facecam rect -> smart_crop")
        layout = "smart_crop"
    style = plan.get("caption_style") if plan.get("caption_style") in STYLES else "bold_pop"
    cap_y = float(plan.get("caption_y") or (0.41 if layout == "facecam" else 0.66))
    cap_y = max(0.30, min(0.80, cap_y))
    hook = " ".join(str(plan.get("hook") or "").split()[:8])[:60] or None
    peak = plan.get("peak_s")
    lp = {"segments": segs, "facecam": facecam, "gameplay": gameplay if layout == "facecam" else None,
          "peak": float(peak) if isinstance(peak, (int, float)) else None, "cold_open": False,
          "punch_zoom": layout == "smart_crop"}
    patch = {"title": hook, "layout": layout, "layout_params": lp,
             "caption_style": {"style": style, "y": round(cap_y, 3), "uppercase": bool(plan.get("uppercase", True)),
                               "hook": bool(hook), "enabled": True}}
    return patch, notes + [f"total {total:.1f} s in {len(segs)} segment(s)"]


def _api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:8765"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def cmd_apply(cid: str, path: str, dry: bool) -> None:
    c, words = clip_row(cid), words_of(cid)
    plan = _parse(Path(path).read_text(encoding="utf-8"))
    patch, notes = validate(plan, c, words)
    for a, b in patch["layout_params"]["segments"]:
        said = " ".join(w["text"] for w in words if w["start"] >= a - 0.05 and w["end"] <= b + 0.05)
        print(f"  [{a:6.2f}-{b:6.2f}] {said}")
    print("  layout:", patch["layout"], "| hook:", patch["title"], "| captions:", patch["caption_style"], "\n  notes:", "; ".join(notes))
    if plan.get("why"):
        print("  gemini says:", plan["why"])
    if dry:
        return
    _api("PATCH", f"/api/clips/{cid}", patch)
    _api("POST", f"/api/clips/{cid}/export")
    print("  exporting...", end="", flush=True)
    for _ in range(240):
        time.sleep(1)
        c2 = _api("GET", f"/api/clips/{cid}")
        if c2["export_status"] in ("done", "failed"):
            break
        print(".", end="", flush=True)
    print()
    if c2["export_status"] != "done":
        sys.exit(f"export failed: {c2.get('error')}")
    print("  reel:", c2["export_path"])


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a or a[0] not in ("list", "prompt", "apply"):
        sys.exit(__doc__)
    if a[0] == "list":
        cmd_list()
    elif a[0] == "prompt" and len(a) >= 2:
        cmd_prompt(a[1])
    elif a[0] == "apply" and len(a) >= 3:
        cmd_apply(a[1], a[2], "--dry-run" in a)
    else:
        sys.exit(__doc__)
