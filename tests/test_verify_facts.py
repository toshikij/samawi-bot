"""verify/facts.py: every computed fact the bot may state (spec §5, §6.3)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from conftest import FIXTURES, TODAY
from samawi_bot.sources import Sources
from samawi_bot.sources.hijri import ISBAT_NOTE
from samawi_bot.sources.prayer import IndonesiaTZ, Prayer
from samawi_bot.verify import facts as F
from samawi_bot.verify.facts import CONSTANTS, EXTRA_PLACES, Fact, UnknownFact, compute, fact_pack, fmt_num, now_wib, prayer_times_today

GOLDEN: dict[str, Any] = json.loads((FIXTURES / "app_engine_golden.json").read_text("utf-8"))


def golden(city: str, day: str) -> dict[str, str]:
    return next(r for r in GOLDEN["prayer"] if r["city"] == city and r["date"] == day)


# ── Fact / fmt_num / registry constants ───────────────────────────────────────


def test_fact_as_prompt() -> None:
    assert Fact("k", "Label", ("1", "2")).as_prompt() == "- k: Label → 1, 2"
    assert Fact("k", "Label", ("1",), notes=("a", "b")).as_prompt() == "- k: Label → 1 (wajib sertakan: a; b)"


def test_fact_defaults() -> None:
    f = Fact("k", "L", ("1",))
    assert f.detail == "" and f.notes == ()


@pytest.mark.parametrize(
    ("x", "dec", "out"),
    [
        (6236, 0, "6.236"),
        (4.0266, 2, "4,03"),
        (1234567.891, 2, "1.234.567,89"),
        (2.5, 1, "2,5"),
        (0, 0, "0"),
        (999, 0, "999"),
        (1000, 0, "1.000"),
        (4.0266, 0, "4"),
    ],
)
def test_fmt_num(x: float, dec: int, out: str) -> None:
    assert fmt_num(x, dec) == out


def test_fmt_num_default_decimals() -> None:
    assert fmt_num(12345.6) == "12.346"


def test_constants_registry() -> None:
    assert CONSTANTS == {
        "mushaf_pages": ("604", "Mushaf Standar Indonesia (Kemenag) dan Mushaf Madinah: 604 halaman"),
        "juz": ("30", "Pembagian Al-Qur'an menjadi 30 juz"),
        "salat_wajib": ("5", "Lima salat fardu"),
        "zakat_rate_pct": ("2,5", "Kadar zakat mal 2,5%"),
    }


def test_extra_places() -> None:
    assert set(EXTRA_PLACES) == {"Sabang", "Merauke"}
    s, m = EXTRA_PLACES["Sabang"], EXTRA_PLACES["Merauke"]
    assert (s.name, s.province, s.latitude, s.longitude, s.tz) == ("Sabang", "Aceh", 5.8926, 95.3238, IndonesiaTZ.WIB)
    assert (m.name, m.province, m.latitude, m.longitude, m.tz) == ("Merauke", "Papua Selatan", -8.4932, 140.4018, IndonesiaTZ.WIT)


@pytest.mark.parametrize("key", sorted(CONSTANTS))
def test_const_facts(sources: Sources, key: str) -> None:
    value, label = CONSTANTS[key]
    assert compute(sources, f"const:{key}") == Fact(f"const:{key}", label, (value,))


# ── quran ─────────────────────────────────────────────────────────────────────


def test_longest_ayah(sources: Sources) -> None:
    assert compute(sources, "quran:longest_ayah") == Fact("quran:longest_ayah", "Ayat terpanjang (jumlah kata): Al-Baqarah ayat 282", ("282", "128"), "2:282")


def test_shortest_surahs(sources: Sources) -> None:
    assert compute(sources, "quran:shortest_surahs") == Fact("quran:shortest_surahs", "Surah terpendek (3 ayat): Al-'Asr, Al-Kausar, An-Nasr", ("3",))


def test_totals(sources: Sources) -> None:
    assert compute(sources, "quran:totals") == Fact("quran:totals", "Jumlah surah dan ayat (Tanzil)", ("114", "6.236"))


@pytest.mark.parametrize(("n", "count", "name"), [(1, "7", "Al-Fatihah"), (2, "286", "Al-Baqarah"), (114, "6", "An-Nas")])
def test_surah_ayahs(sources: Sources, n: int, count: str, name: str) -> None:
    assert compute(sources, f"quran:surah_ayahs:{n}") == Fact(f"quran:surah_ayahs:{n}", f"Jumlah ayat {name}", (count, str(n)))


@pytest.mark.parametrize(
    ("n", "count", "name"),
    [
        (27, "2", "An-Naml"),  # opening + 27:30
        (9, "0", "At-Taubah"),  # no opening Bismillah
        (1, "1", "Al-Fatihah"),  # the Bismillah is ayat 1 itself, not a heading
        (2, "1", "Al-Baqarah"),
        (114, "1", "An-Nas"),
    ],
)
def test_basmala_count(sources: Sources, n: int, count: str, name: str) -> None:
    assert compute(sources, f"quran:basmala_count:{n}") == Fact(f"quran:basmala_count:{n}", f"Jumlah Bismillah di {name} (pembuka + di dalam ayat)", (count,))


@pytest.mark.parametrize(
    ("key", "days", "values", "detail"),
    [
        ("quran:khatam_per_prayer:30", 30, ("604", "30", "5", "4", "4,03"), "604 ÷ 30 ÷ 5 = 4.03"),
        ("quran:khatam_per_prayer", 30, ("604", "30", "5", "4", "4,03"), "604 ÷ 30 ÷ 5 = 4.03"),
        ("quran:khatam_per_prayer:60", 60, ("604", "60", "5", "2", "2,01"), "604 ÷ 60 ÷ 5 = 2.01"),
        ("quran:khatam_per_prayer:11", 11, ("604", "11", "5", "11", "10,98"), "604 ÷ 11 ÷ 5 = 10.98"),  # rounds, not floors
        ("quran:khatam_per_prayer:1", 1, ("604", "1", "5", "121", "120,80"), "604 ÷ 1 ÷ 5 = 120.80"),
    ],
)
def test_khatam_per_prayer(sources: Sources, key: str, days: int, values: tuple[str, ...], detail: str) -> None:
    assert compute(sources, key) == Fact(f"quran:khatam_per_prayer:{days}", f"Halaman per salat agar khatam dalam {days} hari", values, detail)


# ── prayer / compare / extremes ───────────────────────────────────────────────


GOLDEN_SAMPLE = GOLDEN["prayer"][::173]  # ~30 city/date rows across 2026–2028
GOLDEN_TZ = {c["name"]: c["tz"] for c in GOLDEN["cities"]}


@pytest.mark.parametrize("row", GOLDEN_SAMPLE, ids=lambda r: f"{r['city']}-{r['date']}")
def test_prayer_fact_matches_app_engine(sources: Sources, row: dict[str, str]) -> None:
    city, day = row["city"], row["date"]
    for prayer in Prayer:
        f = compute(sources, f"prayer:{city}:{day}:{prayer.value}")
        assert f == Fact(f"prayer:{city}:{day}:{prayer.value}", f"{prayer.value} {city} {day} ({GOLDEN_TZ[city]})", (row[prayer.value],))


def test_golden_sample_covers_several_cities() -> None:
    assert len({r["city"] for r in GOLDEN_SAMPLE}) >= 10


def test_prayer_fact_jakarta_subuh(sources: Sources) -> None:
    assert compute(sources, "prayer:Jakarta:2026-11-26:Subuh").values == ("04:05",)


def test_prayer_fact_extra_places(sources: Sources) -> None:
    assert compute(sources, "prayer:Sabang:2026-11-26:Subuh") == Fact("prayer:Sabang:2026-11-26:Subuh", "Subuh Sabang 2026-11-26 (WIB)", ("05:11",))
    assert compute(sources, "prayer:Merauke:2026-11-26:Isya") == Fact("prayer:Merauke:2026-11-26:Isya", "Isya Merauke 2026-11-26 (WIT)", ("19:00",))


def test_compare_app_cities_matches_app_engine(sources: Sources) -> None:
    # Banda Aceh Subuh 05:11 WIB (= 07:11 WIT) vs Jayapura 03:56 WIT: 195 real minutes apart.
    ba, jp = golden("Banda Aceh", "2026-11-26")["Subuh"], golden("Jayapura", "2026-11-26")["Subuh"]
    assert (ba, jp) == ("05:11", "03:56")
    assert compute(sources, "compare:Banda Aceh:Jayapura:2026-11-26:Subuh") == Fact(
        "compare:Banda Aceh:Jayapura:2026-11-26:Subuh",
        "Subuh Banda Aceh 05:11 WIB vs Jayapura 03:56 WIT; selisih waktu nyata",
        ("05:11", "03:56", "195", "3", "15", "WIB", "WIT"),
        "195 menit = 3 jam 15 menit",
    )


def test_compare_sabang_merauke_either_order(sources: Sources) -> None:
    a = compute(sources, "compare:Sabang:Merauke:2026-11-26:Maghrib")
    assert a == Fact(
        "compare:Sabang:Merauke:2026-11-26:Maghrib",
        "Maghrib Sabang 18:22 WIB vs Merauke 17:44 WIT; selisih waktu nyata",
        ("18:22", "17:44", "158", "2", "38", "WIB", "WIT"),
        "158 menit = 2 jam 38 menit",
    )
    b = compute(sources, "compare:Merauke:Sabang:2026-11-26:Maghrib")
    assert b.values == ("17:44", "18:22", "158", "2", "38", "WIT", "WIB")  # the difference is never negative


def test_compare_same_city(sources: Sources) -> None:
    assert compute(sources, "compare:Jakarta:Jakarta:2026-11-26:Subuh").values == ("04:05", "04:05", "0", "0", "0", "WIB", "WIB")


@pytest.mark.parametrize(
    ("prayer", "first", "last", "diff", "h", "m"),
    [("Subuh", "Merauke", "Banda Aceh", 205, 3, 25), ("Maghrib", "Jayapura", "Banda Aceh", 169, 2, 49), ("Isya", "Jayapura", "Banda Aceh", 169, 2, 49)],
)
def test_extremes(sources: Sources, prayer: str, first: str, last: str, diff: int, h: int, m: int) -> None:
    assert compute(sources, f"extremes:2026-11-26:{prayer}") == Fact(
        f"extremes:2026-11-26:{prayer}",
        f"{prayer} paling awal ({first}) dan paling akhir ({last}) di Indonesia",
        (str(diff), str(h), str(m)),
        f"{first} → {last}: {diff} menit",
    )


def test_extremes_consistent_with_app_engine(sources: Sources) -> None:
    # Jayapura → Banda Aceh Maghrib on 2026-11-26 from the app's own times: 17:34 WIT vs 18:23 WIB.
    jp, ba = golden("Jayapura", "2026-11-26")["Maghrib"], golden("Banda Aceh", "2026-11-26")["Maghrib"]
    jp_utc = datetime.fromisoformat(f"2026-11-26T{jp}") - timedelta(hours=9)
    ba_utc = datetime.fromisoformat(f"2026-11-26T{ba}") - timedelta(hours=7)
    approx = int((ba_utc - jp_utc).total_seconds() // 60)
    assert abs(approx - int(compute(sources, "extremes:2026-11-26:Maghrib").values[0])) <= 1


def test_prayer_times_today(sources: Sources) -> None:
    g = golden("Jakarta", "2026-11-26")
    assert prayer_times_today(sources, "Jakarta", date(2026, 11, 26)) == {p.value: g[p.value] for p in Prayer}
    assert list(prayer_times_today(sources, "Jayapura", date(2026, 11, 26))) == [p.value for p in Prayer]
    assert prayer_times_today(sources, "Merauke", date(2026, 11, 26))["Isya"] == "19:00"


def test_prayer_times_today_unknown_place(sources: Sources) -> None:
    with pytest.raises(UnknownFact) as exc:
        prayer_times_today(sources, "Atlantis", TODAY)
    assert exc.value.args == ("Unknown place 'Atlantis'",)


def test_place_lookup(sources: Sources) -> None:
    assert F._place(sources, "Banda Aceh").name == "Banda Aceh"
    assert F._place(sources, "Sabang") is EXTRA_PLACES["Sabang"]
    assert F._place(sources, "Jakarta") is sources.cities[0]


# ── hijri ─────────────────────────────────────────────────────────────────────


def test_hijri_today_not_near_boundary(sources: Sources) -> None:
    assert compute(sources, "hijri:today:2026-11-26") == Fact("hijri:today:2026-11-26", "Tanggal Hijriah 2026-11-26", ("16", "1448"), "16 Jumadil Akhir 1448 H")


@pytest.mark.parametrize(
    ("day", "values", "detail"), [("2026-12-08", ("28", "1448"), "28 Jumadil Akhir 1448 H"), ("2026-12-10", ("1", "1448"), "1 Rajab 1448 H")]
)
def test_hijri_today_near_boundary_carries_isbat_note(sources: Sources, day: str, values: tuple[str, str], detail: str) -> None:
    assert compute(sources, f"hijri:today:{day}") == Fact(f"hijri:today:{day}", f"Tanggal Hijriah {day}", values, detail, (ISBAT_NOTE,))


def test_hijri_uses_date_in_key_not_today(sources: Sources) -> None:
    assert compute(sources, "hijri:today:2026-11-26", date(2030, 1, 1)).values == ("16", "1448")


def test_hijri_matches_app_engine(sources: Sources) -> None:
    for row in GOLDEN["hijri"][::97]:
        f = compute(sources, f"hijri:today:{row['date']}")
        assert f.values == (str(row["d"]), str(row["y"]))
        assert f.notes == ((ISBAT_NOTE,) if row["near"] else ())


@pytest.mark.parametrize(
    ("day", "dates"),
    [
        ("2026-11-25", ("2026-11-23", "2026-11-24", "2026-11-25")),  # 15 Jumadil Akhir: this month
        ("2026-11-26", ("2026-12-22", "2026-12-23", "2026-12-24")),  # 16 Jumadil Akhir: next month (Rajab)
        ("2026-12-19", ("2026-12-22", "2026-12-23", "2026-12-24")),  # 10 Rajab
        ("2027-05-21", ("2027-05-19", "2027-05-20", "2027-05-21")),  # 15 Zulhijjah 1448: this month
        ("2027-05-22", ("2027-06-18", "2027-06-19", "2027-06-20")),  # 16 Zulhijjah 1448 → Muharram 1449
    ],
)
def test_ayyamul_bidh(sources: Sources, day: str, dates: tuple[str, str, str]) -> None:
    f = compute(sources, f"hijri:ayyamul_bidh:{day}")
    assert f == Fact(
        f"hijri:ayyamul_bidh:{day}",
        "Ayyamul Bidh (13–15) bulan ini, perkiraan kalender",
        (*(str(date.fromisoformat(d).day) for d in dates), "13", "14", "15"),
        ", ".join(dates),
    )


def test_ayyamul_bidh_rollover_year(sources: Sources, monkeypatch: pytest.MonkeyPatch) -> None:
    # Zulhijjah (12) → Muharram (1) of the next year; any other month keeps the year.
    from samawi_bot.sources.hijri import IslamicDate

    seen: list[tuple[int, int]] = []

    def fake_bidh(year: int, month: int) -> tuple[date, date, date]:
        seen.append((year, month))
        return date(2000, 1, 13), date(2000, 1, 14), date(2000, 1, 15)

    monkeypatch.setattr(F, "ayyamul_bidh", fake_bidh)
    for m, d, expect in [(12, 16, (1449, 1)), (12, 15, (1448, 12)), (1, 16, (1448, 2)), (11, 20, (1448, 12)), (6, 30, (1448, 7))]:
        monkeypatch.setattr(F, "islamic_date", lambda today, m=m, d=d: IslamicDate(1448, m, d, False))
        compute(sources, "hijri:ayyamul_bidh:2026-01-01")
        assert seen.pop() == expect


@pytest.mark.parametrize(
    ("day", "n", "year", "start"),
    [
        ("2026-11-26", 74, 1448, "2027-02-08"),
        ("2027-02-07", 1, 1448, "2027-02-08"),
        ("2027-02-08", 0, 1448, "2027-02-08"),  # the (calculated) first day itself
        ("2027-02-09", 353, 1449, "2028-01-28"),  # just after: next year's Ramadan
        ("2027-03-01", 333, 1449, "2028-01-28"),
    ],
)
def test_days_to_ramadan(sources: Sources, day: str, n: int, year: int, start: str) -> None:
    assert compute(sources, f"hijri:days_to_ramadan:{day}") == Fact(
        f"hijri:days_to_ramadan:{day}", f"Hari menuju 1 Ramadan {year} H", (str(n), str(year)), start, (ISBAT_NOTE,)
    )


def test_isbat_note_wording() -> None:
    assert ISBAT_NOTE == "perkiraan, menunggu sidang isbat"


# ── principles ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("key", sorted(F.PRINCIPLES))
def test_principle_facts(sources: Sources, key: str) -> None:
    text, values = F.PRINCIPLES[key]
    assert compute(sources, f"prinsip:{key}") == Fact(f"prinsip:{key}", text, values)


@pytest.mark.parametrize("key", ["prinsip", "prinsip:nope", "prinsip:isbat:x"])
def test_unknown_principle(sources: Sources, key: str) -> None:
    with pytest.raises(UnknownFact) as exc:
        compute(sources, key)
    assert exc.value.args == (key,)


# ── unknown facts ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "msg"),
    [
        ("", ""),
        ("foo:bar", "foo:bar"),
        ("quran", "quran"),
        ("quran:nope", "quran:nope"),
        ("quran:surah_ayahs", "quran:surah_ayahs: list index out of range"),
        ("quran:surah_ayahs:115", "quran:surah_ayahs:115: 115"),
        ("quran:surah_ayahs:0", "quran:surah_ayahs:0: 0"),
        ("quran:surah_ayahs:x", "quran:surah_ayahs:x: invalid literal for int() with base 10: 'x'"),
        ("quran:basmala_count:-1", "quran:basmala_count:-1: -1"),
        ("quran:basmala_count", "quran:basmala_count: list index out of range"),
        ("quran:khatam_per_prayer:0", "quran:khatam_per_prayer:0: days must be at least 1"),
        ("quran:khatam_per_prayer:-3", "quran:khatam_per_prayer:-3: days must be at least 1"),
        ("quran:khatam_per_prayer:x", "quran:khatam_per_prayer:x: invalid literal for int() with base 10: 'x'"),
        ("prayer:Atlantis:2026-11-26:Subuh", "Unknown place 'Atlantis'"),
        ("prayer:jakarta:2026-11-26:Subuh", "Unknown place 'jakarta'"),
        ("prayer:Jakarta:2026-13-01:Subuh", "prayer:Jakarta:2026-13-01:Subuh: month must be in 1..12"),
        ("prayer:Jakarta:26-11-2026:Subuh", "prayer:Jakarta:26-11-2026:Subuh: Invalid isoformat string: '26-11-2026'"),
        ("prayer:Jakarta:2026-11-26:Lunch", "prayer:Jakarta:2026-11-26:Lunch: 'Lunch' is not a valid Prayer"),
        ("prayer:Jakarta:2026-11-26:subuh", "prayer:Jakarta:2026-11-26:subuh: 'subuh' is not a valid Prayer"),
        ("prayer:Jakarta:2026-11-26", "prayer:Jakarta:2026-11-26"),
        ("prayer:Jakarta:2026-11-26:Subuh:x", "prayer:Jakarta:2026-11-26:Subuh:x"),
        ("prayer:Jakarta:9999-12-31:Subuh", "prayer:Jakarta:9999-12-31:Subuh: date value out of range"),
        ("compare:Sabang:2026-11-26:Subuh", "compare:Sabang:2026-11-26:Subuh"),
        ("compare:Sabang:Merauke:Jakarta:2026-11-26:Subuh", "compare:Sabang:Merauke:Jakarta:2026-11-26:Subuh"),
        ("compare:Sabang:Atlantis:2026-11-26:Subuh", "Unknown place 'Atlantis'"),
        ("compare:Atlantis:Sabang:2026-11-26:Subuh", "Unknown place 'Atlantis'"),
        ("extremes:2026-11-26", "extremes:2026-11-26"),
        ("extremes:2026-11-26:Subuh:x", "extremes:2026-11-26:Subuh:x"),
        ("extremes:2026-11-26:Nope", "extremes:2026-11-26:Nope: 'Nope' is not a valid Prayer"),
        ("extremes:2026-02-30:Subuh", "extremes:2026-02-30:Subuh: day is out of range for month"),
        ("hijri:today", "hijri:today"),
        ("hijri:today:2026-11-26:x", "hijri:today:2026-11-26:x"),
        ("hijri:tomorrow:2026-11-26", "hijri:tomorrow"),
        ("hijri:today:bad", "hijri:today:bad: Invalid isoformat string: 'bad'"),
        ("hijri:today:1900-01-01", "hijri:today:1900-01-01: date must be in '1924-08-01'-'2077-11-16', got '1900-01-01'"),
        ("hijri:days_to_ramadan:2077-11-10", "hijri:days_to_ramadan:2077-11-10: year must be in 1343-1500, got '1501'"),
        ("const", "const"),
        ("const:nope", "const:nope"),
        ("const:juz:1", "const:juz:1"),
    ],
)
def test_unknown_fact(sources: Sources, key: str, msg: str) -> None:
    with pytest.raises(UnknownFact) as exc:
        compute(sources, key, TODAY)
    assert exc.value.args == (msg,)


def test_unknown_fact_is_a_key_error() -> None:
    assert issubclass(UnknownFact, KeyError)


def test_unknown_fact_chains_cause(sources: Sources) -> None:
    with pytest.raises(UnknownFact) as exc:
        compute(sources, "quran:surah_ayahs:x")
    assert isinstance(exc.value.__cause__, ValueError)


def test_compute_today_defaults_to_none(sources: Sources) -> None:
    assert compute(sources, "quran:totals").values == ("114", "6.236")


# ── fact packs ────────────────────────────────────────────────────────────────

D = "2026-11-26"


@pytest.mark.parametrize(
    ("pillar", "keys"),
    [
        (
            "langit_waktu_salat",
            [
                f"hijri:today:{D}",
                f"compare:Sabang:Merauke:{D}:Subuh",
                f"compare:Sabang:Merauke:{D}:Maghrib",
                f"extremes:{D}:Subuh",
                f"extremes:{D}:Maghrib",
                f"extremes:{D}:Isya",
                *[f"prayer:{c}:{D}:{p.value}" for c in ("Jakarta", "Banda Aceh", "Jayapura") for p in Prayer],
            ],
        ),
        (
            "fakta_quran",
            [
                f"hijri:today:{D}",
                "quran:longest_ayah",
                "quran:shortest_surahs",
                "quran:totals",
                "quran:basmala_count:27",
                "quran:basmala_count:9",
                "quran:khatam_per_prayer:30",
                "const:mushaf_pages",
                "const:juz",
            ],
        ),
        (
            "hitung_ibadah",
            [f"hijri:today:{D}", "quran:khatam_per_prayer:30", "quran:khatam_per_prayer:60", "const:mushaf_pages", "const:zakat_rate_pct", "const:juz"],
        ),
        ("kalender_hijriah", [f"hijri:today:{D}", f"hijri:ayyamul_bidh:{D}", f"hijri:days_to_ramadan:{D}", "prinsip:hari_hijriah", "prinsip:isbat"]),
        ("sunnah_terlupa", [f"hijri:today:{D}", f"hijri:ayyamul_bidh:{D}", "prinsip:hari_hijriah"]),
        ("doa_dzikir", [f"hijri:today:{D}"]),
        ("musafir", [f"hijri:today:{D}", "prinsip:zona_waktu", "prinsip:posisi_matahari"]),
        ("sejarah_kisah", [f"hijri:today:{D}"]),
    ],
)
def test_fact_pack(sources: Sources, pillar: str, keys: list[str]) -> None:
    pack = fact_pack(sources, pillar, TODAY)
    if pillar == "langit_waktu_salat":
        # The sky pillar also carries principle, annual and cross facts (tested in test_verify_facts_principles.py).
        assert [f.key for f in pack][: len(keys)] == keys
        assert all(f.key.split(":")[0] in ("prinsip", "annual", "cross") for f in pack[len(keys) :])
        pack = pack[: len(keys)]
    assert [f.key for f in pack] == keys
    assert pack == [compute(sources, k, TODAY) for k in keys]


def test_fact_pack_values_for_langit(sources: Sources) -> None:
    pack = {f.key: f for f in fact_pack(sources, "langit_waktu_salat", TODAY)}
    assert pack[f"prayer:Jakarta:{D}:Subuh"].values == ("04:05",)
    assert pack[f"compare:Sabang:Merauke:{D}:Maghrib"].values[2] == "158"


def test_fact_pack_kalender_carries_isbat_note(sources: Sources) -> None:
    pack = {f.key: f for f in fact_pack(sources, "kalender_hijriah", TODAY)}
    ramadan = pack[f"hijri:days_to_ramadan:{D}"]
    assert ramadan.notes == (ISBAT_NOTE,)
    assert "wajib sertakan: perkiraan, menunggu sidang isbat" in ramadan.as_prompt()


# ── now_wib ───────────────────────────────────────────────────────────────────


def test_now_wib() -> None:
    n = now_wib()
    assert n.utcoffset() == timedelta(hours=7)
    assert abs((n - datetime.now(UTC)).total_seconds()) < 5
