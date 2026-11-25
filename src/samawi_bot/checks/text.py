"""Text normalisation for pattern checks, and character counting for platform limits.

Pattern checks run on `normalize()`, which defeats the usual evasions: case, diacritics,
zero-width characters, fullwidth forms, leetspeak (dij4min), and spacing tricks (u m m a,
Muslim  Pro). Limits are counted on what the platform counts: Unicode code points for Instagram
captions, UTF-8 bytes being stricter we use code points for Threads too but also cap bytes.
"""

from __future__ import annotations

import re
import unicodedata

ZERO_WIDTH = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u206f\u180e\ufeff\xad]")
_LEET = str.maketrans({"4": "a", "@": "a", "3": "e", "1": "i", "!": "i", "0": "o", "5": "s", "$": "s", "7": "t"})
_NON_WORD = re.compile(r"[^0-9a-z%]+")


def strip_invisible(text: str) -> str:
    return ZERO_WIDTH.sub("", text)


def fold(text: str) -> str:
    """NFKC, invisible characters removed, lowercased, Latin diacritics removed."""
    text = unicodedata.normalize("NFKC", strip_invisible(text)).replace("°", " derajat ")
    decomposed = unicodedata.normalize("NFD", text.lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")


def _join_single_letters(words: list[str]) -> list[str]:
    out: list[str] = []
    run: list[str] = []
    for w in words:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if run:
            out.append("".join(run) if len(run) > 1 else run[0])
            run = []
        out.append(w)
    if run:
        out.append("".join(run) if len(run) > 1 else run[0])
    return out


def normalize(text: str, leet: bool = False) -> str:
    """Folded, punctuation collapsed to single spaces, spaced-out letters re-joined."""
    folded = fold(text)
    if leet:
        # Only map leet characters inside words, so "99 %" or "5 waktu" stay numbers; a "!" that
        # ends a word is punctuation ("V1r4l!" must read as "viral", not "virali").
        folded = re.sub(r"!+(?![a-z])", " ", folded)
        folded = re.sub(r"(?<=[a-z])[4@3105$7!]+|[4@3105$7!]+(?=[a-z])", lambda m: m.group(0).translate(_LEET), folded)
    words = _NON_WORD.sub(" ", folded).split()
    return " ".join(_join_single_letters(words))


def variants(text: str) -> list[str]:
    """Every normalised form a pattern should be tried against."""
    plain = normalize(text)
    leet = normalize(text, leet=True)
    return [plain] if leet == plain else [plain, leet]


def count_chars(text: str) -> int:
    """Code points, which is what Instagram's 2,200 and Threads' 500 count."""
    return len(text)


def utf8_len(text: str) -> int:
    # surrogatepass: a lone surrogate (valid in JSON "\ud800", invalid UTF-8) must count, not crash.
    return len(text.encode("utf-8", "surrogatepass"))


def word_count(text: str) -> int:
    return len(re.findall(r"\S+", strip_invisible(text)))


ACRONYMS = frozenset({"WIB", "WITA", "WIT", "SAW", "SWT", "HR", "QS", "MUI", "NU", "UTC", "SAMAWI"})


def is_shouting(text: str) -> bool:
    """Two or more consecutive all-caps words of 3+ letters ("WAJIB TAHU"); acronyms exempt.

    SAMAWI is exempt because it is the comment keyword the CTA asks people to type.
    """
    words = re.findall(r"\b[^\W\d_]{3,}\b", strip_invisible(text))
    run = best = 0
    for w in words:
        run = run + 1 if w.isupper() and w not in ACRONYMS else 0
        best = max(best, run)
    return best >= 2


_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿\U0001f000-\U0001f2ff]")


def emoji_count(text: str) -> int:
    return len(_EMOJI.findall(text))


_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")


def numbers(text: str) -> list[str]:
    """Numeric tokens in canonical form: 6.236 → 6236, 4,03 → 4.03, 04:19 kept as a time."""
    out = []
    for tok in _NUMBER.findall(strip_invisible(text)):
        out.append(canonical_number(tok))
    return out


def canonical_number(tok: str) -> str:
    if ":" in tok:
        return tok
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", tok):
        return tok.replace(".", "")
    if re.fullmatch(r"\d+,\d+", tok):
        return tok.replace(",", ".")
    if re.fullmatch(r"\d+\.\d+", tok):
        return tok
    return tok.lstrip("0") or "0"
