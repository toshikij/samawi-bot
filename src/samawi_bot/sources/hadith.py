"""Hadith: HadeethEnc (Indonesian) as the only displayed source, fawazahmed0 grades as a cross-check.

Admission rule (spec §6.2), enforced by `admit()`; every rule fails closed:

1. HadeethEnc has an Indonesian translation and grades it *sahih* or *hasan*.
2. It is attributed to Bukhari and/or Muslim, or else to Abu Dawud / Tirmidzi / Nasa'i / Ibnu
   Majah **and** its matn is found in those Sunan in fawazahmed0 with no grader marking it weak.
   Attributions to anything else are rejected.
3. Displayed: HadeethEnc's Indonesian text verbatim and a short reference ("HR. Bukhari"). Its
   explanation (*syarah*) is never stored for display.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx

from .quran import skeleton


class Collection(StrEnum):
    BUKHARI = "bukhari"
    MUSLIM = "muslim"
    ABUDAWUD = "abudawud"
    TIRMIDHI = "tirmidhi"
    NASAI = "nasai"
    IBNMAJAH = "ibnmajah"


SAHIHAIN = (Collection.BUKHARI, Collection.MUSLIM)
SUNAN = (Collection.ABUDAWUD, Collection.TIRMIDHI, Collection.NASAI, Collection.IBNMAJAH)

DISPLAY = {
    Collection.BUKHARI: "Bukhari",
    Collection.MUSLIM: "Muslim",
    Collection.ABUDAWUD: "Abu Daud",
    Collection.TIRMIDHI: "Tirmidzi",
    Collection.NASAI: "Nasa'i",
    Collection.IBNMAJAH: "Ibnu Majah",
}

# Arabic attribution names, after `_norm_ar` (hamza and taa-marbuta folded).
_AR_NAMES: dict[str, Collection] = {
    "البخاري": Collection.BUKHARI,
    "مسلم": Collection.MUSLIM,
    "ابو داود": Collection.ABUDAWUD,
    "الترمذي": Collection.TIRMIDHI,
    "النسايي": Collection.NASAI,
    "ابن ماجه": Collection.IBNMAJAH,
}

_WEAK_GRADE_TOKENS = ("daif", "da'if", "dhaif", "dha'if", "munkar", "shadh", "mawdu", "maudu", "matruk", "batil")
_NOT_PROPHETIC_TOKENS = ("maqtu", "mauquf", "muquf", "mawquf")
_WEAK_AR = ("ضعيف", "موضوع", "منكر", "شاذ", "باطل")
_STRONG_AR = ("صحيح", "حسن")


class HadithRejected(ValueError):
    pass


@dataclass(frozen=True)
class Hadith:
    id: str
    title: str
    text_id: str  # HadeethEnc Indonesian, verbatim
    arabic: str
    attribution_ar: str
    grade: str
    grade_ar: str
    collections: tuple[Collection, ...] = field(default=())

    @property
    def reference(self) -> str:
        """The short inline reference shown on a post, e.g. "HR. Bukhari dan Muslim"."""
        names = [DISPLAY[c] for c in self.collections]
        if len(names) == 1:
            return f"HR. {names[0]}"
        return "HR. " + ", ".join(names[:-1]) + " dan " + names[-1]


def _norm_ar(text: str) -> str:
    text = skeleton(text)
    for a, b in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ٱ", "ا"), ("ة", "ه"), ("ى", "ي"), ("ئ", "ي"), ("ؤ", "و")):
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", re.sub(r"[^ء-ي\s]", " ", text)).strip()


def parse_attribution(attribution_ar: str) -> tuple[Collection, ...]:
    """Which of the six books HadeethEnc attributes this to, in canonical order."""
    norm = _norm_ar(attribution_ar)
    if "متفق عليه" in norm:
        return SAHIHAIN
    # Attributions are short ("رواه X وY"); without a رواه clause it is something else, e.g. "we
    # narrated it in Al-Hujjah", and is rejected.
    if "رواه" not in norm:
        return ()
    found = {c for name, c in _AR_NAMES.items() if re.search(rf"(^| |و){re.escape(name)}( |$)", norm)}
    return tuple(c for c in Collection if c in found)


_WEAK_ID = ("daif", "dhaif", "da'if", "dha'if", "lemah", "palsu", "maudhu", "mawdu", "munkar")


def grade_is_strong(grade: str, grade_ar: str) -> bool:
    """Sahih or hasan, and nothing in either grade says weak: if the Arabic and Indonesian labels
    disagree, the hadith is rejected (fail closed)."""
    g = _fold_grade(grade)
    if any(t in g for t in _WEAK_ID) or any(t in grade_ar for t in _WEAK_AR):
        return False
    if grade_ar.strip():
        return any(t in grade_ar for t in _STRONG_AR)
    return "sahih" in g or "hasan" in g


def _fold_grade(grade: str) -> str:
    """Lower-case, strip combining marks (Ḍaʿīf → daʿif) and unify ayn/hamza/quote signs to '."""
    text = "".join(ch for ch in unicodedata.normalize("NFKD", grade) if not unicodedata.combining(ch)).lower()
    for q in ("ʿ", "ʻ", "ʾ", "’", "‘", "`"):
        text = text.replace(q, "'")
    return text


def grader_marks_weak(grades: Iterable[dict[str, str]]) -> bool:
    for g in grades:
        text = _fold_grade(str(g.get("grade", "")))
        if any(t in text for t in _WEAK_GRADE_TOKENS + _NOT_PROPHETIC_TOKENS + _WEAK_AR):
            return True
    return False


def extract_matn(arabic: str) -> str:
    """The Prophet's words: the text inside «…» if present, otherwise the whole text."""
    quoted = re.findall(r"«([^»]+)»", arabic)
    body = " ".join(quoted) if quoted else arabic
    return _norm_ar(body)


@dataclass
class SunanIndex:
    """fawazahmed0 Arabic editions, normalised for matn search. Verification only, never shown."""

    hadiths: dict[Collection, list[dict[str, Any]]]
    _norm: dict[Collection, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._norm = {c: [_norm_ar(h.get("text", "")) for h in hs] for c, hs in self.hadiths.items()}

    def find(self, collection: Collection, matn: str) -> list[dict[str, Any]]:
        if len(matn) < 20:
            return []
        texts = self._norm.get(collection, [])
        return [self.hadiths[collection][i] for i, t in enumerate(texts) if matn in t]


def admit(raw: dict[str, Any], sunan: SunanIndex | None) -> Hadith:
    """Apply the admission rule to one HadeethEnc `hadeeths/one` response. Raises HadithRejected."""
    hid = str(raw.get("id", ""))
    text_id = str(raw.get("hadeeth") or "").strip()
    if not hid or not text_id:
        raise HadithRejected(f"{hid or '?'}: no Indonesian translation")
    grade, grade_ar = str(raw.get("grade") or ""), str(raw.get("grade_ar") or "")
    if not grade_is_strong(grade, grade_ar):
        raise HadithRejected(f"{hid}: HadeethEnc grade {grade!r} is not sahih/hasan")
    collections = parse_attribution(str(raw.get("attribution_ar") or ""))
    if not collections:
        raise HadithRejected(f"{hid}: not attributed to an allowed collection ({raw.get('attribution')!r})")
    allowed = tuple(c for c in collections if c in SAHIHAIN)
    if not allowed:
        if sunan is None:
            raise HadithRejected(f"{hid}: Sunan-only hadith and no grade cross-check data available")
        matn = extract_matn(str(raw.get("hadeeth_ar") or ""))
        matched: list[Collection] = []
        for c in collections:
            hits = sunan.find(c, matn)
            if any(grader_marks_weak(h.get("grades", [])) for h in hits):
                raise HadithRejected(f"{hid}: a grader marks it weak in {DISPLAY[c]}")
            if hits:
                matched.append(c)
        if not matched:
            raise HadithRejected(f"{hid}: matn not found in any attributed Sunan; cannot cross-check grades")
        allowed = tuple(matched)
    return Hadith(
        id=hid,
        title=str(raw.get("title") or ""),
        text_id=text_id,
        arabic=str(raw.get("hadeeth_ar") or ""),
        attribution_ar=str(raw.get("attribution_ar") or ""),
        grade=grade,
        grade_ar=grade_ar,
        collections=allowed,
    )


# ── Fetching and the on-disk cache ───────────────────────────────────────────────────────────


STALE_AFTER = timedelta(days=30)
DEFAULT_CATEGORIES = ("1", "4", "5", "7")  # Al-Qur'an, Fikih, Keutamaan & Adab, Sejarah


@dataclass
class HadithCache:
    """`root/hadeethenc/<id>.json` (raw API rows, syarah stripped) and `root/fawaz/<edition>.json`."""

    root: Path

    def _dir(self, name: str) -> Path:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def put_raw(self, row: dict[str, Any], now: datetime) -> None:
        clean = {k: v for k, v in row.items() if not k.startswith(("explanation", "hints", "words_meanings"))}
        clean["_fetched_at"] = now.isoformat()
        (self._dir("hadeethenc") / f"{row['id']}.json").write_text(json.dumps(clean, ensure_ascii=False), "utf-8")

    def get_raw(self, hid: str) -> dict[str, Any] | None:
        p = self.root / "hadeethenc" / f"{hid}.json"
        return json.loads(p.read_text("utf-8")) if p.is_file() else None

    def ids(self) -> list[str]:
        d = self.root / "hadeethenc"
        return sorted(p.stem for p in d.glob("*.json")) if d.is_dir() else []

    def oldest_fetch(self) -> datetime | None:
        stamps = [datetime.fromisoformat(r["_fetched_at"]) for i in self.ids() if (r := self.get_raw(i)) and "_fetched_at" in r]
        return min(stamps) if stamps else None

    def put_edition(self, collection: Collection, data: dict[str, Any]) -> None:
        (self._dir("fawaz") / f"{collection.value}.json").write_text(json.dumps(data["hadiths"], ensure_ascii=False), "utf-8")

    def sunan_index(self) -> SunanIndex | None:
        d = self.root / "fawaz"
        loaded = {c: json.loads((d / f"{c.value}.json").read_text("utf-8")) for c in SUNAN if (d / f"{c.value}.json").is_file()}
        return SunanIndex(loaded) if loaded else None


@dataclass
class IngestReport:
    fetched: int = 0
    failed: int = 0
    api_down: bool = False
    stale_days: int | None = None
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def ingest(
    client: httpx.Client,
    cache: HadithCache,
    base_url: str,
    fawaz_base: str,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    categories: Iterable[str] = DEFAULT_CATEGORIES,
    per_page: int = 100,
    pages: int = 2,
    sleep: Callable[[float], None] = time.sleep,
) -> IngestReport:
    """Refresh the HadeethEnc cache and the fawazahmed0 Sunan editions.

    If HadeethEnc is down the cached snapshot is used; a snapshot older than 30 days is reported
    (the caller alerts) but production continues (spec §10.3).
    """
    report = IngestReport()
    try:
        ids: list[str] = []
        for cat in categories:
            for page in range(1, pages + 1):
                resp = client.get(f"{base_url}/hadeeths/list/", params={"language": "id", "category_id": cat, "page": page, "per_page": per_page})
                resp.raise_for_status()
                rows = resp.json().get("data", [])
                ids.extend(str(r["id"]) for r in rows)
                if len(rows) < per_page:
                    break
        # Known rows are re-fetched once they are older than STALE_AFTER, so displayed text tracks
        # HadeethEnc's latest version (their terms, SOURCES.md).
        fresh = {
            i
            for i in cache.ids()
            if (r := cache.get_raw(i)) and now() - datetime.fromisoformat(r.get("_fetched_at", "1970-01-01T00:00:00+00:00")) < STALE_AFTER
        }
        for hid in dict.fromkeys(ids):
            if hid in fresh:
                continue
            try:
                resp = client.get(f"{base_url}/hadeeths/one/", params={"language": "id", "id": hid})
                resp.raise_for_status()
                cache.put_raw(resp.json(), now())
                report.fetched += 1
            except (httpx.HTTPError, ValueError) as exc:
                report.failed += 1
                report.errors.append(f"{hid}: {exc}")
            sleep(0.2)
    except (httpx.HTTPError, ValueError, KeyError, IndexError) as exc:
        report.api_down = True
        report.errors.append(f"HadeethEnc unavailable: {exc}")
    for c in SUNAN:
        try:
            resp = client.get(f"{fawaz_base}/editions/ara-{c.value}.min.json")
            resp.raise_for_status()
            cache.put_edition(c, resp.json())
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            report.errors.append(f"fawazahmed0 {c.value}: {exc}")
    oldest = cache.oldest_fetch()
    if report.api_down and oldest is not None:
        age = now() - oldest
        report.stale_days = age.days if age > STALE_AFTER else None
    return report


class HadithLibrary:
    """Admitted hadith only. Duplicates across collections collapse to one HadeethEnc id."""

    def __init__(self, cache: HadithCache):
        self.cache = cache
        self._sunan = cache.sunan_index()
        self._admitted: dict[str, Hadith] = {}
        self.rejections: dict[str, str] = {}

    def get(self, hid: str) -> Hadith:
        if hid in self._admitted:
            return self._admitted[hid]
        raw = self.cache.get_raw(hid)
        if raw is None:
            raise HadithRejected(f"{hid}: not in the HadeethEnc snapshot")
        try:
            h = admit(raw, self._sunan)
        except HadithRejected as exc:
            self.rejections[hid] = str(exc)
            raise
        self._admitted[hid] = h
        return h

    def candidates(self, limit: int | None = None) -> list[Hadith]:
        out: list[Hadith] = []
        for hid in self.cache.ids():
            try:
                out.append(self.get(hid))
            except HadithRejected:
                continue
            if limit is not None and len(out) >= limit:
                break
        return out
