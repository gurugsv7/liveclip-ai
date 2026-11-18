"""Layer B scoring: transcript features, laughter, novelty, weighted total,
and transcript-derived title suggestions (never invented text)."""
from __future__ import annotations

import re

_OLD_SHOCK = ["no way", "what the", "oh my god", "omg", "bro", "wait", "what?", "are you serious", "holy", "let's go",
         "lets go", "nah", "yo", "dude", "insane", "crazy", "oh no", "run", "help", "stop", "why", "how"]
LAUGH = ["haha", "hahaha", "lmao", "lol", "hehe"]
CONFLICT = ["shut up", "you're", "youre", "liar", "fight", "dumb", "stupid", "trash", "ratio"]
WIN_FAIL = ["won", "win", "clutch", "died", "dead", "lost", "fail", "gg", "first try", "finally", "world record"]
PROFANITY_RE = re.compile(r"\bf+u+c+k+\w*|\bs+h+i+t+\w*|\bd+a+m+n+\w*", re.I)
TOKEN_RE = re.compile(r"[a-z0-9']+")


def tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall(text.lower()))


# Reaction phrases for whole-clip scoring. Whole-word matches only ("how" must not match "show"),
# and no everyday words (what/why/wait/yo/bro/run) — those made plain talk look like hype.
SHOCK = ["no way", "what the", "oh my god", "oh my gosh", "omg", "are you serious", "are you kidding", "holy",
         "let's go", "lets go", "insane", "oh no", "no shot", "of all time", "in human history", "sick to my stomach",
         "i'm done", "what just happened", "why did i", "what was that", "how did i", "i can't believe"]
LAUGH = ["haha", "hahaha", "lmao", "lol", "hehe"]
CONFLICT = ["shut up", "liar", "stupid", "trash", "idiot", "idiots"]
WIN_FAIL = ["clutch", "first try", "world record", "we won", "i won", "i lost", "gg"]


def _count(low: str, phrases: list[str]) -> int:
    return sum(len(re.findall(r"(?<![\w'])" + re.escape(p) + r"(?![\w'])", low)) for p in phrases)


def transcript_features(text: str, words: list[dict], duration: float) -> dict:
    low = text.lower()
    n_words = len(words)
    wps = n_words / duration if duration > 0 else 0
    excl = low.count("!") + low.count("?")
    shock = _count(low, SHOCK)
    laugh = _count(low, LAUGH)
    conflict = _count(low, CONFLICT)
    winfail = _count(low, WIN_FAIL)
    prof = len(PROFANITY_RE.findall(low))
    energy = min(1.0, wps / 3.2)
    intensity = min(1.0, (shock * 0.12 + excl * 0.06 + prof * 0.08 + conflict * 0.1 + winfail * 0.1))
    score = 0.0 if n_words == 0 else min(1.0, 0.35 * energy + 0.65 * intensity)
    return {"score": round(score, 3), "words_per_s": round(wps, 2), "shock": shock, "laugh": laugh,
            "conflict": conflict, "win_fail": winfail, "profanity": prof, "exclaim": excl,
            "laugh_score": round(min(1.0, laugh * 0.25), 3)}


def novelty(text: str, recent_texts: list[str], seconds_since_last: float | None) -> float:
    t = tokens(text)
    sim = 0.0
    for r in recent_texts:
        rt = tokens(r)
        if t and rt:
            sim = max(sim, len(t & rt) / len(t | rt))
    score = 1.0 - sim
    if seconds_since_last is not None and seconds_since_last < 60:
        score *= 0.6
    return round(max(0.0, min(1.0, score)), 3)


def drop_quiet_partner(scores: dict, strong: float = 0.6, weak: float = 0.3, chat_info: dict | None = None) -> None:
    """Chat and audio are two views of the same reaction. When one is strong and
    the other is near zero (calm-voiced streamer + exploding chat, or a scream
    over a slow chat), the quiet one is absence of evidence, not evidence
    against — exclude it instead of letting it drag the average down."""
    views = [k for k in ("chat", "audio", "speech") if scores.get(k) is not None]
    # only a crowd (chat) or a physical reaction (loudness/laugh/scream) is strong enough to
    # make the quiet views "no evidence"; excited words alone are weaker (ASR can hallucinate)
    if not any(scores.get(k) is not None and scores[k] >= strong for k in ("chat", "audio")):
        return
    # An active chat (>= 1 msg/s normally) that stayed at or below its normal pace IS evidence:
    # a real moment makes a busy chat react. Real case: xqc watching a bodycam video, one loud
    # bang, chat at 3.2/s vs a 6/s normal -> it was clipped as "audio excitement".
    chat_quiet_but_active = bool(chat_info) and (chat_info.get("baseline") or 0) >= 1.0         and (chat_info.get("z") if chat_info.get("z") is not None else 99) <= 0.5
    for k in views:
        if k == "chat" and chat_quiet_but_active:
            continue
        if scores[k] < weak:
            scores[k] = None


def weighted(scores: dict, weights: dict) -> float:
    """Weighted mean over the signals that are actually available."""
    total, wsum = 0.0, 0.0
    for k, w in weights.items():
        v = scores.get(k)
        if v is None or w <= 0:
            continue
        total += w * v
        wsum += w
    return round(total / wsum, 3) if wsum else 0.0


def title_suggestions(streamer: str, words: list[dict], text: str, peak_offset: float | None,
                      window: list[tuple[float, float]] | None = None) -> list[str]:
    """Hook/title candidates quoted from what was actually said: complete lines (3-12 words)
    from inside the reel when its cut is known, ranked by reaction strength and closeness
    to the peak. No streamer-name prefix (it's on-screen hook text)."""
    fillers = {"um", "uh", "uhm", "umm", "erm", "er", "hmm", "mm", "mhm"}
    ws = [w for w in words if re.sub(r"[^\w']", "", w["text"].lower()) not in fillers]
    if window:
        ws = [w for w in ws if any(a - 0.05 <= w["start"] and w["end"] <= b + 0.05 for a, b in window)] or ws
    if not ws:
        return []
    sents, cur = [], []
    for i, w in enumerate(ws):
        cur.append(w)
        gap = (ws[i + 1]["start"] - w["end"]) if i + 1 < len(ws) else 9
        if re.search(r"[.!?]$", w["text"]) or gap > 0.6:
            sents.append(cur)
            cur = []
    if cur:
        sents.append(cur)

    def clean(ws_: list[dict]) -> str:
        t = " ".join(w["text"] for w in ws_).strip().strip('"')
        t = re.sub(r"^(and|but|so|like|um|uh)\b[ ,]*", "", t, flags=re.I)
        return (t[:1].upper() + t[1:]) if t else t

    scored = []
    for sw in sents:
        n = len(sw)
        if n < 3:
            continue
        sw = sw[:12]
        txt = clean(sw)
        raw = live_speech_raw(sw)[0]
        sc = raw * 3 + txt.count("!") * 0.5 + txt.count("?") * 0.3
        sc += 0.5 if re.search(r"[.!?]$", sw[-1]["text"]) and n <= 12 else 0.0     # complete line
        if re.search(r"[,;:\-]$", sw[-1]["text"]) or n > 12:
            sc -= 0.6                                                              # trails off mid-sentence
        txt = txt.rstrip(",;:- ")
        if peak_offset is not None:
            sc += max(0.0, 1.5 - abs(sw[0]["start"] - peak_offset) / 10)
        if raw == 0 and "!" not in txt and "?" not in txt:
            sc -= 1.0                                                              # plain narration
        sc += min(n, 9) * 0.08 - (0.7 if n < 4 else 0.0)                          # 4-10 word hooks read best
        if len(txt) > 60:
            txt = txt[:57].rsplit(" ", 1)[0] + "…"
        scored.append((sc, txt))
    out, seen = [], set()
    for _, t in sorted(scored, key=lambda x: -x[0]):
        if t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
    return out[:3]


# ---------------------------------------------------------------- live speech (Layer A)
# Reaction phrases carry most of the weight; everyday filler words ("bro", "wait")
# only a little, and a streamer's own habits are subtracted by SpeechNormalizer.
_STRONG = [
    r"\blet'?s (\w+ )?go+\b", r"\boh my (god|gosh|lord)\b", r"\bno way\b", r"\bwhat the (fuck|hell|heck)\b",
    r"\bholy (shit|crap|moly)\b", r"\bare you (kidding|serious)\b", r"\bi can'?t believe\b", r"\bclip (it|that)\b",
    r"\bthank (god|you god)\b", r"\bget (out|outta)\b", r"\bwhat just happened\b", r"\bi'?m (dead|crying)\b",
    r"\bno+ no+ no+\b", r"\byes+ yes+\b", r"\bwe (won|did it)\b", r"\boh no+\b",
    r"\bof all time\b", r"\bin human history\b", r"\bsick to my stomach\b", r"\bi'?m done\b", r"\bno shot\b",
    # self-blame / disbelief payoffs ("I had a C4, why did I swing?")
    r"\bwhy (did|would|do) i\b", r"\bwhat was that\b", r"\bhow (did|do) i\b",
]
_MEDIUM = [r"\bbro\b", r"\bdude\b", r"\bwhat\?", r"\binsane\b", r"\bcrazy\b", r"\bwait\b", r"\bhelp\b", r"\bstop\b",
           r"\bnah\b", r"\bhaha+\b", r"\bbeautiful\b", r"\bgg\b", r"\bdamn\b"]


def live_speech_raw(words: list[dict]) -> tuple[float, float | None, list[str]]:
    """Excitement of a short live window from what was said: (0..1 score, peak
    time of the strongest phrase relative to the window, matched phrases)."""
    if not words:
        return 0.0, None, []
    text = " ".join(w["text"] for w in words)
    low = re.sub(r"[^\w'?! ]+", " ", text.lower())
    low = re.sub(r"\s+", " ", low)
    score, hits, peak_pos = 0.0, [], None
    for pat in _STRONG:
        for m in re.finditer(pat, low):
            score += 0.35
            hits.append(m.group(0))
            peak_pos = m.start() if peak_pos is None else peak_pos
    for pat in _MEDIUM:
        n = len(re.findall(pat, low))
        score += min(2, n) * 0.08
    prof = len(PROFANITY_RE.findall(low))
    score += min(3, prof) * 0.10
    # repetition: "he didn't he didn't he didn't", "yes yes yes"
    toks = re.findall(r"[a-z']+", low)
    for n in (1, 2, 3):
        grams = [" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1)]
        for i in range(len(grams) - 2 * n):
            if grams[i] == grams[i + n] == grams[i + 2 * n] and len(grams[i]) > 2:   # not "la la la"
                # repetition is a strong cue only alongside another reaction ("no he didn't he
                # didn't... oh thank god"); alone it's often game comms ("up metal, up metal, up metal")
                score += 0.3 if any(not h.startswith("repeat") for h in hits) else 0.12
                hits.append(f"repeat: {grams[i]}")
                break
        else:
            continue
        break
    score += min(3, text.count("!")) * 0.05
    peak_t = None
    if peak_pos is not None:
        # map char position back to the word that contains it
        pos = 0
        for w in words:
            pos += len(re.sub(r"[^\w'?! ]+", " ", w["text"].lower())) + 1
            if pos > peak_pos:
                peak_t = w["start"]
                break
    return round(min(1.0, score), 3), peak_t, hits[:6]


class SpeechNormalizer:
    """Per-streamer: subtracts half of the streamer's typical window score (over the
    last ~5 min) so someone who says "bro, what?!" constantly isn't always 'hype'."""

    def __init__(self, history: int = 40):
        from collections import deque

        self.hist = deque(maxlen=history)

    def __call__(self, raw: float) -> float:
        typical = sorted(self.hist)[len(self.hist) // 2] if len(self.hist) >= 5 else 0.0
        self.hist.append(raw)
        return round(max(0.0, min(1.0, raw - 0.5 * typical)), 3)
