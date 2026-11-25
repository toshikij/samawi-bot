"""The Quran, from the bundled Tanzil Uthmani text and the Kemenag translation (spec §6.1–6.2).

The LLM only ever emits a *reference*. This module resolves it to the verbatim source text. A
reference that does not resolve is an error the checks turn into a blocked draft.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from functools import cached_property

from .snapshot import QURAN_TEXT, SURAHS, TRANSLATION_KEMENAG, Snapshot

TOTAL_AYAHS = 6236
TOTAL_SURAHS = 114
MAX_RANGE = 5  # spec §6.2: quote at most ~5 ayat per post


class InvalidReference(ValueError):
    """A Quran reference that does not name real ayat, or names too many."""


@dataclass(frozen=True)
class Surah:
    number: int
    arabic: str
    latin: str
    meaning: str
    ayah_count: int
    meccan: bool


@dataclass(frozen=True, order=True)
class AyahRef:
    surah: int
    ayah: int

    def __str__(self) -> str:
        return f"{self.surah}:{self.ayah}"


@dataclass(frozen=True)
class AyahRange:
    surah: int
    start: int
    end: int

    @property
    def refs(self) -> list[AyahRef]:
        return [AyahRef(self.surah, n) for n in range(self.start, self.end + 1)]

    def __len__(self) -> int:
        return self.end - self.start + 1


def _is_diacritic(ch: str) -> bool:
    c = ord(ch)
    return 0x064B <= c <= 0x065F or c == 0x0670 or 0x06D6 <= c <= 0x06ED


def skeleton(text: str) -> str:
    """Consonant skeleton: diacritics and Quranic marks removed (for matching, never display)."""
    return "".join(ch for ch in text if not _is_diacritic(ch))


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


_SURAH_RE = re.compile(
    r'Surah\(\s*number:\s*(\d+),\s*name:\s*"([^"]+)",\s*latinName:\s*"([^"]+)",\s*'
    r'meaning:\s*"([^"]+)",\s*ayahCount:\s*(\d+),\s*isMeccan:\s*(true|false)'
)


def parse_surahs(dart_source: str) -> list[Surah]:
    surahs = [Surah(int(n), ar, lat, mean, int(cnt), mec == "true") for n, ar, lat, mean, cnt, mec in _SURAH_RE.findall(dart_source)]
    if [s.number for s in surahs] != list(range(1, TOTAL_SURAHS + 1)):
        raise ValueError(f"surahs.dart parsed to {len(surahs)} surahs, expected 1..114 in order")
    return surahs


_TANZIL_LINE = re.compile(r"^(\d+)\|(\d+)\|(.+)$")


def parse_tanzil(text: str) -> dict[int, list[str]]:
    by_surah: dict[int, list[str]] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = _TANZIL_LINE.match(line)
        if not m:
            raise ValueError(f"Unexpected Tanzil line: {line[:40]!r}")
        s, a, body = int(m[1]), int(m[2]), m[3]
        verses = by_surah.setdefault(s, [])
        if a != len(verses) + 1:
            raise ValueError(f"Tanzil gap at {s}:{a}")
        verses.append(body)
    total = sum(len(v) for v in by_surah.values())
    if sorted(by_surah) != list(range(1, TOTAL_SURAHS + 1)) or total != TOTAL_AYAHS:
        raise ValueError(f"Tanzil text has {len(by_surah)} surahs / {total} ayahs")
    return by_surah


class Quran:
    def __init__(self, arabic: dict[int, list[str]], translation: dict[str, dict[str, str]], surahs: list[Surah]):
        self._arabic = arabic
        self._translation = translation
        self.surahs = {s.number: s for s in surahs}
        for s in surahs:
            if len(arabic[s.number]) != s.ayah_count:
                raise ValueError(f"surahs.dart says {s.number} has {s.ayah_count} ayat; text has {len(arabic[s.number])}")
            tr = translation.get(str(s.number), {})
            if len(tr) != s.ayah_count:
                raise ValueError(f"Translation of surah {s.number} has {len(tr)} entries")

    @classmethod
    def load(cls, snap: Snapshot) -> Quran:
        return cls(
            parse_tanzil(snap.read_text(QURAN_TEXT)),
            json.loads(snap.read_text(TRANSLATION_KEMENAG)),
            parse_surahs(snap.read_text(SURAHS)),
        )

    @cached_property
    def basmala(self) -> str:
        return self._arabic[1][0]

    def _basmala_end(self, text: str) -> int:
        target = skeleton(self.basmala)
        i = matched = 0
        while i < len(text) and matched < len(target):
            if _is_diacritic(text[i]):
                i += 1
                continue
            if text[i] != target[matched]:
                return -1
            matched += 1
            i += 1
        if matched < len(target):
            return -1
        while i < len(text) and _is_diacritic(text[i]):
            i += 1
        return i

    def has_basmala_prefix(self, ref: AyahRef) -> bool:
        return ref.surah != 1 and ref.ayah == 1 and self._basmala_end(self.raw(ref)) > 0

    def raw(self, ref: AyahRef) -> str:
        self.validate(ref)
        return self._arabic[ref.surah][ref.ayah - 1]

    def arabic(self, ref: AyahRef) -> str:
        """The verse as displayed: verbatim, minus the surah-opening basmala heading (as the app)."""
        text = self.raw(ref)
        if self.has_basmala_prefix(ref):
            return text[self._basmala_end(text) :].strip()
        return text

    def translation(self, ref: AyahRef) -> str:
        self.validate(ref)
        return self._translation[str(ref.surah)][str(ref.ayah)]

    def surah_name(self, number: int) -> str:
        if number not in self.surahs:
            raise InvalidReference(f"No surah {number}")
        return self.surahs[number].latin

    def validate(self, ref: AyahRef) -> None:
        surah = self.surahs.get(ref.surah)
        if surah is None or not 1 <= ref.ayah <= surah.ayah_count:
            raise InvalidReference(f"{ref} is not an ayah")

    def citation(self, rng: AyahRange) -> str:
        """Inline reference as it appears on a post, e.g. (QS. Al-Baqarah: 282) or (QS. Al-Kahf: 1–5)."""
        name = self.surah_name(rng.surah)
        span = str(rng.start) if rng.start == rng.end else f"{rng.start}–{rng.end}"
        return f"(QS. {name}: {span})"

    def all_refs(self) -> list[AyahRef]:
        return [AyahRef(s, a + 1) for s, verses in self._arabic.items() for a in range(len(verses))]


_REF_RE = re.compile(r"^\s*(\d{1,3})\s*:\s*(\d{1,3})\s*(?:[-–]\s*(?:(\d{1,3})\s*:\s*)?(\d{1,3}))?\s*$")


def parse_reference(value: object, quran: Quran | None = None) -> AyahRange:
    """Parse "2:255", "18:1-5" or "18:1–18:5" strictly. Anything else is InvalidReference.

    Rejects: non-strings, empty, suffixes like "2:255a", ranges across surahs, reversed ranges, and
    ranges longer than MAX_RANGE. With `quran`, also rejects ayat that do not exist.
    """
    if not isinstance(value, str) or not value.strip():
        raise InvalidReference(f"Not a reference: {value!r}")
    m = _REF_RE.match(value)
    if not m:
        raise InvalidReference(f"Malformed reference {value!r}")
    surah, start = int(m[1]), int(m[2])
    end_surah = int(m[3]) if m[3] else surah
    end = int(m[4]) if m[4] else start
    if end_surah != surah:
        raise InvalidReference(f"{value!r} spans surahs")
    if end < start:
        raise InvalidReference(f"{value!r} is reversed")
    rng = AyahRange(surah, start, end)
    if len(rng) > MAX_RANGE:
        raise InvalidReference(f"{value!r} is longer than {MAX_RANGE} ayat")
    if surah < 1 or start < 1:
        raise InvalidReference(f"{value!r} is not an ayah")
    if quran is not None:
        for ref in rng.refs:
            quran.validate(ref)
    return rng


def from_parts(surah: object, ayah: object, end: object = None, quran: Quran | None = None) -> AyahRange:
    """Build a range from structured JSON fields ({"surah":2,"ayah":282}); bools and floats rejected."""
    for v in (surah, ayah) + ((end,) if end is not None else ()):
        if not isinstance(v, int) or isinstance(v, bool):
            raise InvalidReference(f"Reference parts must be integers, got {v!r}")
    text = f"{surah}:{ayah}" + (f"-{end}" if end is not None else "")
    return parse_reference(text, quran)
