"""Tamil script -> casual Tanglish (how Tamil speakers type Tamil in English letters).

Whisper transcribes Tamil in Tamil script with word timestamps; captions for a Tamil
streamer read best in Tanglish ("dei enna panra", "semma", "vera level"), so each word is
romanized on its own and keeps its exact timing. This is the casual spelling people use in
chat, not a scholarly scheme (no diacritics): context decides k/g, th/dh, s/ch, p/b, d/t.
Latin text (English words Whisper wrote in English) passes through unchanged.
"""
from __future__ import annotations

import re

VOWELS = {"அ": "a", "ஆ": "aa", "இ": "i", "ஈ": "ee", "உ": "u", "ஊ": "oo", "எ": "e", "ஏ": "e",
          "ஐ": "ai", "ஒ": "o", "ஓ": "o", "ஔ": "au"}
SIGNS = {"ா": "aa", "ி": "i", "ீ": "ee", "ு": "u", "ூ": "oo", "ெ": "e", "ே": "e", "ை": "ai",
         "ொ": "o", "ோ": "o", "ௌ": "au"}
VIRAMA = "்"
CONS = {"க": "k", "ங": "ng", "ச": "s", "ஞ": "nj", "ட": "d", "ண": "n", "த": "th", "ந": "n", "ப": "p",
        "ம": "m", "ய": "y", "ர": "r", "ல": "l", "வ": "v", "ழ": "zh", "ள": "l", "ற": "r", "ன": "n",
        "ஜ": "j", "ஷ": "sh", "ஸ": "s", "ஹ": "h", "ஶ": "sh"}
NASAL = {"ங", "ஞ", "ண", "ந", "ம", "ன"}
DIGITS = {chr(0x0BE6 + i): str(i) for i in range(10)}
TAMIL = re.compile(r"[஀-௿]")


def _units(word: str) -> list[tuple[str | None, str, str]]:
    """Split into (consonant or None, vowel romanization, raw) units. vowel '' = virama (dead consonant)."""
    out: list[tuple[str | None, str, str]] = []
    i = 0
    while i < len(word):
        ch = word[i]
        if ch in CONS:
            nxt = word[i + 1] if i + 1 < len(word) else ""
            if nxt == VIRAMA:
                out.append((ch, "", ch + nxt)); i += 2
            elif nxt in SIGNS:
                out.append((ch, SIGNS[nxt], ch + nxt)); i += 2
            else:
                out.append((ch, "a", ch)); i += 1
        elif ch in VOWELS:
            out.append((None, VOWELS[ch], ch)); i += 1
        elif ch == "ஃ":
            out.append((None, "h", ch)); i += 1
        else:
            out.append((None, DIGITS.get(ch, ch), ch)); i += 1
    return out


def _consonant(units, k: int) -> str:
    c, v, _ = units[k]
    prev = units[k - 1] if k > 0 else None
    nxt = units[k + 1] if k + 1 < len(units) else None
    initial = prev is None or (prev[0] is None and not re.match(r"[aeiou]", prev[1] or ""))
    after_dead = prev is not None and prev[0] is not None and prev[1] == ""
    doubled = after_dead and prev[0] == c
    after_nasal = after_dead and prev[0] in NASAL
    intervocalic = prev is not None and prev[1] not in ("",) and not initial
    if v == "":                                    # dead consonant: depends on what follows
        if c == "ங":
            return "n" if nxt and nxt[0] == "க" else "ng"
        if c == "ஞ":
            return "n" if nxt and nxt[0] == "ச" else "nj"
        if c == "ச" and nxt and nxt[0] == "ச":
            return ""                              # ச்ச -> "ch" (pachai)
        if c == "த" and nxt and nxt[0] == "த":
            return ""                              # த்த -> "th" (sathiyama, pathu, muthu)
        if c in ("ட", "ற") and nxt and nxt[0] == c:
            return "t"                             # vittu, vetri
        if c == "ற" and nxt and nxt[0] == "ற":
            return "t"
    base = CONS[c]
    if c == "க":
        if doubled:
            return "k"
        if after_nasal or (intervocalic and v != ""):
            return "g"                             # enga, pagal
        return "k"
    if c == "ச":
        if doubled:
            return "ch"
        if after_dead and prev[0] == "ஞ":
            return "j"                             # konjam
        return "s"                                 # sari, pesu
    if c == "ட":
        return "t" if doubled else "d"             # vittu / vidu, vendam
    if c == "த":
        if doubled:
            return "th"
        if after_dead and prev[0] in ("ந", "ன"):
            return "dh"                            # vandhu
        if intervocalic and v != "":
            return "dh"                            # adhu, edhu, podhum
        return "th"                                # thambi, thaan
    if c == "ப":
        return "b" if after_dead and prev[0] == "ம" else "p"   # ambu
    if c == "ற":
        if doubled:
            return "r"                             # vet-ri
        if after_dead and prev[0] == "ன":
            return "dr"                            # endru
        return "r"
    return base


def romanize_word(word: str) -> str:
    if not TAMIL.search(word):
        return word
    units = _units(word)
    syllables = sum(1 for c, v, _ in units if v not in ("",) and (c is not None or v in VOWELS.values()))
    out = []
    for k, (c, v, raw) in enumerate(units):
        if c is None:
            out.append(v)
            continue
        out.append(_consonant(units, k))
        if v:
            last = k == len(units) - 1
            # casual spelling drops the long final -aa in longer words: poda, vaada -> "vaada"? keep 1-syllable "vaa"
            out.append("a" if (last and v == "aa" and syllables > 1) else v)
    return "".join(out)


def romanize_text(text: str) -> str:
    return re.sub(r"[஀-௿]+", lambda m: romanize_word(m.group(0)), text)


def romanize_words(words: list[dict]) -> list[dict]:
    """Same word list, same timings, Tamil script replaced by Tanglish."""
    return [{**w, "text": romanize_text(w["text"])} for w in words]


def is_tamil(text: str) -> bool:
    return bool(TAMIL.search(text or ""))
