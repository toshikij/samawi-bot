"""Hadith admission (spec §6.2) and the HadeethEnc / fawazahmed0 ingest (spec §10.3 "Hadith")."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from conftest import FIXTURES, NOW
from samawi_bot.sources.hadith import (
    DEFAULT_CATEGORIES,
    DISPLAY,
    SAHIHAIN,
    STALE_AFTER,
    SUNAN,
    Collection,
    Hadith,
    HadithCache,
    HadithLibrary,
    HadithRejected,
    IngestReport,
    SunanIndex,
    _norm_ar,
    admit,
    extract_matn,
    grade_is_strong,
    grader_marks_weak,
    ingest,
    parse_attribution,
)

HADEETHENC = FIXTURES / "hadith" / "hadeethenc"
FAWAZ = FIXTURES / "hadith" / "fawaz"
BASE = "https://hadeethenc.test/api/v1"
FAWAZ_BASE = "https://fawaz.test"


def raw(hid: str, **overrides: Any) -> dict[str, Any]:
    data = json.loads((HADEETHENC / f"{hid}.json").read_text("utf-8"))
    data.update(overrides)
    return data


@pytest.fixture(scope="module")
def sunan() -> SunanIndex:
    index = HadithCache(FIXTURES / "hadith").sunan_index()
    assert index is not None
    return index


@pytest.fixture
def library(hadith_dir: Path) -> HadithLibrary:
    return HadithLibrary(HadithCache(hadith_dir))


# ── The real fixture rows ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("hid", "collections", "reference"),
    [
        ("2962", (Collection.BUKHARI, Collection.MUSLIM), "HR. Bukhari dan Muslim"),
        ("3044", (Collection.BUKHARI,), "HR. Bukhari"),
        ("3342", (Collection.MUSLIM,), "HR. Muslim"),
        ("3350", (Collection.ABUDAWUD,), "HR. Abu Daud"),
        ("3567", (Collection.TIRMIDHI, Collection.IBNMAJAH), "HR. Tirmidzi dan Ibnu Majah"),
    ],
)
def test_real_rows_admitted(library: HadithLibrary, hid: str, collections: tuple[Collection, ...], reference: str) -> None:
    h = library.get(hid)
    row = raw(hid)
    assert h.collections == collections
    assert h.reference == reference
    assert h.id == hid
    assert h.text_id == row["hadeeth"].strip()  # verbatim Indonesian
    assert h.arabic == row["hadeeth_ar"]
    assert (h.title, h.grade, h.grade_ar, h.attribution_ar) == (row["title"], row["grade"], row["grade_ar"], row["attribution_ar"])


@pytest.mark.parametrize(
    ("hid", "reason"),
    [
        ("4191", "a grader marks it weak in Tirmidzi"),
        ("5344", "a grader marks it weak in Ibnu Majah"),
        ("66535", "not attributed to an allowed collection"),
    ],
)
def test_real_rows_rejected(library: HadithLibrary, hid: str, reason: str) -> None:
    with pytest.raises(HadithRejected, match=reason):
        library.get(hid)
    assert reason in library.rejections[hid]


def test_nasai_unmatched_is_dropped_from_reference(sunan: SunanIndex) -> None:
    # 3567 is attributed to Tirmidzi, Nasa'i and Ibnu Majah; the Nasa'i edition has no match, so
    # the reference names only the collections whose grades were actually cross-checked.
    h = admit(raw("3567"), sunan)
    assert Collection.NASAI not in h.collections


# ── Admission rule, branch by branch ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("overrides", [{"hadeeth": ""}, {"hadeeth": "   "}, {"hadeeth": None}, {"id": ""}, {"id": None, "hadeeth": None}])
def test_no_translation_rejected(sunan: SunanIndex, overrides: dict[str, Any]) -> None:
    with pytest.raises(HadithRejected, match="no Indonesian translation"):
        admit(raw("3044", **overrides), sunan)


def test_missing_id_rejected() -> None:
    row = raw("3044")
    del row["id"]
    with pytest.raises(HadithRejected, match=r"^\?: no Indonesian translation"):
        admit(row, None)


@pytest.mark.parametrize(
    ("grade", "grade_ar"),
    [
        ("Daif", "ضعيف"),
        ("Sahih", "ضعيف"),  # grade_ar is authoritative when present
        ("Sahih", "حسن ضعيف"),
        ("Sahih", "موضوع"),
        ("Hasan", "مقبول"),  # no strong token at all
        ("Daif", ""),
        ("Sahih lemah", ""),
        ("Hadis palsu", ""),
        ("Maudhu", ""),
        ("Dhaif", ""),
        ("", ""),
        (None, None),
    ],
)
def test_weak_hadeethenc_grade_rejected(grade: str | None, grade_ar: str | None) -> None:
    with pytest.raises(HadithRejected, match="is not sahih/hasan"):
        admit(raw("3044", grade=grade, grade_ar=grade_ar), None)


@pytest.mark.parametrize(
    ("grade", "grade_ar", "strong"),
    [
        ("Sahih", "صحيح", True),
        ("Hasan", "حسن", True),
        ("", "حديث صحيح", True),
        ("Daif", "صحيح", False),  # labels disagree → rejected (fail closed)
        ("Sahih", "  ", True),  # blank grade_ar falls back to the Indonesian label
        ("Hadis hasan", "", True),
        ("HASAN SAHIH", "", True),
        ("Sahih", "ضعيف", False),
        ("Daif", "  ", False),
        ("Hasan", "منكر", False),
        ("Hasan", "شاذ", False),
        ("Hasan", "باطل", False),
        ("Mungkar", "", False),
    ],
)
def test_grade_is_strong(grade: str, grade_ar: str, strong: bool) -> None:
    assert grade_is_strong(grade, grade_ar) is strong


@pytest.mark.parametrize("attribution_ar", ["", None, "رواه أحمد", "رواه الدارمي", "رويناه في كتاب الحجة بإسناد صحيح", "أخرجه البخاري"])
def test_unattributed_rejected(attribution_ar: str | None) -> None:
    with pytest.raises(HadithRejected, match="not attributed to an allowed collection"):
        admit(raw("3044", attribution_ar=attribution_ar), None)


def test_sahihain_needs_no_cross_check() -> None:
    assert admit(raw("3044"), None).collections == (Collection.BUKHARI,)


def test_sahihain_plus_sunan_keeps_only_sahihain(sunan: SunanIndex) -> None:
    h = admit(raw("3044", attribution_ar="رواه البخاري والترمذي"), sunan)
    assert h.collections == (Collection.BUKHARI,)
    assert h.reference == "HR. Bukhari"


def test_sunan_only_without_cross_check_data_rejected() -> None:
    with pytest.raises(HadithRejected, match="no grade cross-check data"):
        admit(raw("3350"), None)


def test_matn_not_found_rejected() -> None:
    with pytest.raises(HadithRejected, match="matn not found"):
        admit(raw("3350"), SunanIndex({Collection.ABUDAWUD: []}))
    with pytest.raises(HadithRejected, match="matn not found"):
        admit(raw("3350"), SunanIndex({Collection.TIRMIDHI: [{"text": raw("3350")["hadeeth_ar"], "grades": []}]}))


def test_short_matn_never_matches() -> None:
    text = "قال: «إنما الأعمال»"
    index = SunanIndex({Collection.ABUDAWUD: [{"text": "حدثنا فلان قال إنما الأعمال بالنيات", "grades": []}]})
    assert len(extract_matn(text)) < 20
    assert index.find(Collection.ABUDAWUD, extract_matn(text)) == []
    with pytest.raises(HadithRejected, match="matn not found"):
        admit(raw("3350", hadeeth_ar=text), index)


def test_missing_matn_never_matches(sunan: SunanIndex) -> None:
    with pytest.raises(HadithRejected, match="matn not found"):
        admit(raw("3350", hadeeth_ar=None), sunan)


def _index_with_grades(grades: list[dict[str, str]]) -> SunanIndex:
    return SunanIndex({Collection.ABUDAWUD: [{"text": "حدثنا " + extract_matn(raw("3350")["hadeeth_ar"]), "grades": grades}]})


@pytest.mark.parametrize("grade", ["Da'if", "Daʿif", "Da`if Isnaad", "Daif", "Dhaif", "Munkar", "Shadh", "Mawdu", "Maudu'", "Matruk", "Batil", "Ḍaʿīf"])
def test_grader_weak_rejected(grade: str) -> None:
    with pytest.raises(HadithRejected, match="a grader marks it weak in Abu Daud"):
        admit(raw("3350"), _index_with_grades([{"name": "Al-Albani", "grade": "Sahih"}, {"name": "Other", "grade": grade}]))


@pytest.mark.parametrize("grade", ["Maqtu", "Mauquf", "Muquf", "Mawquf Sahih"])
def test_not_prophetic_grader_rejected(grade: str) -> None:
    with pytest.raises(HadithRejected, match="a grader marks it weak"):
        admit(raw("3350"), _index_with_grades([{"name": "Zubair", "grade": grade}]))


def test_strong_graders_admitted() -> None:
    h = admit(raw("3350"), _index_with_grades([{"name": "Al-Albani", "grade": "Sahih"}, {"name": "Zubair", "grade": "Hasan"}]))
    assert h.collections == (Collection.ABUDAWUD,)


def test_weak_hit_in_any_collection_rejects_even_after_a_match() -> None:
    matn = extract_matn(raw("3567")["hadeeth_ar"])
    index = SunanIndex(
        {
            Collection.TIRMIDHI: [{"text": matn, "grades": [{"grade": "Hasan"}]}],
            Collection.IBNMAJAH: [{"text": matn, "grades": [{"grade": "Da'if"}]}],
        }
    )
    with pytest.raises(HadithRejected, match="weak in Ibnu Majah"):
        admit(raw("3567"), index)


@pytest.mark.parametrize(
    ("grades", "weak"),
    [
        ([], False),
        ([{"name": "x"}], False),
        ([{"grade": "Sahih"}, {"grade": "Hasan Sahih"}], False),
        ([{"grade": "Sahih"}, {"grade": "DA'IF"}], True),
        ([{"grade": "Daʿif"}], True),
        ([{"grade": "Ḍaʿīf"}], True),  # scholarly transliteration with dots and macrons
        ([{"grade": "Daʻīf Jiddan"}], True),
        ([{"grade": "Da’if"}], True),
        ([{"grade": "Mawḍūʿ"}], True),
        ([{"grade": "Maqṭūʿ"}], True),
        ([{"grade": "Mawqūf"}], True),
        ([{"grade": "ضعيف"}], True),
        ([{"grade": "إسناده ضَعِيف"}], True),
        ([{"grade": "صحيح"}], False),
        ([{"grade": "Ṣaḥīḥ"}], False),
        ([{"grade": "Mauquf"}], True),
    ],
)
def test_grader_marks_weak(grades: list[dict[str, str]], weak: bool) -> None:
    assert grader_marks_weak(grades) is weak


# ── Attribution parsing ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("متفق عليه", SAHIHAIN),
        ("مُتَّفَقٌ عَلَيْهِ", SAHIHAIN),
        ("رواه البخاري", (Collection.BUKHARI,)),
        ("رَوَاهُ البُخَارِيُّ", (Collection.BUKHARI,)),
        ("رواه مسلم والبخاري", (Collection.BUKHARI, Collection.MUSLIM)),
        ("رواه أبو داود", (Collection.ABUDAWUD,)),
        ("رواه أبو داود والترمذي والنسائي وابن ماجه", (Collection.ABUDAWUD, Collection.TIRMIDHI, Collection.NASAI, Collection.IBNMAJAH)),
        ("رواه النسائي", (Collection.NASAI,)),
        ("رواه النسائى", (Collection.NASAI,)),
        ("رواه الترمذي والنسائي في الكبرى وابن ماجه", (Collection.TIRMIDHI, Collection.NASAI, Collection.IBNMAJAH)),
        ("رواه الترمذي وابن ماجه والدارمي وأحمد", (Collection.TIRMIDHI, Collection.IBNMAJAH)),
        ("رواه أحمد", ()),
        ("رواه مسلمة", ()),  # a different name sharing a prefix
        ("رويناه في كتاب الحجة", ()),
        ("البخاري", ()),  # no رواه clause
        ("", ()),
    ],
)
def test_parse_attribution(text: str, expected: tuple[Collection, ...]) -> None:
    assert parse_attribution(text) == expected


def test_norm_ar_folds_hamza_and_punctuation() -> None:
    assert _norm_ar("أَبُو دَاوُدَ") == "ابو داود"
    assert _norm_ar("إِسْلَام، آمَنَ ٱللَّه") == "اسلام امن الله"
    assert _norm_ar("صلاة مُصْطَفَى شيء مسؤول") == "صلاه مصطفي شيء مسوول"
    assert _norm_ar("«hello» 123  الله\n") == "الله"


def test_extract_matn() -> None:
    assert extract_matn("قال: «أَوَّلُ» ثم «مَا يُقْضَى»") == "اول ما يقضي"
    assert extract_matn("من خرج في طلب العلم") == "من خرج في طلب العلم"


def test_sunan_index_find() -> None:
    index = SunanIndex({Collection.TIRMIDHI: [{"text": "حدثنا قال من خرج في طلب العلم فهو في سبيل الله"}, {}]})
    matn = _norm_ar("من خرج في طلب العلم فهو")
    assert index.find(Collection.TIRMIDHI, matn) == [index.hadiths[Collection.TIRMIDHI][0]]
    assert index.find(Collection.NASAI, matn) == []
    assert index.find(Collection.TIRMIDHI, "من خرج") == []


# ── Reference formatting ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("collections", "reference"),
    [
        ((Collection.NASAI,), "HR. Nasa'i"),
        ((Collection.BUKHARI, Collection.MUSLIM), "HR. Bukhari dan Muslim"),
        ((Collection.ABUDAWUD, Collection.TIRMIDHI, Collection.IBNMAJAH), "HR. Abu Daud, Tirmidzi dan Ibnu Majah"),
    ],
)
def test_reference(collections: tuple[Collection, ...], reference: str) -> None:
    h = Hadith("1", "t", "teks", "نص", "رواه", "Sahih", "صحيح", collections)
    assert h.reference == reference


def test_display_names_cover_every_collection() -> None:
    assert set(DISPLAY) == set(Collection)
    assert set(SAHIHAIN) | set(SUNAN) == set(Collection)


# ── Cache ────────────────────────────────────────────────────────────────────────────────────


def test_cache_put_strips_syarah(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    assert cache.ids() == []
    assert cache.get_raw("3044") is None
    assert cache.oldest_fetch() is None
    assert cache.sunan_index() is None
    row = raw("3044")
    assert {"explanation", "explanation_ar", "hints", "hints_ar", "words_meanings_ar"} <= set(row)
    cache.put_raw(row, NOW)
    stored = cache.get_raw("3044")
    assert stored is not None
    assert not [k for k in stored if k.startswith(("explanation", "hints", "words_meanings"))]
    assert stored["_fetched_at"] == NOW.isoformat()
    assert stored["hadeeth"] == row["hadeeth"] and stored["hadeeth_ar"] == row["hadeeth_ar"]
    assert "explanation" in row  # the input is not mutated
    assert cache.ids() == ["3044"]


def test_cache_ids_and_oldest_fetch(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    cache.put_raw(raw("3342"), NOW)
    cache.put_raw(raw("2962"), NOW - timedelta(days=3))
    (tmp_path / "hadeethenc" / "9999.json").write_text(json.dumps({"id": "9999"}), "utf-8")  # no stamp
    assert cache.ids() == ["2962", "3342", "9999"]
    assert cache.oldest_fetch() == NOW - timedelta(days=3)


def test_cache_oldest_fetch_without_stamps(hadith_dir: Path) -> None:
    assert HadithCache(hadith_dir).ids() == ["2962", "3044", "3342", "3350", "3567", "4191", "5344", "66535"]
    assert HadithCache(hadith_dir).oldest_fetch() is None


def test_cache_editions_and_sunan_index(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    (tmp_path / "fawaz").mkdir()
    assert cache.sunan_index() is None
    hadiths = json.loads((FAWAZ / "abudawud.json").read_text("utf-8"))
    cache.put_edition(Collection.ABUDAWUD, {"metadata": {}, "hadiths": hadiths})
    index = cache.sunan_index()
    assert index is not None
    assert set(index.hadiths) == {Collection.ABUDAWUD}
    assert index.hadiths[Collection.ABUDAWUD] == hadiths


def test_real_fixture_sunan_index(sunan: SunanIndex) -> None:
    assert set(sunan.hadiths) == set(SUNAN)
    assert sunan.hadiths[Collection.NASAI] == []


# ── Library ──────────────────────────────────────────────────────────────────────────────────


def test_library_caches_admissions(tmp_path: Path) -> None:
    root = tmp_path / "h"
    shutil.copytree(FIXTURES / "hadith", root)
    lib = HadithLibrary(HadithCache(root))
    first = lib.get("3044")
    (root / "hadeethenc" / "3044.json").unlink()
    assert lib.get("3044") is first
    assert lib.rejections == {}


def test_library_unknown_id(library: HadithLibrary) -> None:
    with pytest.raises(HadithRejected, match="not in the HadeethEnc snapshot"):
        library.get("123456")
    assert "123456" not in library.rejections


def test_library_without_fawaz_rejects_sunan_only(tmp_path: Path) -> None:
    shutil.copytree(HADEETHENC, tmp_path / "hadeethenc")
    lib = HadithLibrary(HadithCache(tmp_path))
    assert [h.id for h in lib.candidates()] == ["2962", "3044", "3342"]
    assert "no grade cross-check data" in lib.rejections["3350"]
    assert "no grade cross-check data" in lib.rejections["3567"]


def test_candidates(library: HadithLibrary) -> None:
    assert [h.id for h in library.candidates()] == ["2962", "3044", "3342", "3350", "3567"]
    assert set(library.rejections) == {"4191", "5344", "66535"}
    assert [h.id for h in library.candidates(limit=2)] == ["2962", "3044"]
    assert [h.id for h in library.candidates(limit=100)] == ["2962", "3044", "3342", "3350", "3567"]


def test_candidates_limit_skips_rejections(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    for hid in ("4191", "5344", "66535", "3044"):
        cache.put_raw(raw(hid), NOW)
    lib = HadithLibrary(cache)
    assert [h.id for h in lib.candidates(limit=1)] == ["3044"]


def test_candidates_dedupe_by_id(library: HadithLibrary) -> None:
    ids = [h.id for h in library.candidates()]
    assert len(ids) == len(set(ids))


# ── Ingest ───────────────────────────────────────────────────────────────────────────────────


def _row(hid: str) -> dict[str, Any]:
    return raw("3044", id=hid, title=f"t{hid}")


class FakeAPI:
    """HadeethEnc + fawazahmed0 behind httpx.MockTransport. Failures are injected per route."""

    def __init__(self, lists: dict[tuple[str, int], list[str]], fail_ids: dict[str, httpx.Response] | None = None):
        self.lists = lists
        self.fail_ids = fail_ids or {}
        self.fawaz: dict[str, httpx.Response] = {}
        self.list_failure: httpx.Response | None = None
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        url = str(req.url)
        if url.startswith(f"{BASE}/hadeeths/list/"):
            assert req.url.params["language"] == "id"
            if self.list_failure is not None:
                return self.list_failure
            key = (req.url.params["category_id"], int(req.url.params["page"]))
            return httpx.Response(200, json={"data": [{"id": i} for i in self.lists.get(key, [])]})
        if url.startswith(f"{BASE}/hadeeths/one/"):
            hid = req.url.params["id"]
            if hid in self.fail_ids:
                return self.fail_ids[hid]
            return httpx.Response(200, json=_row(hid))
        if url.startswith(f"{FAWAZ_BASE}/editions/"):
            name = req.url.path.rsplit("/", 1)[1]
            if name in self.fawaz:
                return self.fawaz[name]
            c = name.removeprefix("ara-").removesuffix(".min.json")
            return httpx.Response(200, json={"hadiths": [{"hadithnumber": 1, "text": f"نص {c}", "grades": []}]})
        return httpx.Response(404)


def _run(api: FakeAPI, cache: HadithCache, now: Callable[[], datetime] = lambda: NOW, **kw: Any) -> tuple[IngestReport, list[float]]:
    sleeps: list[float] = []
    with httpx.Client(transport=httpx.MockTransport(api)) as client:
        report = ingest(client, cache, BASE, FAWAZ_BASE, now=now, sleep=sleeps.append, **kw)
    return report, sleeps


def test_ingest_success(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    cache.put_raw(raw("3044"), NOW - timedelta(days=29))  # cached and fresh: not refetched
    api = FakeAPI({("1", 1): ["10", "11"], ("1", 2): ["12", "3044"], ("2", 1): ["11"], ("2", 2): ["99"]})
    report, sleeps = _run(api, cache, categories=("1", "2"), per_page=2, pages=2)
    assert report.as_dict() == {"fetched": 3, "failed": 0, "api_down": False, "stale_days": None, "errors": []}
    assert sleeps == [0.2, 0.2, 0.2]
    assert cache.ids() == ["10", "11", "12", "3044"]
    stored = cache.get_raw("10")
    assert stored is not None and stored["_fetched_at"] == NOW.isoformat() and "explanation" not in stored
    # A full page asks for the next; a short page stops; ("2", 2) is never requested.
    pages = [(r.url.params["category_id"], r.url.params["page"]) for r in api.requests if "/list/" in str(r.url)]
    assert pages == [("1", "1"), ("1", "2"), ("2", "1")]
    one = [r.url.params["id"] for r in api.requests if "/one/" in str(r.url)]
    assert one == ["10", "11", "12"]  # deduped across categories
    index = cache.sunan_index()
    assert index is not None and set(index.hadiths) == set(SUNAN)


def test_ingest_defaults(tmp_path: Path) -> None:
    api = FakeAPI({(c, 1): [] for c in DEFAULT_CATEGORIES})
    with httpx.Client(transport=httpx.MockTransport(api)) as client:
        report = ingest(client, HadithCache(tmp_path), BASE, FAWAZ_BASE)
    assert report.fetched == 0 and not report.api_down
    assert {r.url.params["category_id"] for r in api.requests if "/list/" in str(r.url)} == set(DEFAULT_CATEGORIES)
    assert {r.url.params["per_page"] for r in api.requests if "/list/" in str(r.url)} == {"100"}


def test_ingest_default_clock(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    api = FakeAPI({("1", 1): ["7"]})
    before = datetime.now(UTC)
    with httpx.Client(transport=httpx.MockTransport(api)) as client:
        ingest(client, cache, BASE, FAWAZ_BASE, categories=("1",), sleep=lambda s: None)
    oldest = cache.oldest_fetch()
    assert oldest is not None and before <= oldest <= datetime.now(UTC)


def test_ingest_per_id_failures_are_counted(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    api = FakeAPI(
        {("1", 1): ["1", "2", "3", "4"]},
        fail_ids={"2": httpx.Response(500, json={}), "3": httpx.Response(200, content=b"<html>not json")},
    )
    report, sleeps = _run(api, cache, categories=("1",))
    assert (report.fetched, report.failed, report.api_down) == (2, 2, False)
    assert len(sleeps) == 4
    assert report.errors[0].startswith("2: ") and report.errors[1].startswith("3: ")
    assert cache.ids() == ["1", "4"]


def test_ingest_api_down_with_stale_cache(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    cache.put_raw(raw("3044"), NOW - timedelta(days=45))
    cache.put_raw(raw("3342"), NOW - timedelta(days=2))
    api = FakeAPI({})
    api.list_failure = httpx.Response(503)
    report, _ = _run(api, cache)
    assert report.api_down
    assert report.stale_days == 45
    assert report.errors[0].startswith("HadeethEnc unavailable")
    assert cache.ids() == ["3044", "3342"]  # the snapshot is kept and still used
    assert HadithLibrary(cache).get("3044").reference == "HR. Bukhari"


@pytest.mark.parametrize(("age", "stale"), [(STALE_AFTER, None), (STALE_AFTER + timedelta(seconds=1), 30), (timedelta(days=1), None)])
def test_ingest_api_down_staleness_boundary(tmp_path: Path, age: timedelta, stale: int | None) -> None:
    cache = HadithCache(tmp_path)
    cache.put_raw(raw("3044"), NOW - age)
    api = FakeAPI({})
    api.list_failure = httpx.Response(500)
    report, _ = _run(api, cache)
    assert report.api_down and report.stale_days == stale


def test_ingest_api_down_with_empty_cache(tmp_path: Path) -> None:
    def boom(req: httpx.Request) -> httpx.Response:
        if "hadeethenc" in str(req.url):
            raise httpx.ConnectError("unreachable", request=req)
        return httpx.Response(200, json={"hadiths": []})

    with httpx.Client(transport=httpx.MockTransport(boom)) as client:
        report = ingest(client, HadithCache(tmp_path), BASE, FAWAZ_BASE, now=lambda: NOW, sleep=lambda s: None)
    assert report.api_down and report.stale_days is None


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"data": [{"no_id": 1}]}),
    ],
)
def test_ingest_malformed_list_is_api_down(tmp_path: Path, response: httpx.Response) -> None:
    cache = HadithCache(tmp_path)
    cache.put_raw(raw("3044"), NOW - timedelta(days=31))
    api = FakeAPI({})
    api.list_failure = response
    report, _ = _run(api, cache, categories=("1",))
    assert report.api_down and report.stale_days == 31


def test_ingest_api_fresh_is_not_stale(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    cache.put_raw(raw("3044"), NOW - timedelta(days=90))
    report, _ = _run(FakeAPI({}), cache)
    assert not report.api_down and report.stale_days is None  # old but the API is up: no alert


def test_ingest_fawaz_failures_recorded(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    cache.put_edition(Collection.ABUDAWUD, {"hadiths": [{"text": "قديم"}]})
    api = FakeAPI({})
    api.fawaz = {
        "ara-abudawud.min.json": httpx.Response(404),
        "ara-tirmidhi.min.json": httpx.Response(200, content=b"{oops"),
        "ara-nasai.min.json": httpx.Response(200, json={"metadata": {}}),
    }
    report, _ = _run(api, cache)
    assert not report.api_down
    assert [e.split(":")[0] for e in report.errors] == ["fawazahmed0 abudawud", "fawazahmed0 tirmidhi", "fawazahmed0 nasai"]
    index = cache.sunan_index()
    assert index is not None
    assert index.hadiths[Collection.ABUDAWUD] == [{"text": "قديم"}]  # previous copy kept
    assert set(index.hadiths) == {Collection.ABUDAWUD, Collection.IBNMAJAH}


def test_ingest_refetches_rows_older_than_30_days(tmp_path: Path) -> None:
    cache = HadithCache(tmp_path)
    cache.put_raw(raw("3044"), NOW - timedelta(days=31))
    no_stamp = dict(raw("3342"))
    (tmp_path / "hadeethenc").mkdir(exist_ok=True)
    (tmp_path / "hadeethenc" / "3342.json").write_text(json.dumps(no_stamp), "utf-8")  # legacy row without _fetched_at
    api = FakeAPI({("1", 1): ["3044", "3342"]})
    report, _ = _run(api, cache, categories=("1",), per_page=5, pages=1)
    assert report.fetched == 2
    stored = cache.get_raw("3044")
    assert stored is not None and stored["_fetched_at"] == NOW.isoformat()
