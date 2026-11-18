"""Pick the best <= N-second reel out of a longer clip (house rule: reels are
30 s max — people swipe away from minute-long clips).

Cuts like an editor, from two timelines of the clip:
  * Whisper words  -> phrases (what was said)
  * YAMNet frames  -> voiced bursts Whisper couldn't transcribe (raging, yelling,
                      laughing is often garbled) + vocal reactions + loudness
Steps:
  1. every phrase / voiced burst gets a value: excited phrases, repetition,
     screams/laughs, loud voiced frames, untranscribed outbursts
  2. for every stretch up to ~1.7x the budget, drop its weakest pieces until it fits
     <= max_s (jump cuts, original order kept) and score what remains by value density,
     with a bonus for containing the hype peak
  3. if budget remains, splice on a short setup line from earlier in the clip
  4. end right after the payoff — never trail into filler
Never invents anything: every piece is real, contiguous source footage.
"""
from __future__ import annotations

import math
import re

from ..ai.scoring import live_speech_raw

GAP_SPLIT = 0.45      # pause that ends a phrase
DEAD_AIR = 1.0        # silence longer than this inside the reel is cut out
LEAD_IN = 0.15
TAIL = 0.6            # let the reaction land after the last word
PAD = 0.25            # padding per kept piece when budgeting
MERGE_GAP = 7.0       # inside the chosen moment keep short stretches; only cut long dull blocks
VOICED = 0.3          # YAMNet voice prob that means "someone is voicing" (not dead air)


def phrases(words: list[dict]) -> list[dict]:
    out, cur = [], []
    for i, w in enumerate(words):
        cur.append(w)
        nxt = words[i + 1]["start"] if i + 1 < len(words) else None
        if nxt is None or nxt - w["end"] > GAP_SPLIT or re.search(r"[.!?]$", w["text"]):
            out.append({"start": cur[0]["start"], "end": cur[-1]["end"], "words": cur})
            cur = []
    return out


def _strength(ws: list[dict]) -> float:
    if not ws:
        return 0.0
    raw, _, hits = live_speech_raw(ws)
    return raw + 0.15 * len(hits)


def _voiced(t0: float, t1: float, tl: list[dict] | None) -> bool:
    """YAMNet heard a human voicing between t0 and t1 (garbled speech / reaction, not dead air)."""
    if not tl:
        return False
    fr = [f for f in tl if t0 - 0.2 <= f["t"] + 0.48 and f["t"] <= t1 + 0.2]
    return bool(fr) and sum(f["voice"] >= VOICED for f in fr) >= max(1, 0.4 * len(fr))


def _voiced_median(tl: list[dict] | None) -> float:
    v = sorted(f["rms_db"] for f in (tl or []) if f["voice"] >= 0.5)
    return v[len(v) // 2] if v else -40.0


def _outburst(t0: float, t1: float, tl: list[dict] | None, words: list[dict], med: float) -> float:
    """Non-verbal reaction strength in [t0, t1]: vocal reactions (scream/laugh/groan), loud voiced
    frames, and voiced stretches Whisper couldn't transcribe (raging is garbled)."""
    if not tl:
        return 0.0
    fr = [f for f in tl if t0 - 0.24 <= f["t"] <= t1]
    s = sum(min(1.0, max(0.0, (f["reaction"] - 0.15) / 0.5)) for f in fr) * 0.25
    s += sum(1 for f in fr if f["voice"] >= 0.5 and f["rms_db"] > med + 4) * 0.08

    def has_word(f):
        return any(w["start"] - 0.3 <= f["t"] + 0.48 and w["end"] + 0.3 >= f["t"] for w in words)
    s += sum(1 for f in fr if f["voice"] >= VOICED and not has_word(f)) * 0.05
    return s


def _end_after(word: dict, words: list[dict], pad: float) -> float:
    """Cut point after `word`: padded, but never into the next word's first syllable."""
    nxt = [w["start"] for w in words if w["start"] >= word["end"] - 1e-6 and w is not word]
    end = word["end"] + pad
    if nxt:
        end = min(end, max(word["end"] + 0.05, min(nxt) - 0.03))
    return round(end, 3)


def _add_voiced_gaps(ph: list[dict], tl: list[dict], lo: float, hi: float) -> list[dict]:
    """Voiced stretches with no words become pseudo-phrases so they can be kept or dropped."""
    edges = [lo] + [x for p in ph for x in (p["start"], p["end"])] + [hi]
    extra = []
    for g0, g1 in zip(edges[0::2], edges[1::2]):
        if g1 - g0 < 1.5:
            continue
        run = None
        for f in tl:
            if g0 <= f["t"] and f["t"] + 0.48 <= g1:
                if f["voice"] >= VOICED:
                    run = [run[0] if run else f["t"], f["t"] + 0.48]
                elif run:
                    if run[1] - run[0] >= 1.0:
                        extra.append({"start": run[0], "end": run[1], "words": [], "voiced": True})
                    run = None
        if run and run[1] - run[0] >= 1.0:
            extra.append({"start": run[0], "end": run[1], "words": [], "voiced": True})
    return sorted(ph + extra, key=lambda p: p["start"])


def _dur(p: dict) -> float:
    return p["end"] - p["start"] + PAD


def _span(run: list[dict]) -> float:
    return run[-1]["end"] - run[0]["start"] + PAD


def _compress(run: list[dict], budget: float) -> list[dict] | None:
    """Fit a stretch into the budget the way an editor would: keep it whole if it fits,
    otherwise remove ONE contiguous dull block from its middle (a single jump cut).
    Returns None if no single block removal makes it fit."""
    if _span(run) <= budget:
        return run
    best = None
    for x in range(1, len(run) - 1):
        for y in range(x, len(run) - 1):
            new = (run[x - 1]["end"] - run[0]["start"]) + (run[-1]["end"] - run[y + 1]["start"]) + 2 * PAD
            if new <= budget:
                lost = sum(p["val"] for p in run[x:y + 1])
                if best is None or lost < best[0] - 1e-9:
                    best = (lost, x, y)
                break                     # larger y only removes more
    if not best:
        return None
    _, x, y = best
    return run[:x] + run[y + 1:]


def _total(kept: list[dict]) -> float:
    """Playback length of kept phrases once contiguous neighbours are merged."""
    t, cur = 0.0, None
    for p in kept:
        if cur and p["start"] - cur[1] < MERGE_GAP:
            cur[1] = p["end"]
        else:
            if cur:
                t += cur[1] - cur[0] + PAD
            cur = [p["start"], p["end"]]
    return t + (cur[1] - cur[0] + PAD if cur else 0.0)


def _pieces(kept: list[dict], words: list[dict], lo: float, hi: float, tl=None) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for p in kept:
        a = max(lo, p["start"] - LEAD_IN)
        b = _end_after(p["words"][-1], words, 0.25) if p["words"] else p["end"] + 0.15
        b = min(hi, b)
        if out and a - out[-1][1] < MERGE_GAP:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    res = []
    for a, b in out:
        res += _drop_dead_air(a, b, words, tl)
    return [(round(a, 3), round(b, 3)) for a, b in res if b - a > 0.3]


def _drop_dead_air(a: float, b: float, words: list[dict], tl=None) -> list[tuple[float, float]]:
    inside = [w for w in words if w["start"] >= a - 0.05 and w["end"] <= b + 0.05]
    if not inside:
        return [(a, b)]
    pieces, s = [], a
    for w1, w2 in zip(inside, inside[1:]):
        if w2["start"] - w1["end"] > DEAD_AIR * 1.5 and not _voiced(w1["end"], w2["start"], tl):
            pieces.append((s, w1["end"] + 0.25))
            s = w2["start"] - 0.2
    pieces.append((s, b))
    return [(x, y) for x, y in pieces if y - x > 0.3]


def best_reel_cut(words: list[dict], duration: float, peak: float | None, max_s: float = 30.0,
                  lo: float = 0.0, hi: float | None = None, min_s: float = 15.0,
                  timeline: list[dict] | None = None) -> list[tuple[float, float]]:
    """Return ordered source pieces [(a, b), ...] totalling <= max_s."""
    hi = duration if hi is None else min(hi, duration)
    ws = [w for w in words if w["start"] >= lo and w["end"] <= hi]
    tl = [f for f in (timeline or []) if lo <= f["t"] <= hi] or None
    ph = phrases(ws)
    if tl:
        ph = _add_voiced_gaps(ph, tl, lo, hi)
    if not ph:
        c = peak if peak is not None else (lo + hi) / 2
        a = max(lo, c - max_s * 0.6)
        return [(round(a, 3), round(min(hi, a + max_s), 3))]
    med = _voiced_median(tl)
    for p in ph:
        p["val"] = _strength(p["words"]) + _outburst(p["start"], p["end"], tl, ws, med) + 0.02

    best, best_score = None, -1.0
    for need in (min_s, 0.0):
        for i in range(len(ph)):
            for j in range(i, len(ph)):
                if ph[j]["end"] - ph[i]["start"] > max_s * 1.7:
                    break
                kept = _compress(ph[i:j + 1], max_s)
                if kept is None:
                    continue
                total = _total(kept)
                if total < need:
                    continue
                s = sum(p["val"] for p in kept) / math.sqrt(max(min_s, total))
                if peak is not None and any(p["start"] - 4 <= peak <= p["end"] + 4 for p in kept):
                    s += 0.6           # the moment chat/viewers/the detector reacted to
                if s > best_score:
                    best, best_score = (i, j, kept), s
        if best:
            break
    i, j, kept = best
    # end on the payoff: drop trailing pieces with no reaction in them
    while len(kept) > 1 and kept[-1]["val"] <= 0.12 and live_speech_raw(kept[-1]["words"])[0] < 0.3 \
            and _total(kept[:-1]) >= min(min_s, max_s):
        kept = kept[:-1]
    pieces = _pieces(kept, ws, lo, hi, tl)
    # ...and inside the last spoken phrase stop right after its last reaction word
    last = kept[-1]["words"]
    if last:
        full = live_speech_raw(last)[0]
        if full > 0:
            for k in range(1, len(last) + 1):
                if live_speech_raw(last[:k])[0] >= full:
                    a, b = pieces[-1]
                    pieces[-1] = (a, round(max(a + 0.5, min(b, _end_after(last[k - 1], ws, TAIL))), 3))
                    break
    # no scraps: a piece under 1.5 s next to a jump cut just looks like a glitch
    if len(pieces) > 1:
        pieces = [pc for pc in pieces if pc[1] - pc[0] >= 1.5] or pieces
    # setup splice: earliest strong short line before the chosen stretch, if it fits
    budget = max_s - sum(b - a for a, b in pieces)
    first = kept[0]
    if budget >= 3.0 and first["start"] - lo > 6.0:
        k0 = ph.index(first)
        for k in range(k0):
            best_m = None
            for m in range(k, k0):
                if not ph[m]["words"]:
                    continue
                sa = max(lo, ph[k]["start"] - LEAD_IN)
                sb = _end_after(ph[m]["words"][-1], ws, 0.25)
                if sb - sa > min(6.0, budget) or sb > first["start"] - 1.0:
                    break
                sc = live_speech_raw([w for q in ph[k:m + 1] for w in q["words"]])[0]
                if sc >= 0.3 and (best_m is None or sc > best_m[2]):
                    best_m = (sa, sb, sc)
            if best_m:
                pieces = [(round(best_m[0], 3), round(best_m[1], 3))] + pieces
                break
    # too little speech to build from (silent streamer, visual moment): show the stretch
    # around the moment instead of a 6-second scrap
    if sum(b - a for a, b in pieces) < 12.0:
        c = peak if peak is not None else (pieces[0][0] + pieces[-1][1]) / 2
        a = max(lo, c - min(18.0, max_s * 0.6))
        pieces = [(round(a, 3), round(min(hi, a + min(max_s, 26.0)), 3))]
    # hard guarantee of the house rule
    total = sum(b - a for a, b in pieces)
    while total > max_s + 1e-6 and pieces:
        a, b = pieces[-1]
        cut = total - max_s
        if b - a - cut >= 0.5:
            pieces[-1] = (a, round(b - cut, 3))
        else:
            pieces.pop()
        total = sum(b - a for a, b in pieces)
    return pieces
