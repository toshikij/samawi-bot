"""checks/draft_checks.py: every deterministic check a draft must pass (spec §6.3 step 2, §10.3).

Each test starts from tests/fixtures/good_draft.json (which passes every check), changes one thing,
and asserts the exact list of errors, so a check that fires for the wrong reason or with the wrong
number is caught too.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta
from typing import Any

import pytest

from conftest import TODAY, load_draft
from samawi_bot.brain.models import Draft, SourceRef
from samawi_bot.checks import draft_checks as DC
from samawi_bot.checks.draft_checks import (
    DATED_FACTS,
    FOLLOW_HANDLE,
    CheckContext,
    all_copy,
    check_draft,
    resolve_source,
    uses,
)
from samawi_bot.checks.rules import TooEasy, topic_hash
from samawi_bot.config import LaunchMode
from samawi_bot.sources import Sources
from samawi_bot.sources.duas import DuaUnavailable
from samawi_bot.sources.features_reader import FeatureCatalog, parse_features
from samawi_bot.sources.hadith import HadithRejected
from samawi_bot.sources.quran import InvalidReference
from samawi_bot.sources.snapshot import FEATURES
from samawi_bot.verify.facts import UnknownFact

GOOD = load_draft()
CAP = GOOD.caption


def mkctx(**kw: Any) -> CheckContext:
    kw.setdefault("today", TODAY)
    kw.setdefault("launch_mode", LaunchMode.PRELAUNCH)
    return CheckContext(**kw)


def errors(sources: Sources, ctx: CheckContext | None = None, **overrides: Any) -> list[str]:
    return check_draft(load_draft(**overrides), sources, ctx or mkctx()).errors


def caption_plus(text: str) -> str:
    return f"{CAP}\n\n{text}"


def slides_json() -> list[dict[str, Any]]:
    return [s.model_dump() for s in GOOD.slides]


def claims_json() -> list[dict[str, Any]]:
    return [c.model_dump() for c in GOOD.claims]


def hooks_with(text: str, i: int = 0) -> list[str]:
    hooks = list(GOOD.hooks)
    hooks[i] = text
    return hooks


def claim(type_: str, ref: str, text: str = "Klaim") -> dict[str, Any]:
    return {"text": text, "source": {"type": type_, "ref": ref}}


def padded(base: str, n: int, fill: str = "x") -> str:
    out = base + " " + fill * (n - len(base) - 1)
    assert len(out) == n
    return out


# ── the fixture itself ────────────────────────────────────────────────────────


def test_good_draft_passes(sources: Sources, draft: Draft, ctx: CheckContext) -> None:
    report = check_draft(draft, sources, ctx)
    assert report.errors == []
    assert report.ok
    # The post's own date (26 November 2026) may be named too.
    assert report.allowed_numbers == {"1", "2", "5", "26", "128", "282", "2026", "2:282"}


def test_good_draft_passes_live(sources: Sources) -> None:
    assert errors(sources, mkctx(launch_mode=LaunchMode.LIVE)) == []


def test_constants() -> None:
    assert FOLLOW_HANDLE == "@samawi.indonesia"
    assert frozenset({"prayer", "compare", "extremes", "hijri"}) == DATED_FACTS


# ── resolve_source / all_copy / uses ──────────────────────────────────────────


def test_resolve_source_ayat(sources: Sources) -> None:
    text, fact = resolve_source(sources, SourceRef(type="ayat", ref="2:282"), TODAY)
    assert fact is None
    assert text.startswith("(QS. Al-Baqarah: 282) Wahai orang-orang yang beriman, apabila kamu berutang piutang")
    assert text == f"(QS. Al-Baqarah: 282) {sources.quran.translation(sources.quran.all_refs()[281 + 7])}"


def test_resolve_source_ayat_range_joins_translations(sources: Sources) -> None:
    from samawi_bot.sources.quran import AyahRef

    text, fact = resolve_source(sources, SourceRef(type="ayat", ref="112:1-2"), TODAY)
    assert fact is None
    t1, t2 = sources.quran.translation(AyahRef(112, 1)), sources.quran.translation(AyahRef(112, 2))
    assert text == f"(QS. Al-Ikhlas: 1\u20132) {t1} {t2}"


def test_resolve_source_hadith(sources: Sources) -> None:
    text, fact = resolve_source(sources, SourceRef(type="hadith", ref="2962"), TODAY)
    h = sources.hadith.get("2962")
    assert fact is None
    assert text == f"(HR. Bukhari dan Muslim) {h.text_id}"


def test_resolve_source_dua(sources: Sources) -> None:
    text, fact = resolve_source(sources, SourceRef(type="dua", ref="harian.bangun.1"), TODAY)
    d = sources.duas.get("harian.bangun.1")
    assert fact is None
    assert text == f"(HR. Bukhari 6312) {d.indonesian}"


def test_resolve_source_fact(sources: Sources) -> None:
    text, fact = resolve_source(sources, SourceRef(type="fact", ref="quran:longest_ayah"), TODAY)
    assert fact is not None and fact.key == "quran:longest_ayah"
    assert text == "Ayat terpanjang (jumlah kata): Al-Baqarah ayat 282: 282, 128 2:282"


def test_resolve_source_fact_uses_today(sources: Sources) -> None:
    text, fact = resolve_source(sources, SourceRef(type="fact", ref="quran:khatam_per_prayer:30"), date(2000, 1, 1))
    assert text == "Halaman per salat agar khatam dalam 30 hari: 604, 30, 5, 4, 4,03 604 ÷ 30 ÷ 5 = 4.03"
    assert fact is not None


@pytest.mark.parametrize(
    ("ref", "exc"),
    [
        (SourceRef(type="ayat", ref="2:287"), InvalidReference),
        (SourceRef(type="hadith", ref="4191"), HadithRejected),
        (SourceRef(type="dua", ref="no.such.dua"), DuaUnavailable),
        (SourceRef(type="fact", ref="quran:nope"), UnknownFact),
    ],
)
def test_resolve_source_raises(sources: Sources, ref: SourceRef, exc: type[Exception]) -> None:
    with pytest.raises(exc):
        resolve_source(sources, ref, TODAY)


def test_all_copy_lists_every_published_piece() -> None:
    d = load_draft(
        share_prompt="",
        slides=[
            {
                "template": "comparison",
                "heading": "H",
                "body": "",
                "items": ["a", "b"],
                "left_label": "L",
                "left_value": "1",
                "right_label": "R",
                "right_value": "2",
            }
        ],
    )
    where = [w for w, _ in all_copy(d)]
    assert where == [
        "hook 1",
        "hook 2",
        "hook 3",
        "hook 4",
        "hook 5",
        "slide 2",
        "slide 2",
        "slide 2",
        "slide 2",
        "slide 2",
        "slide 2",
        "slide 2",
        "takeaway",
        "bridge",
        "teaser",
        "caption",
        "threads subuh",
        "threads malam",
        "thread 1",
        "thread 2",
        "thread 3",
        "claim 1",
        "claim 2",
    ]
    texts = [t for _, t in all_copy(d)]
    assert texts[5:12] == ["H", "a", "b", "L", "1", "R", "2"]
    assert texts[12] == d.takeaway
    assert texts[-2:] == [c.text for c in d.claims]


def test_all_copy_good_draft_slide_numbers() -> None:
    copy = all_copy(GOOD)
    assert ("slide 2", "282") in copy
    assert ("slide 6", "Catat utangmu hari ini") in copy
    assert ("share prompt", GOOD.share_prompt) in copy
    assert ("thread 1", GOOD.thread[0]) in copy


def test_uses_yields_claims_then_slides() -> None:
    assert list(uses(GOOD)) == [
        SourceRef(type="fact", ref="quran:longest_ayah"),
        SourceRef(type="ayat", ref="2:282"),
        SourceRef(type="ayat", ref="2:282"),
        SourceRef(type="hadith", ref="2962"),
    ]


# ── sources ───────────────────────────────────────────────────────────────────


def test_no_claims(sources: Sources) -> None:
    # Without claims, 128 (word count from the longest_ayah fact) is no longer backed either.
    assert errors(sources, claims=[]) == [
        "draft has no claims; every factual statement needs a source",
        "hook 4: number 128 is not backed by any cited source or computation",
    ]


def test_unresolvable_ayat(sources: Sources) -> None:
    assert errors(sources, claims=[*claims_json(), claim("ayat", "2:287")]) == ["source ayat:2:287 does not resolve: 2:287 is not an ayah"]


def test_malformed_ayat(sources: Sources) -> None:
    assert errors(sources, claims=[*claims_json(), claim("ayat", "2:255a")]) == ["source ayat:2:255a does not resolve: Malformed reference '2:255a'"]


def test_rejected_hadith(sources: Sources) -> None:
    assert errors(sources, claims=[*claims_json(), claim("hadith", "4191")]) == [
        "source hadith:4191 does not resolve: 4191: a grader marks it weak in Tirmidzi"
    ]


def test_missing_hadith(sources: Sources) -> None:
    assert errors(sources, claims=[*claims_json(), claim("hadith", "999999")]) == [
        "source hadith:999999 does not resolve: 999999: not in the HadeethEnc snapshot"
    ]


def test_unknown_dua(sources: Sources) -> None:
    assert errors(sources, claims=[*claims_json(), claim("dua", "no.such")]) == ["source dua:no.such does not resolve: \"No dua 'no.such'\""]


def test_unknown_fact(sources: Sources) -> None:
    assert errors(sources, claims=[*claims_json(), claim("fact", "quran:nope")]) == ["source fact:quran:nope does not resolve: 'quran:nope'"]


def test_every_failing_source_is_reported(sources: Sources) -> None:
    assert errors(sources, claims=[*claims_json(), claim("ayat", "2:287"), claim("fact", "quran:nope")]) == [
        "source ayat:2:287 does not resolve: 2:287 is not an ayah",
        "source fact:quran:nope does not resolve: 'quran:nope'",
    ]


def test_failed_source_contributes_no_numbers(sources: Sources) -> None:
    # 2:287 does not exist; its numbers must not become "allowed".
    assert errors(sources, claims=[*claims_json(), claim("ayat", "2:287")], caption=caption_plus("Lihat ayat 287.")) == [
        "source ayat:2:287 does not resolve: 2:287 is not an ayah",
        "caption: number 287 is not backed by any cited source or computation",
    ]


def test_bad_slide_source_is_checked(sources: Sources) -> None:
    slides = slides_json()
    slides[3]["source"] = {"type": "hadith", "ref": "5344"}
    assert errors(sources, slides=slides) == ["source hadith:5344 does not resolve: 5344: a grader marks it weak in Ibnu Majah"]


def test_extra_sources_are_checked(sources: Sources) -> None:
    ctx = mkctx(extra_sources=[SourceRef(type="ayat", ref="115:1")])
    assert errors(sources, ctx) == ["source ayat:115:1 does not resolve: 115:1 is not an ayah"]


@pytest.mark.parametrize("template", ["ayat", "hadith", "dua"])
def test_scripture_slide_without_source(sources: Sources, template: str) -> None:
    slides = slides_json()
    slides.insert(0, {"template": template, "heading": "Teks"})
    assert errors(sources, slides=slides) == [f"{template} slide must carry a {template} source; the renderer pulls the text from it"]


def test_scripture_slide_with_wrong_source_type(sources: Sources) -> None:
    slides = slides_json()
    slides[1]["source"] = {"type": "hadith", "ref": "2962"}  # an ayat slide carrying a hadith
    assert errors(sources, slides=slides) == ["ayat slide must carry a ayat source; the renderer pulls the text from it"]


def test_dua_slide_with_dua_source_passes(sources: Sources) -> None:
    slides = slides_json()
    slides[3] = {"template": "dua", "heading": "Doa bangun tidur", "source": {"type": "dua", "ref": "harian.bangun.1"}}
    assert errors(sources, slides=slides) == []


def test_fact_slide_needs_no_source(sources: Sources) -> None:
    slides = slides_json()
    slides[0]["source"] = None
    assert errors(sources, slides=slides) == []


# ── numbers ───────────────────────────────────────────────────────────────────


def test_off_by_one_number(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("Ayat 283 juga panjang.")) == ["caption: number 283 is not backed by any cited source or computation"]


def test_every_location_is_number_checked(sources: Sources) -> None:
    d = GOOD.model_dump()
    d["takeaway"] = "Catat 3 hal."
    d["thread"] = [*GOOD.thread[:2], GOOD.thread[2] + " 7"]
    d["claims"][0]["text"] += " 9"
    assert errors(sources, **{k: d[k] for k in ("takeaway", "thread", "claims")}) == [
        "takeaway: number 3 is not backed by any cited source or computation",
        "thread 3: number 7 is not backed by any cited source or computation",
        "claim 1: number 9 is not backed by any cited source or computation",
    ]


def test_rounding_mismatch(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "quran:khatam_per_prayer:30")]
    assert errors(sources, claims=claims, caption=caption_plus("Cukup 4,03 halaman per salat, sekitar 4.")) == []
    assert errors(sources, claims=claims, caption=caption_plus("Cukup 4.03 halaman per salat.")) == []
    assert errors(sources, claims=claims, caption=caption_plus("Cukup 4,1 halaman per salat.")) == [
        "caption: number 4.1 is not backed by any cited source or computation"
    ]
    assert errors(sources, claims=claims, caption=caption_plus("Cukup 4,0 halaman per salat.")) == [
        "caption: number 4.0 is not backed by any cited source or computation"
    ]


def test_indonesian_thousands(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "quran:totals")]
    assert errors(sources, claims=claims, caption=caption_plus("Ada 114 surah dan 6.236 ayat.")) == []
    assert errors(sources, claims=claims, caption=caption_plus("Ada 6236 ayat.")) == []
    assert errors(sources, claims=claims, caption=caption_plus("Ada 6.237 ayat.")) == ["caption: number 6237 is not backed by any cited source or computation"]
    assert errors(sources, claims=claims, caption=caption_plus("Ada 115 surah.")) == ["caption: number 115 is not backed by any cited source or computation"]


def test_times(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "prayer:Jakarta:2026-11-26:Subuh")]
    assert errors(sources, claims=claims, caption=caption_plus("Subuh Jakarta 04:05 WIB.")) == []
    assert errors(sources, claims=claims, caption=caption_plus("Subuh Jakarta 04:06 WIB.")) == [
        "caption: number 04:06 is not backed by any cited source or computation"
    ]
    assert errors(sources, claims=claims, caption=caption_plus("Subuh Jakarta 4:05 WIB.")) == [
        "caption: number 4:05 is not backed by any cited source or computation"
    ]


def test_fact_detail_numbers_allowed(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "compare:Sabang:Merauke:2026-11-26:Maghrib")]
    text = "Maghrib Sabang 18:22 WIB, Merauke 17:44 WIT: selisih 158 menit, 2 jam 38 menit."
    assert errors(sources, claims=claims, caption=caption_plus(text)) == []
    assert errors(sources, claims=claims, caption=caption_plus("selisih 159 menit")) == ["caption: number 159 is not backed by any cited source or computation"]


def test_ayat_range_numbers(sources: Sources) -> None:
    claims = [*claims_json(), claim("ayat", "18:1-5")]
    assert errors(sources, claims=claims, caption=caption_plus("Al-Kahf (18) ayat 1 sampai 5.")) == []
    # Ayat inside the range are not individually admitted unless the translation states them.
    assert errors(sources, claims=claims, caption=caption_plus("Al-Kahf ayat 3.")) == ["caption: number 3 is not backed by any cited source or computation"]


def test_numbers_in_ayat_translation_allowed(sources: Sources, monkeypatch: pytest.MonkeyPatch) -> None:
    claims = [*claims_json(), claim("ayat", "112:1")]
    assert errors(sources, claims=claims, caption=caption_plus("Ada 40 hari.")) == ["caption: number 40 is not backed by any cited source or computation"]
    real = sources.quran.translation
    monkeypatch.setattr(sources.quran, "translation", lambda r: real(r) + " (40 hari)" if r.surah == 112 else real(r))
    assert errors(sources, claims=claims, caption=caption_plus("Ada 40 hari.")) == []


def test_real_translation_number_allowed(sources: Sources) -> None:
    # 37:100's Kemenag translation text starts with "100."; numbers from the text itself count.
    claims = [*claims_json(), claim("ayat", "37:100")]
    assert errors(sources, claims=claims, caption=caption_plus("Doa Nabi Ibrahim di ayat 100 surah 37.")) == []


def test_numbers_in_source_reference_allowed(sources: Sources) -> None:
    claims = [*claims_json(), claim("dua", "harian.bangun.1")]
    assert errors(sources, claims=claims, caption=caption_plus("Diriwayatkan Bukhari (6312).")) == []
    assert errors(sources, caption=caption_plus("Diriwayatkan Bukhari (6312).")) == ["caption: number 6312 is not backed by any cited source or computation"]


def test_series_numbers_allowed(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("Seri bagian 1 dari 5.")) == []
    assert errors(sources, series=None, caption=caption_plus("Seri bagian 1 dari 5.")) == [
        "caption: number 1 is not backed by any cited source or computation",
        "caption: number 5 is not backed by any cited source or computation",
    ]
    series = {"id": "s", "title": "Seri", "part": 3, "total": 7}
    assert errors(sources, series=series, caption=caption_plus("Bagian 3/7.")) == []


@pytest.mark.parametrize(("part", "total", "ok"), [(1, 5, True), (5, 5, True), (0, 5, False), (6, 5, False), (-1, 5, False), (1, 0, False)])
def test_series_range(sources: Sources, part: int, total: int, ok: bool) -> None:
    series = {"id": "s", "title": "Seri", "part": part, "total": total}
    errs = errors(sources, series=series)
    assert errs == ([] if ok else [f"series part {part}/{total} is out of range"])


def test_extra_sources_numbers_allowed(sources: Sources) -> None:
    caption = caption_plus("Al-Qur'an punya 6.236 ayat.")
    assert errors(sources, caption=caption) == ["caption: number 6236 is not backed by any cited source or computation"]
    ctx = mkctx(extra_sources=[SourceRef(type="fact", ref="quran:totals")])
    assert errors(sources, ctx, caption=caption) == []


def test_allowed_numbers_reported(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "const:zakat_rate_pct")]
    report = check_draft(load_draft(claims=claims, caption=caption_plus("Zakat 2,5%.")), sources, mkctx())
    assert report.errors == []
    assert report.allowed_numbers == {"1", "2", "5", "26", "128", "282", "2026", "2:282", "2.5"}


# ── notes (isbat) ─────────────────────────────────────────────────────────────

RAMADAN_KEY = "hijri:days_to_ramadan:2026-11-26"


def test_days_to_ramadan_requires_isbat_wording(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", RAMADAN_KEY)]
    assert errors(sources, claims=claims, caption=caption_plus("74 hari lagi menuju Ramadan 1448 H.")) == [
        f"{RAMADAN_KEY} requires the wording 'perkiraan, menunggu sidang isbat'"
    ]


def test_days_to_ramadan_with_wording(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", RAMADAN_KEY)]
    assert errors(sources, claims=claims, caption=caption_plus("74 hari lagi menuju Ramadan (perkiraan, menunggu sidang isbat).")) == []
    assert errors(sources, claims=claims, caption=caption_plus("74 hari lagi (Perkiraan, Menunggu Sidang Isbat).")) == []
    assert errors(sources, claims=claims, takeaway="Perkiraan, menunggu sidang isbat.") == []


def test_days_to_ramadan_sidang_isbat_alone_is_not_enough(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", RAMADAN_KEY)]
    assert errors(sources, claims=claims, caption=caption_plus("74 hari lagi, tanggal pastinya lewat Sidang Isbat.")) == [
        f"{RAMADAN_KEY} requires the wording 'perkiraan, menunggu sidang isbat'"
    ]
    # Both parts in the writer's own words is fine.
    assert errors(sources, claims=claims, caption=caption_plus("Perkiraan 74 hari lagi; tanggal pastinya lewat sidang isbat.")) == []


def test_note_only_for_facts_with_notes(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "hijri:today:2026-11-26")]  # 16 Jumadil Akhir: not near a boundary
    assert errors(sources, claims=claims, caption=caption_plus("Hari ke-16 Jumadil Akhir 1448.")) == []
    claims = [*claims_json(), claim("fact", "hijri:today:2026-12-08")]  # 28 of a 29-day month
    assert errors(sources, claims=claims) == ["hijri:today:2026-12-08 requires the wording 'perkiraan, menunggu sidang isbat'"]


# ── bridge ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("feature", "msg"),
    [
        ("customAdzanVoices", "bridge feature 'customAdzanVoices' has status planned, not shipped"),
        ("offlineMurottal", "bridge feature 'offlineMurottal' has status blocked, not shipped"),
        ("quranAudio", "bridge feature 'quranAudio' is not in samawi_features.dart"),
        ("referralRewards", "bridge feature 'referralRewards' has no public name yet"),
    ],
)
def test_bridge_not_bridgeable(sources: Sources, feature: str, msg: str) -> None:
    assert errors(sources, bridge_feature=feature) == [msg]


def test_bridge_renamed_in_app(sources: Sources) -> None:
    src = sources.snapshot.read_text(FEATURES).replace("  tafsir(FeatureTier.paid),", "  tafsirLengkap(FeatureTier.paid),")
    assert "tafsirLengkap" in src
    renamed = dataclasses.replace(sources, features=FeatureCatalog(parse_features(src)))
    assert errors(renamed) == ["bridge feature 'tafsir' is not in samawi_features.dart"]


def test_bridge_deleted_from_app(sources: Sources) -> None:
    src = sources.snapshot.read_text(FEATURES).replace("  tafsir(FeatureTier.paid),", "")
    deleted = dataclasses.replace(sources, features=FeatureCatalog(parse_features(src)))
    assert errors(deleted) == ["bridge feature 'tafsir' is not in samawi_features.dart"]


@pytest.mark.parametrize(
    ("recent", "ok"),
    [
        ([], True),
        (["tafsir"], True),
        (["tafsir", "qibla"], True),
        (["qibla", "tafsir"], True),
        (["qibla", "tafsir", "tafsir"], True),
        (["tafsir", "tafsir"], False),
        (["tafsir", "tafsir", "qibla"], False),
    ],
)
def test_bridge_three_days_running(sources: Sources, recent: list[str], ok: bool) -> None:
    errs = errors(sources, mkctx(recent_bridges=recent))
    assert errs == ([] if ok else ["bridge 'tafsir' would appear 3 days in a row"])


def test_bridge_unshipped_reported_once_even_if_repeated(sources: Sources) -> None:
    errs = errors(sources, mkctx(recent_bridges=["offlineMurottal"] * 2), bridge_feature="offlineMurottal")
    assert errs == ["bridge feature 'offlineMurottal' has status blocked, not shipped"]


def test_bridge_line_must_name_feature(sources: Sources) -> None:
    assert errors(sources, bridge_feature="qibla") == ["bridge line must name the feature as the app names it"]
    line = "Arah kiblat di Samawi akurat, tanpa iklan."
    assert errors(sources, bridge_feature="qibla", bridge_line=line) == []
    assert errors(sources, bridge_feature="qibla", bridge_line="ARAH kiblat di Samawi, tanpa iklan.") == []


def test_bridge_line_must_say_tanpa_iklan(sources: Sources) -> None:
    assert errors(sources, bridge_line="Tafsir di Samawi bisa dibaca kapan saja.") == ["bridge line must say 'tanpa iklan'"]
    assert errors(sources, bridge_line="Tafsir di Samawi, Tanpa Iklan.") == []


def test_bridge_line_both_missing(sources: Sources) -> None:
    assert errors(sources, bridge_line="Samawi bisa dibaca kapan saja.") == [
        "bridge line must name the feature as the app names it",
        "bridge line must say 'tanpa iklan'",
    ]


# ── banned patterns, competitors, blocked topics ──────────────────────────────


@pytest.mark.parametrize(
    ("hook", "hit"),
    [
        ("Dijamin ayat terpanjang Al-Baqarah 282", "dijamin"),
        ("DIJAMIN ayat terpanjang Al-Baqarah 282", "dijamin"),
        ("Díjámín ayat terpanjang Al-Baqarah 282", "dijamin"),
        ("Di\u200bja\u200dmin ayat terpanjang Al-Baqarah 282", "dijamin"),
        ("Dij4min ayat terpanjang Al-Baqarah 282", "dijamin"),
        ("Ｄｉｊａｍｉｎ ayat terpanjang Al-Baqarah 282", "dijamin"),
        ("V1r4l! Ayat terpanjang Al-Baqarah 282", "viral"),
        ("Ayat terpanjang Al-Baqarah 282, viral", "viral"),
    ],
)
def test_banned_hook_in_hook(sources: Sources, hook: str, hit: str) -> None:
    for i in (0, 4):
        # Leetspeak digits are also unbacked numbers, so a leet hook fails twice.
        leet = [f"hook {i + 1}: number 4 is not backed by any cited source or computation"] if "4" in hook else []
        assert errors(sources, hooks=hooks_with(hook, i)) == [*leet, f"hook {i + 1}: banned hook pattern {hit!r}"]


def test_wajib_tahu_shouting(sources: Sources) -> None:
    assert errors(sources, hooks=hooks_with("WAJIB TAHU: ayat terpanjang Al-Baqarah 282")) == [
        "hook 1: all-caps shouting",
        "hook 1: banned hook pattern 'wajib tahu'",
    ]


def test_banned_hook_in_thread_one(sources: Sources) -> None:
    thread = ["Viral: ayat terpanjang ternyata soal utang.", *GOOD.thread[1:]]
    assert errors(sources, thread=thread) == ["thread 1: banned hook pattern 'viral'"]


def test_banned_hook_only_applies_to_hooks(sources: Sources) -> None:
    # The caption and later thread posts may use these words; only hooks are policed for them.
    thread = [*GOOD.thread[:2], "Ini bukan konten viral. " + GOOD.thread[2]]
    assert errors(sources, caption=caption_plus("Bukan konten viral."), thread=thread) == []


def test_shouting_in_caption(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("INI PENTING sekali.")) == ["caption: all-caps shouting"]


def test_acronyms_not_shouting(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "prayer:Jakarta:2026-11-26:Subuh")]
    text = "Subuh 04:05 WIB. Di WITA dan WIT beda. SAMAWI QS MUI SWT SAW."
    assert errors(sources, claims=claims, caption=caption_plus(text)) == []


@pytest.mark.parametrize(("emoji", "n_errors"), [("🙏", 0), ("🙏🌙", 0), ("🙏🌙✨", 0), ("🙏🌙✨🤲", 1), ("🙏🌙✨🤲🕌", 1)])
def test_emoji_spam(sources: Sources, emoji: str, n_errors: int) -> None:
    assert errors(sources, caption=caption_plus(emoji)) == ["caption: emoji spam"] * n_errors


def test_emoji_counted_per_piece(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("🙏🌙✨"), takeaway=GOOD.takeaway + " 🤲🕌") == []


@pytest.mark.parametrize(
    ("text", "hit"),
    [("Muslim  Pro", "muslim pro"), ("u m m a", "umma"), ("MuslimPro", "muslimpro"), ("Muslim-Pro", "muslim pro"), ("Ｕｍｍａ", "umma")],
)
def test_competitor_names(sources: Sources, text: str, hit: str) -> None:
    assert errors(sources, caption=caption_plus(f"Lebih baik dari {text}.")) == [f"caption: competitor name {hit!r}"]


def test_normal_words_not_competitors(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("Untuk umat yang ingin rutin baca Al-Kahfi tiap Jumat.")) == []


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Coba trial dulu.", "trial"),
        ("Langganan sekarang.", "langganan"),
        ("Ketik AAMIIN di komentar.", "ketik aamiin"),
        ("Kalau gak share, rugi.", "kalau gak share"),
        ("Kalau kamu tidak bagikan, sayang.", "kalau kamu tidak bagikan"),
        ("Ada diskon.", "diskon"),
    ],
)
def test_banned_cta(sources: Sources, text: str, hit: str) -> None:
    assert errors(sources, caption=caption_plus(text)) == [f"caption: banned CTA phrase {hit!r}"]


def test_banned_cta_price_also_fails_number_check(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("Cuma Rp19.000.")) == [
        "caption: number 19000 is not backed by any cited source or computation",
        "caption: banned CTA phrase 'rp1'",
    ]


def test_banned_cta_in_share_prompt(sources: Sources) -> None:
    assert errors(sources, share_prompt="Kalau nggak share, dosa lho.") == ["share prompt: banned CTA phrase 'kalau nggak share'"]


@pytest.mark.parametrize(
    ("text", "topic", "hits"),
    [
        ("Soal qunut Subuh.", "khilafiyah", "qunut"),
        ("Tentang tahlilan.", "khilafiyah", "tahlilan"),
        ("Tentang maulid.", "khilafiyah", "maulid"),
        ("Debat bid'ah.", "khilafiyah", "bid ah"),
        ("Beda NU dan Muhammadiyah.", "khilafiyah", "nu dan muhammadiyah"),
        ("Berapa rakaat tarawih?", "khilafiyah", "rakaat tarawih"),
        ("Subuh 18° vs 20°?", "khilafiyah", "18 derajat vs 20 derajat"),
        ("Bukan soal pemilu.", "politics", "pemilu"),
        ("Bukan soal Kristen.", "other_religions", "kristen"),
        ("Hukumnya kamu boleh menunda.", "fatwa_style", "hukumnya kamu boleh, kamu boleh"),
        ("Doa ini menyembuhkan.", "health_wealth", "menyembuhkan"),
        ("Rahasia angka 19.", "ijaz_numerology", "angka 19"),
        ("Ini i'jaz ilmi.", "ijaz_numerology", "i jaz"),
        ("Awas azab.", "fear_guilt", "azab"),
        ("Soal poligami.", "gender_controversy", "poligami"),
        ("Hukum hijab.", "gender_controversy", "hukum hijab"),
    ],
)
def test_blocked_topics_in_copy(sources: Sources, text: str, topic: str, hits: str) -> None:
    errs = errors(sources, caption=caption_plus(text))
    assert f"caption: blocked topic {topic} ({hits})" in errs
    assert [e for e in errs if "blocked topic" not in e and "number" not in e] == []


def test_legit_degree_text_not_blocked(sources: Sources) -> None:
    # "Subuh 20 derajat" alone is a fact about the Kemenag standard, not the debate. (It fails the
    # number check here only because no fact backs 20 in this draft.)
    errs = errors(sources, caption=caption_plus("Kemenag memakai Subuh 20 derajat dan Isya 18 derajat."))
    assert errs == [
        "caption: number 20 is not backed by any cited source or computation",
        "caption: number 18 is not backed by any cited source or computation",
    ]


def test_check_order_within_one_piece(sources: Sources) -> None:
    errs = errors(sources, caption=caption_plus("Trial Muslim Pro, soal qunut, PENTING SEKALI 🙏🌙✨🤲"))
    assert errs == [
        "caption: banned CTA phrase 'trial'",
        "caption: competitor name 'muslim pro'",
        "caption: blocked topic khilafiyah (qunut)",
        "caption: all-caps shouting",
        "caption: emoji spam",
    ]


# ── hooks ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [4, 6])
def test_hook_count(sources: Sources, n: int) -> None:
    hooks = (list(GOOD.hooks) * 2)[:n]
    assert errors(sources, hooks=hooks) == [f"expected 5 hook candidates, got {n}"]


def test_cover_hook_word_limit(sources: Sources) -> None:
    ten = "Ayat terpanjang Al-Qur'an ada di Al-Baqarah, isinya soal utang piutang"
    assert len(ten.split()) == 10
    assert errors(sources, hooks=hooks_with(ten)) == []
    eleven = ten + " lagi"
    assert errors(sources, hooks=hooks_with(eleven, 2)) == ["hook 3: more than 10 words"]


def test_zero_width_not_counted_as_words(sources: Sources) -> None:
    ten = "Ayat terpanjang Al-Qur'an ada di Al-Baqarah, isinya soal utang piutang \u200b"
    assert errors(sources, hooks=hooks_with(ten)) == []


@pytest.mark.parametrize(
    ("hook", "ok"),
    [
        ("Ternyata ayat terpanjang bukan soal salat", False),  # only the sentence-initial capital
        ("ayat terpanjang bukan soal salat", False),
        ("Ternyata ayat terpanjang ada di Al-Baqarah", True),  # proper noun
        ("Ternyata ayat terpanjang ada di surah kedua", True),
        ("Ternyata ayat terpanjang ada di SURAH kedua", True),
        ("Lihat QS kedua, ayat terpanjang", True),
        ("Ayat terpanjang punya 128 kata", True),
        ("Ternyata ayat terpanjang bukan 'soal' salat", False),
        ("Ternyata 'Ayat' terpanjang", False),  # quoted capital: first char is not a letter
        ("Kenapa Al-Baqarah punya ayat terpanjang", True),  # proper noun as the second word
        ("QS kedua, ayat terpanjang soal utang", True),  # QS as the first word
        ("lihat qs kedua soal utang", False),  # lowercase "qs" is not a reference
        ("Ternyata ayat terpanjang soal E-KTP", True),
    ],
)
def test_hook_needs_something_specific(sources: Sources, hook: str, ok: bool) -> None:
    errs = errors(sources, hooks=hooks_with(hook, 1))
    assert errs == ([] if ok else ["hook 2: needs a number, a proper noun, or a surah/ayat reference"])


def test_threads_first_line_limit(sources: Sources) -> None:
    line140 = padded("Ternyata ayat terpanjang bukan soal salat.", 140)
    assert errors(sources, thread=[line140, *GOOD.thread[1:]]) == []
    assert errors(sources, thread=[line140 + "\nBaris kedua boleh panjang.", *GOOD.thread[1:]]) == []
    multi = padded("Ternyata ayat terpanjang bukan soal salat.", 100) + "\n" + "y" * 100 + "\n" + "z" * 100
    assert errors(sources, thread=[multi, *GOOD.thread[1:]]) == []  # only the first line counts
    line141 = padded("Ternyata ayat terpanjang bukan soal salat.", 141)
    assert errors(sources, thread=[line141, *GOOD.thread[1:]]) == ["thread 1: first line over 140 characters"]


# ── limits ────────────────────────────────────────────────────────────────────


def test_caption_2200_passes_2201_fails(sources: Sources) -> None:
    assert errors(sources, caption=padded(CAP, 2200)) == []
    assert errors(sources, caption=padded(CAP, 2201)) == ["caption is 2201 characters; max 2200"]


def test_caption_counts_code_points_for_arabic_and_emoji(sources: Sources) -> None:
    base = CAP + " 🙏🌙✨"
    two_byte = padded(base, 2200, "é")
    assert len(two_byte.encode()) > 2200
    assert errors(sources, caption=two_byte) == []
    assert errors(sources, caption=padded(base, 2201, "é")) == ["caption is 2201 characters; max 2200"]
    # Arabic is counted in code points too (the length error appears only at 2,201), but the writer
    # may not type Arabic at all: scripture comes from sources.
    arabic_msg = "caption: contains Arabic script; reference the source instead of typing it"
    assert errors(sources, caption=padded(base, 2200, "ب")) == [arabic_msg]
    assert errors(sources, caption=padded(base, 2201, "ب")) == [arabic_msg, "caption is 2201 characters; max 2200"]


def test_threads_post_500_passes_501_fails(sources: Sources) -> None:
    base = "Al-Baqarah ayat 282 memerintahkan pencatatan."
    assert errors(sources, thread=[GOOD.thread[0], padded(base, 500), GOOD.thread[2]]) == []
    assert errors(sources, thread=[GOOD.thread[0], padded(base, 501), GOOD.thread[2]]) == ["threads post 2 is 501 characters; max 500"]


def test_threads_subuh_and_malam_limits(sources: Sources) -> None:
    long = padded("Pagi ini.", 501)
    assert errors(sources, threads_subuh=long) == ["threads post 4 is 501 characters; max 500"]
    assert errors(sources, threads_malam=long) == ["threads post 5 is 501 characters; max 500"]


def test_threads_multibyte_counted_as_code_points(sources: Sources) -> None:
    # 500 four-byte code points = 2,000 bytes: still within both limits.
    four = "𝐀"
    assert len(four.encode()) == 4
    post = "x" + four * 499
    assert errors(sources, threads_malam=post) == []
    assert errors(sources, threads_malam=post + four) == ["threads post 5 is 501 characters; max 500"]
    assert errors(sources, threads_subuh=padded("Renungan:", 500, "é")) == []
    arabic_msg = "threads subuh: contains Arabic script; reference the source instead of typing it"
    assert errors(sources, threads_subuh=padded("Renungan:", 500, "ب")) == [arabic_msg]
    assert errors(sources, threads_subuh=padded("Renungan:", 501, "ب")) == [arabic_msg, "threads post 4 is 501 characters; max 500"]


def test_threads_all_four_byte_at_limit(sources: Sources) -> None:
    post = "𝐀" * 500
    assert len(post.encode()) == 2000
    assert errors(sources, threads_malam=post) == []


def test_lone_surrogate_does_not_crash_the_checks(sources: Sources) -> None:
    assert errors(sources, threads_malam=GOOD.threads_malam + "\ud800") == []


def test_threads_byte_cap(sources: Sources, monkeypatch: pytest.MonkeyPatch) -> None:
    # The 2,000-byte cap cannot be hit with ≤ 500 code points of real text, so check it with a
    # counter that reports bytes over the cap.
    monkeypatch.setattr(DC, "utf8_len", lambda t: 2001 if t == GOOD.threads_malam else 2000)
    assert errors(sources) == [f"threads post 5 is {len(GOOD.threads_malam)} characters; max 500"]


@pytest.mark.parametrize(("n", "ok"), [(3, True), (5, True), (6, False), (7, False)])
def test_hashtag_count(sources: Sources, n: int, ok: bool) -> None:
    tags = [f"#tag{chr(97 + i)}" for i in range(n)]
    assert errors(sources, hashtags=tags) == ([] if ok else [f"{n} hashtags; max 5"])


@pytest.mark.parametrize("bad", ["#fakta quran", "faktaquran", " #faktaquran"])
def test_hashtag_must_be_single_word(sources: Sources, bad: str) -> None:
    assert errors(sources, hashtags=["#samawi", bad]) == ["hashtags must be single #words"]


def test_no_hashtags_ok(sources: Sources) -> None:
    assert errors(sources, hashtags=[]) == []


def _slides(n: int) -> list[dict[str, Any]]:
    base = slides_json()
    extra = {"template": "list", "heading": "Catatan", "items": ["Tulis dengan jelas"]}
    return (base + [extra] * n)[:n]


@pytest.mark.parametrize(("n_slides", "ok"), [(3, False), (4, True), (5, True), (7, True), (8, False)])
def test_carousel_total_slides(sources: Sources, n_slides: int, ok: bool) -> None:
    # Cover and CTA are added by code: total = len(slides) + 2, which must be 6\u20139.
    errs = errors(sources, slides=_slides(n_slides))
    assert errs == ([] if ok else [f"carousel has {n_slides + 2} slides; needs 6\u20139"])


def test_reel_has_no_slide_limit(sources: Sources) -> None:
    assert errors(sources, format="reel", slides=_slides(1)) == []
    assert errors(sources, format="reel", slides=_slides(12)) == []


@pytest.mark.parametrize(("n", "ok"), [(1, False), (2, True), (20, True), (21, False)])
def test_thread_chain_length(sources: Sources, n: int, ok: bool) -> None:
    thread = (list(GOOD.thread) + ["Satu ide per balasan."] * 20)[:n]
    assert errors(sources, thread=thread) == ([] if ok else ["thread chain needs 2\u201320 posts"])


def test_empty_thread(sources: Sources) -> None:
    assert errors(sources, thread=[]) == ["thread chain needs 2\u201320 posts"]


# ── teaser / caption rules ────────────────────────────────────────────────────


def test_teaser_must_start_with_besok(sources: Sources) -> None:
    teaser = "Nanti: surah yang punya dua Bismillah. Follow @samawi.indonesia biar nggak ketinggalan."
    assert errors(sources, next_teaser=teaser) == ["teaser must start with 'Besok'"]
    assert errors(sources, next_teaser="besok: " + teaser) == []
    assert errors(sources, next_teaser="BESOK: " + teaser) == []


def test_follow_handle_in_teaser_or_caption(sources: Sources) -> None:
    teaser = "Besok: surah yang punya dua Bismillah."
    assert errors(sources, next_teaser=teaser) == ["the follow hook must name @samawi.indonesia"]
    assert errors(sources, next_teaser=teaser, caption=caption_plus("Follow @samawi.indonesia.")) == []
    assert errors(sources, next_teaser=teaser + " Follow @samawi.indo") == ["the follow hook must name @samawi.indonesia"]


@pytest.mark.parametrize("extra", ["Sumber: QS. Al-Baqarah 282.", "SUMBER: Kemenag", "Terjemahan Kemenag.", "terjemahan kemenag"])
def test_caption_no_sources_list(sources: Sources, extra: str) -> None:
    errs = errors(sources, caption=caption_plus(extra))
    assert "caption must carry no sources list or translation credit" in errs
    assert [e for e in errs if "number" not in e and "shouting" not in e] == ["caption must carry no sources list or translation credit"]


# ── too easy / topic reuse ────────────────────────────────────────────────────


def test_too_easy_key_fact(sources: Sources) -> None:
    assert errors(sources, key_fact="Al-Qur'an ada 114 surah") == ["too easy: matches 'Al-Qur'an ada 114 surah'"]


def test_too_easy_topic(sources: Sources) -> None:
    assert errors(sources, topic="Ayat Kursi, ayat 255") == ["too easy: matches 'Ayat Kursi itu ayat 255'"]


def test_too_easy_both_reported_once(sources: Sources) -> None:
    assert errors(sources, topic="Salat 5 waktu", key_fact="Salat wajib ada lima waktu") == ["too easy: matches 'Salat wajib 5 waktu'"]


def test_too_easy_extra_entries(sources: Sources) -> None:
    extra = TooEasy("learned-1", "Ayat terpanjang itu Al-Baqarah 282", (("terpanjang",), ("282",)))
    assert errors(sources, mkctx(extra_too_easy=[extra])) == ["too easy: matches 'Ayat terpanjang itu Al-Baqarah 282'"]
    other = TooEasy("learned-2", "Ayat pendek", (("terpendek",),))
    assert errors(sources, mkctx(extra_too_easy=[other])) == []


@pytest.mark.parametrize(("days_ago", "ok"), [(0, False), (1, False), (59, False), (60, True), (61, True), (365, True)])
def test_topic_cooldown(sources: Sources, days_ago: int, ok: bool) -> None:
    # The code fails when today - last < 60 days: day 59 is a reuse, day 60 is allowed again.
    last = TODAY - timedelta(days=days_ago)
    ctx = mkctx(topic_history={topic_hash(GOOD.topic): last})
    assert errors(sources, ctx) == ([] if ok else [f"topic reused within 60 days (last {last.isoformat()})"])


def test_topic_cooldown_catches_reworded_topic(sources: Sources) -> None:
    last = TODAY - timedelta(days=10)
    ctx = mkctx(topic_history={topic_hash("membahas utang-piutang: ayat TERPANJANG dalam Al-Qur'an"): last})
    assert errors(sources, ctx) == ["topic reused within 60 days (last 2026-11-16)"]


def test_topic_history_other_topics_ignored(sources: Sources) -> None:
    ctx = mkctx(topic_history={topic_hash("Ayat terpendek"): TODAY})
    assert errors(sources, ctx) == []


# ── evergreen ─────────────────────────────────────────────────────────────────


def _evergreen_draft(**overrides: Any) -> dict[str, Any]:
    slides = slides_json()
    slides[4]["heading"] = "Catat utangmu"
    base: dict[str, Any] = {"series": None, "slides": slides}
    base.update(overrides)
    return base


def test_evergreen_clean_draft_passes(sources: Sources) -> None:
    assert errors(sources, mkctx(evergreen=True), **_evergreen_draft()) == []


def test_evergreen_rejects_series_and_dated_copy(sources: Sources) -> None:
    # The good draft is not evergreen: it is part 1 of a series and slide 6 says "hari ini".
    assert errors(sources, mkctx(evergreen=True)) == [
        "evergreen posts are not part of a series",
        "slide 6: evergreen copy refers to a specific day",
    ]


@pytest.mark.parametrize(
    "key",
    [
        "prayer:Jakarta:2026-11-26:Subuh",
        "compare:Sabang:Merauke:2026-11-26:Maghrib",
        "extremes:2026-11-26:Subuh",
        "hijri:today:2026-11-26",
    ],
)
def test_evergreen_rejects_dated_facts(sources: Sources, key: str) -> None:
    claims = [*claims_json(), claim("fact", key, "Fakta")]
    assert errors(sources, mkctx(evergreen=True), **_evergreen_draft(claims=claims)) == [f"evergreen post cites dated fact {key}"]


def test_evergreen_dated_fact_in_extra_sources(sources: Sources) -> None:
    ctx = mkctx(evergreen=True, extra_sources=[SourceRef(type="fact", ref="hijri:today:2026-11-26")])
    assert errors(sources, ctx, **_evergreen_draft()) == ["evergreen post cites dated fact hijri:today:2026-11-26"]


def test_evergreen_allows_undated_facts_and_scripture(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "quran:totals", "Fakta"), claim("fact", "const:juz", "Fakta"), claim("hadith", "3044", "Hadis")]
    assert errors(sources, mkctx(evergreen=True), **_evergreen_draft(claims=claims)) == []


def test_non_evergreen_allows_dated_facts(sources: Sources) -> None:
    claims = [*claims_json(), claim("fact", "hijri:today:2026-11-26", "Fakta")]
    assert errors(sources, claims=claims) == []


@pytest.mark.parametrize("phrase", ["hari ini", "Hari  ini", "pekan ini", "minggu ini", "bulan ini", "tahun ini", "kemarin", "HARI\u200b INI"])
def test_evergreen_rejects_dated_words(sources: Sources, phrase: str) -> None:
    errs = errors(sources, mkctx(evergreen=True), **_evergreen_draft(caption=caption_plus(f"Mulai {phrase}.")))
    assert [e for e in errs if "shouting" not in e] == ["caption: evergreen copy refers to a specific day"]


@pytest.mark.parametrize("field", ["next_teaser", "threads_subuh", "threads_malam"])
def test_evergreen_dated_words_allowed_in_exempt_fields(sources: Sources, field: str) -> None:
    value = {"next_teaser": GOOD.next_teaser + " Mulai hari ini.", "threads_subuh": "Hari ini, satu ayat.", "threads_malam": "Apa yang kamu catat hari ini?"}[
        field
    ]
    assert errors(sources, mkctx(evergreen=True), **_evergreen_draft(**{field: value})) == []


def test_evergreen_dated_words_checked_on_raw_text_too(sources: Sources) -> None:
    # Removing the zero-width space joins "Mulai" and "Hari"; the raw lowercase form still catches it.
    errs = errors(sources, mkctx(evergreen=True), **_evergreen_draft(caption=caption_plus("Mulai\u200bHari ini.")))
    assert errs == ["caption: evergreen copy refers to a specific day"]


def test_evergreen_dated_word_must_be_whole(sources: Sources) -> None:
    assert errors(sources, mkctx(evergreen=True), **_evergreen_draft(caption=caption_plus("Sehari inilah."))) == []


def test_non_evergreen_allows_dated_words(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("Mulai hari ini.")) == []


# ── launch mode ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cta",
    [
        "Link di bio.",
        "LINK DI BIO",
        "Download sekarang.",
        "Cek di Play Store.",
        "Cek di PlayStore.",
        "Cek di Play\u200bStore.",
        "Komentar SAMAWI, kami kirim link-nya.",
        "Komentar  samawi",
        "link  di  bio",
        "d o w n l o a d",
    ],
)
def test_prelaunch_rejects_install_cta(sources: Sources, cta: str) -> None:
    errs = errors(sources, caption=caption_plus(cta))
    assert [e for e in errs if "shouting" not in e] == ["caption: install CTA before launch"]
    assert [e for e in errors(sources, mkctx(launch_mode=LaunchMode.LIVE), caption=caption_plus(cta)) if "shouting" not in e] == []


def test_prelaunch_install_cta_anywhere(sources: Sources) -> None:
    assert errors(sources, share_prompt="Download Samawi lalu kirim ke teman.", takeaway="Link di bio.") == [
        "takeaway: install CTA before launch",
        "share prompt: install CTA before launch",
    ]


def test_prelaunch_allows_coming_soon(sources: Sources) -> None:
    assert errors(sources, caption=caption_plus("Segera hadir di Android. Follow biar kamu yang pertama tahu.")) == []
