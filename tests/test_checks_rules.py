"""checks/rules.py and the YAML rules in content/: loading, matching, topic hashing."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from samawi_bot.checks import rules as R
from samawi_bot.checks.rules import (
    STOPWORDS,
    CheckReport,
    TooEasy,
    blocked_topic_hits,
    find,
    load_rules,
    load_too_easy,
    topic_hash,
)


@pytest.fixture(scope="module")
def rules() -> R.Rules:
    return load_rules()


# ── loading ───────────────────────────────────────────────────────────────────


def test_limits_are_exactly_the_spec(rules: R.Rules) -> None:
    assert rules.limits == {
        "hashtags_max": 5,
        "caption_max_chars": 2200,
        "threads_post_max_chars": 500,
        "cover_max_words": 10,
        "threads_hook_max_chars": 140,
        "carousel_slides_min": 6,
        "carousel_slides_max": 9,
        "topic_cooldown_days": 60,
    }


def test_rule_sets_loaded(rules: R.Rules) -> None:
    assert list(rules.blocked_topics) == [
        "khilafiyah",
        "politics",
        "other_religions",
        "fatwa_style",
        "health_wealth",
        "ijaz_numerology",
        "fear_guilt",
        "gender_controversy",
    ]
    assert len(rules.banned_hook) == 7
    assert len(rules.banned_cta) == 12
    assert all(isinstance(p, re.Pattern) for p in rules.banned_hook + rules.banned_cta)
    assert [p.pattern for p in rules.competitors] == [
        r"\bumma\b",
        r"\bmuslim\ pro\b",
        r"\bmuslimpro\b",
        r"\bmuslim\ muda\b",
        r"\bquran\ kemenag\b",
        r"\bqur\ an\ kemenag\b",
        r"\bathan\b",
        r"\bpillars\b",
        r"\bsalaam\b",
        r"\bmuslim\ assistant\b",
        r"\bjadwal\ sholat\ pro\b",
    ]
    assert load_rules() is rules  # cached


def test_load_rules_uncached_compiles_every_pattern(rules: R.Rules) -> None:
    fresh = load_rules.__wrapped__()
    assert fresh == rules
    assert [p.pattern for p in fresh.banned_hook][:2] == [r"\b99 ?% orang", r"\bdijamin\b"]
    assert all(isinstance(p, re.Pattern) for ps in fresh.blocked_topics.values() for p in ps)
    assert len(fresh.blocked_topics["khilafiyah"]) == 9


def test_read_rejects_non_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "bad.yaml").write_text("- just\n- a list\n", "utf-8")
    monkeypatch.setattr(R, "resources", SimpleNamespace(files=lambda pkg: tmp_path))
    with pytest.raises(ValueError, match=r"^bad.yaml is not a mapping$"):
        R._read("bad.yaml")


def test_read_returns_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ok.yaml").write_text("a: 1\n", "utf-8")
    seen: list[str] = []

    def files(pkg: str) -> Path:
        seen.append(pkg)
        return tmp_path

    monkeypatch.setattr(R, "resources", SimpleNamespace(files=files))
    assert R._read("ok.yaml") == {"a": 1}
    assert seen == ["samawi_bot.content"]


# ── too easy ──────────────────────────────────────────────────────────────────


def test_load_too_easy_seed() -> None:
    entries = load_too_easy()
    assert [e.id for e in entries] == [
        "quran-114-surah",
        "salat-5-waktu",
        "fatihah-7-ayat",
        "rukun-islam-5",
        "lailatul-qadar-ramadan",
        "nabi-terakhir",
        "puasa-ramadan-wajib",
        "ayat-kursi-255",
    ]
    assert entries[0] == TooEasy("quran-114-surah", "Al-Qur'an ada 114 surah", (("114",), ("surah", "surat")))


def test_load_too_easy_extra_appended() -> None:
    extra = [{"id": "x", "text": "Subuh 2 rakaat", "all_of": [["subuh"], ["2", "dua"]]}]
    entries = load_too_easy(extra)
    assert len(entries) == 9
    assert entries[-1] == TooEasy("x", "Subuh 2 rakaat", (("subuh",), ("2", "dua")))


def test_load_too_easy_learned_and_missing_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    learned = {"id": "l", "text": "L", "all_of": [["a"]]}
    monkeypatch.setattr(R, "_read", lambda name: {"seed": None, "learned": [learned]})
    assert load_too_easy([{"id": "e", "text": "E", "all_of": [["b"]]}]) == [TooEasy("l", "L", (("a",),)), TooEasy("e", "E", (("b",),))]
    monkeypatch.setattr(R, "_read", lambda name: {"seed": [learned]})
    assert load_too_easy() == [TooEasy("l", "L", (("a",),))]
    monkeypatch.setattr(R, "_read", lambda name: {})
    assert load_too_easy() == []


@pytest.mark.parametrize(
    ("text", "hit"),
    [
        ("Al-Qur'an ada 114 surah", True),
        ("AL-QUR'AN ADA 114 SURAT", True),
        ("114 surah", True),
        ("Ada 114 ayat", False),  # second group missing
        ("Ada 1140 surah", False),  # whole-token match only
        ("Ada 114surah", False),
        ("surah", False),
        ("", False),
    ],
)
def test_too_easy_matches(text: str, hit: bool) -> None:
    entry = load_too_easy()[0]
    assert entry.matches(text) is hit


def test_too_easy_multiword_terms_and_spacing() -> None:
    entry = TooEasy("x", "x", (("rukun islam",), ("lima", "5")))
    assert entry.matches("Rukun  Islam ada lima")
    assert entry.matches("r u k u n islam 5") is True  # spaced-out letters re-join
    assert entry.matches("Rukun Islam") is False
    assert TooEasy("y", "y", ()).matches("anything") is True


def test_too_easy_spaced_letters_join() -> None:
    entry = TooEasy("x", "x", (("rukun islam",),))
    assert entry.matches("r u k u n islam") is True


# ── find / blocked topics ─────────────────────────────────────────────────────


def test_find_dedupes_across_variants(rules: R.Rules) -> None:
    assert find(rules.banned_hook, "viral v1r4l VIRAL") == ["viral"]
    assert find(rules.banned_hook, "v1r4l") == ["viral"]
    assert find(rules.banned_hook, "dijamin viral") == ["dijamin", "viral"]
    assert find(rules.banned_hook, "aman") == []


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("Dijamin", ["dijamin"]),
        ("DIJAMIN", ["dijamin"]),
        ("Díjámín", ["dijamin"]),
        ("di\u200bja\u200cmin", ["dijamin"]),
        ("dij4min", ["dijamin"]),
        ("Dij4min!", ["dijamin"]),
        ("ｄｉｊａｍｉｎ", ["dijamin"]),
        ("v1r4l", ["viral"]),
        ("V I R A L", ["viral"]),
        ("99% orang tidak tahu", ["99% orang"]),
        ("99 % orang", ["99 % orang"]),
        ("Pasti masuk surga", ["pasti masuk surga"]),
        ("rezeki lancar", ["rezeki lancar"]),
        ("Dosa jika dilewatkan", ["dosa jika"]),
        ("WAJIB TAHU", ["wajib tahu"]),
        ("wajib\u2066 tahu", ["wajib tahu"]),
        ("Ayat terpanjang Al-Baqarah 282", []),
        ("viralkan", []),
        ("jaminan", []),
    ],
)
def test_banned_hooks(rules: R.Rules, text: str, hits: list[str]) -> None:
    assert find(rules.banned_hook, text) == hits


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("Coba trial 7 hari", ["trial"]),
        ("TR14L", ["trial"]),
        ("uji coba gratis", ["uji coba gratis", "coba gratis"]),
        ("gratis 30 hari", ["gratis 30 hari"]),
        ("Rp19.000 saja", ["rp1"]),
        ("Rp 19.000", ["rp 1"]),
        ("harga spesial", ["harga"]),
        ("ada diskon", ["diskon"]),
        ("kalau gak share", ["kalau gak share"]),
        ("Kalau kamu tidak bagikan", ["kalau kamu tidak bagikan"]),
        ("kalau nggak share", ["kalau nggak share"]),
        ("kalau ga share", ["kalau ga share"]),
        ("Ketik AAMIIN", ["ketik aamiin"]),
        ("ketik amin", ["ketik amin"]),
        ("Jadi Muslim yang lebih baik dengan Samawi", ["jadi muslim yang lebih baik dengan"]),
        ("Langganan sekarang", ["langganan"]),
        ("berlangganan", ["berlangganan"]),
        ("Kalau kamu share, terima kasih", []),
        ("gratis selamanya, tanpa iklan", []),
        ("trials", []),
        ("RPG", []),
    ],
)
def test_banned_cta(rules: R.Rules, text: str, hits: list[str]) -> None:
    assert find(rules.banned_cta, text) == hits


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("Muslim  Pro", ["muslim pro"]),
        ("muslim-pro", ["muslim pro"]),
        ("MuslimPro", ["muslimpro"]),
        ("u m m a", ["umma"]),
        ("U\u200bmma", ["umma"]),
        ("Umm4", ["umma"]),
        ("Muslim Muda", ["muslim muda"]),
        ("Quran Kemenag", ["quran kemenag"]),
        ("Al-Qur'an Kemenag", ["qur an kemenag"]),
        ("Athan", ["athan"]),
        ("Pillars", ["pillars"]),
        ("Salaam", ["salaam"]),
        ("Muslim Assistant", ["muslim assistant"]),
        ("Jadwal Sholat Pro", ["jadwal sholat pro"]),
        ("umat Islam", []),
        ("Jumat berkah", []),
        ("ummat", []),
        ("Terjemahan Kemenag", []),
        ("muslim profesional", []),
        ("Nathan", []),
    ],
)
def test_competitors(rules: R.Rules, text: str, hits: list[str]) -> None:
    assert find(rules.competitors, text) == hits


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hukum qunut Subuh", {"khilafiyah": ["qunut"]}),
        ("tahlilan", {"khilafiyah": ["tahlilan"]}),
        ("tahlil", {"khilafiyah": ["tahlil"]}),
        ("Maulid Nabi", {"khilafiyah": ["maulid"]}),
        ("maulidan", {"khilafiyah": ["maulidan"]}),
        ("ziarah kubur", {"khilafiyah": ["ziarah"]}),
        ("bid'ah", {"khilafiyah": ["bid ah"]}),
        ("bidah", {"khilafiyah": ["bidah"]}),
        ("NU vs Muhammadiyah", {"khilafiyah": ["nu vs muhammadiyah"]}),
        ("Muhammadiyah dan NU", {"khilafiyah": ["muhammadiyah dan nu"]}),
        ("rakaat tarawih", {"khilafiyah": ["rakaat tarawih"]}),
        ("tarawih 11 rakaat", {"khilafiyah": ["tarawih 11 rakaat"]}),
        ("23 rakaat", {"khilafiyah": ["23 rakaat"]}),
        ("8 rakaat", {"khilafiyah": ["8 rakaat"]}),
        ("18 vs 20 derajat", {"khilafiyah": ["18 vs 20 derajat"]}),
        ("Subuh 18° atau 20°", {"khilafiyah": ["18 derajat atau 20 derajat"]}),
        ("20 derajat versus 18 derajat", {"khilafiyah": ["20 derajat versus 18 derajat"]}),
        ("20 lawan 18", {"khilafiyah": ["20 lawan 18"]}),
        ("Subuh kepagian", {"khilafiyah": ["subuh kepagian"]}),
        ("subuh terlalu cepat", {"khilafiyah": ["subuh terlalu cepat"]}),
        ("Pemilu 2029", {"politics": ["pemilu"]}),
        ("pilpres", {"politics": ["pilpres"]}),
        ("pilkada", {"politics": ["pilkada"]}),
        ("partai", {"politics": ["partai"]}),
        ("Presiden", {"politics": ["presiden"]}),
        ("capres", {"politics": ["capres"]}),
        ("Gaza", {"politics": ["gaza"]}),
        ("Palestina", {"politics": ["palestina"]}),
        ("Israel", {"politics": ["israel"]}),
        ("Kristen", {"other_religions": ["kristen"]}),
        ("Hindu", {"other_religions": ["hindu"]}),
        ("nasrani", {"other_religions": ["nasrani"]}),
        ("kafir", {"other_religions": ["kafir"]}),
        ("takfir", {"other_religions": ["takfir"]}),
        ("Syiah", {"other_religions": ["syiah"]}),
        ("wahabi", {"other_religions": ["wahabi"]}),
        ("Hukumnya kamu boleh", {"fatwa_style": ["hukumnya kamu boleh", "kamu boleh"]}),
        ("kamu wajib untuk", {"fatwa_style": ["kamu wajib"]}),
        ("Kamu haram makan", {"fatwa_style": ["kamu haram"]}),
        ("Jadi kamu wajib.", {"fatwa_style": ["kamu wajib"]}),
        ("Doa ini menyembuhkan", {"health_wealth": ["menyembuhkan"]}),
        ("amalan pembuka rezeki", {"health_wealth": ["pembuka rezeki"]}),
        ("rezeki pasti datang", {"health_wealth": ["rezeki pasti"]}),
        ("kaya mendadak", {"health_wealth": ["kaya mendadak"]}),
        ("Keajaiban angka 19", {"ijaz_numerology": ["angka 19"]}),
        ("mukjizat ilmiah", {"ijaz_numerology": ["mukjizat ilmiah"]}),
        ("mukjizat angka", {"ijaz_numerology": ["mukjizat angka"]}),
        ("I'jaz ilmi", {"ijaz_numerology": ["i jaz"]}),
        ("ijaz", {"ijaz_numerology": ["ijaz"]}),
        ("Al-Qur'an meramalkan", {"ijaz_numerology": ["al qur an meramalkan"]}),
        ("Al-Quran meramalkan", {"ijaz_numerology": ["al quran meramalkan"]}),
        ("Alquran meramalkan", {"ijaz_numerology": ["alquran meramalkan"]}),
        ("Sains membuktikan", {"ijaz_numerology": ["sains membuktikan"]}),
        ("5ain5 membuktikan", {"ijaz_numerology": ["sains membuktikan"]}),
        ("dosa kalau gak share", {"fear_guilt": ["dosa kalau gak share"]}),
        ("Azab", {"fear_guilt": ["azab"]}),
        ("siksa kubur", {"fear_guilt": ["siksa kubur"]}),
        ("Neraka menantimu", {"fear_guilt": ["neraka menantimu"]}),
        ("Poligami", {"gender_controversy": ["poligami"]}),
        ("hukum hijab", {"gender_controversy": ["hukum hijab"]}),
        ("hukum cadar", {"gender_controversy": ["hukum cadar"]}),
        ("Qunut dan pemilu", {"khilafiyah": ["qunut"], "politics": ["pemilu"]}),
    ],
)
def test_blocked_topics(rules: R.Rules, text: str, expected: dict[str, list[str]]) -> None:
    assert blocked_topic_hits(rules, text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Subuh 20 derajat",
        "Isya 18 derajat",
        "Subuh memakai sudut 20°",
        "Isya 18°",
        "Subuh 2 rakaat",
        "Samudra Hindia",
        "Buddhisme",  # not a whole-word match
        "presidensial",
        "Ayat terpanjang Al-Baqarah 282",
        "kamu",
        "kamu bolehkan",
        "Mas'ud meriwayatkan",
        "kisah Nabi Yusuf",
        "Jumat berkah",
        "hijab",
        "",
    ],
)
def test_legitimate_text_not_blocked(rules: R.Rules, text: str) -> None:
    assert blocked_topic_hits(rules, text) == {}


# ── topic hash ────────────────────────────────────────────────────────────────


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def test_topic_hash_exact() -> None:
    assert topic_hash("Ayat Terpanjang") == _sha("ayat terpanjang")
    assert topic_hash("Ternyata, ayat yang terpanjang!") == _sha("ayat terpanjang")
    assert topic_hash("") == _sha("")
    assert len(topic_hash("x")) == 16


def test_topic_hash_stable_across_case_punctuation_order_stopwords() -> None:
    a = topic_hash("Ayat terpanjang di Al-Qur'an membahas utang-piutang")
    assert a == topic_hash("ayat TERPANJANG di al qur'an: membahas utang piutang")
    assert a == topic_hash("membahas utang piutang, ayat terpanjang Al-Qur'an")
    assert a == topic_hash("Ayat terpanjang dalam Al-Qur'an yang membahas utang piutang")
    assert a == topic_hash("ayat ayat terpanjang Al-Qur'an membahas utang piutang")
    assert a != topic_hash("Ayat terpendek di Al-Qur'an")


@pytest.mark.parametrize("word", sorted(STOPWORDS))
def test_every_stopword_is_dropped(word: str) -> None:
    assert topic_hash(f"{word} kiblat") == topic_hash("kiblat")


def test_stopword_list_exact() -> None:
    assert (
        frozenset(
            "yang di ke dari dan atau dalam pada untuk dengan ini itu adalah apa kenapa mengapa ternyata satu satunya the of al an".split()  # noqa: SIM905
        )
        == STOPWORDS
    )


# ── CheckReport ───────────────────────────────────────────────────────────────


def test_check_report() -> None:
    r = CheckReport()
    assert r.ok is True
    assert r.errors == [] and r.warnings == [] and r.allowed_numbers == set()
    r.fail("x")
    r.fail("y")
    assert r.errors == ["x", "y"]
    assert r.ok is False
    assert CheckReport().errors is not CheckReport().errors
    assert CheckReport(warnings=["w"]).ok is True
