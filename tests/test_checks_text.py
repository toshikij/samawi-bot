"""checks/text.py: the normaliser every pattern check relies on, and the platform counters."""

from __future__ import annotations

import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from samawi_bot.checks import text as T
from samawi_bot.checks.text import (
    ACRONYMS,
    canonical_number,
    count_chars,
    emoji_count,
    fold,
    is_shouting,
    normalize,
    numbers,
    strip_invisible,
    utf8_len,
    variants,
    word_count,
)

INVISIBLES = [
    "\u200b",  # zero width space
    "\u200c",  # ZWNJ
    "\u200d",  # ZWJ
    "\u200e",  # LRM
    "\u200f",  # RLM
    "\u202a",
    "\u202b",
    "\u202c",
    "\u202d",
    "\u202e",  # RLO
    "\u2060",  # word joiner
    "\u2061",
    "\u2062",
    "\u2063",
    "\u2064",
    "\u2066",  # LRI (bidi isolates)
    "\u2067",
    "\u2068",
    "\u2069",
    "\u180e",  # Mongolian vowel separator
    "\ufeff",  # BOM
    "\xad",  # soft hyphen
]


# ── strip_invisible / fold ────────────────────────────────────────────────────


@pytest.mark.parametrize("ch", INVISIBLES)
def test_strip_invisible_removes_every_invisible(ch: str) -> None:
    assert strip_invisible(f"vi{ch}ral") == "viral"
    assert normalize(f"di{ch}ja{ch}min") == "dijamin"


def test_strip_invisible_keeps_visible_text() -> None:
    assert strip_invisible("Al-Baqarah 282 ✓ ب") == "Al-Baqarah 282 ✓ ب"
    # Neighbours of the ranges are not stripped.
    assert strip_invisible("a\u200ab\u2010c\u2070d") == "a\u200ab\u2010c\u2070d"


def test_fold_case_diacritics_fullwidth_degree() -> None:
    assert fold("VIRÁL") == "viral"
    assert fold("ｖｉｒａｌ") == "viral"
    assert fold("Ｖ\u200bＩＲＡＬ") == "viral"
    assert fold("Raḍiyallāhu") == "radiyallahu"
    assert fold("18°") == "18 derajat "
    assert fold("ＡＢ") == "ab"


# ── normalize / variants ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Muslim  Pro", "muslim pro"),
        ("u m m a", "umma"),
        ("U.M.M.A", "umma"),
        ("MuslimPro", "muslimpro"),
        ("a umma b", "a umma b"),
        ("x y zz w v", "xy zz wv"),
        ("z", "z"),
        ("ab c", "ab c"),
        ("c ab", "c ab"),
        ("5 waktu", "5 waktu"),
        ("99% orang", "99% orang"),
        ("Hello, World!", "hello world"),
        ("   ", ""),
        ("", ""),
        ("18° vs 20°", "18 derajat vs 20 derajat"),
        ("I'jaz", "i jaz"),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "plain", "leet"),
    [
        ("dij4min", "dij4min", "dijamin"),
        ("v1r4l", "v1r4l", "viral"),
        ("V1R4L", "v1r4l", "viral"),
        ("tr14l", "tr14l", "trial"),
        ("5ain5", "5ain5", "sains"),
        ("@zab", "zab", "azab"),
        ("t3s7", "t3s7", "test"),
        ("b0s", "b0s", "bos"),
        ("$aya", "aya", "saya"),
        ("h!dup", "h dup", "hidup"),
        ("V1r4l!", "v1r4l", "viral"),
        ("dij4min!!", "dij4min", "dijamin"),
    ],
)
def test_normalize_leet_maps_only_inside_words(raw: str, plain: str, leet: str) -> None:
    assert normalize(raw) == plain
    assert normalize(raw, leet=True) == leet


def test_leet_trailing_bang_is_punctuation_not_a_letter() -> None:
    assert normalize("ok!5x", leet=True) == "ok sx"
    assert normalize("ok!!", leet=True) == "ok"
    assert normalize("ok!x", leet=True) == "okix"


def test_leet_leaves_free_standing_numbers_alone() -> None:
    assert normalize("99 % orang, 5 waktu, 2026", leet=True) == "99 % orang 5 waktu 2026"
    assert normalize("5waktu", leet=True) == "swaktu"


def test_variants_single_when_no_leet() -> None:
    assert variants("Ayat terpanjang") == ["ayat terpanjang"]


def test_variants_plain_then_leet() -> None:
    assert variants("dij4min!") == ["dij4min", "dijamin"]
    assert variants("Dijamin!!!") == ["dijamin"]
    assert variants("v1r4l! sekarang") == ["v1r4l sekarang", "viral sekarang"]


# ── counting ──────────────────────────────────────────────────────────────────


def test_count_chars_counts_code_points() -> None:
    assert count_chars("abc") == 3
    assert count_chars("🙂") == 1
    assert count_chars("بِسْمِ") == 6  # 3 letters + 3 harakat
    assert count_chars("❤️") == 2  # heart + variation selector
    assert count_chars("") == 0


def test_utf8_len_counts_bytes() -> None:
    assert utf8_len("abc") == 3
    assert utf8_len("ب") == 2
    assert utf8_len("✓") == 3
    assert utf8_len("🙂") == 4
    assert utf8_len("\ud800") == 3  # lone surrogate: counted, never a crash


def test_word_count_ignores_invisibles() -> None:
    assert word_count("satu dua  tiga\n empat") == 4
    assert word_count("satu \u200b dua") == 2
    assert word_count("") == 0
    assert word_count("\u200b") == 0


# ── shouting ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["WAJIB TAHU", "Ini WAJIB TAHU ya", "WAJIB\u200b TAHU", "SEMUA ORANG HARUS", "WAJÍB TÁHU", "MUSLIM  PRO!"],
)
def test_is_shouting_true(text: str) -> None:
    assert is_shouting(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "WAJIB tahu",
        "Subuh 04:05 WIB, Dzuhur WITA, Isya WIT",
        "Komentar SAMAWI ya",
        "SAMAWI WIB WITA WIT SAW SWT HR QS MUI NU UTC",
        "QS MUI",
        "WAJIB WIB TAHU",  # acronym breaks the run
        "OK GO",  # 2-letter words are not counted
        "AB12 CD34",
        "",
        "Wajib Tahu",
    ],
)
def test_is_shouting_false(text: str) -> None:
    assert is_shouting(text) is False


def test_acronym_list_is_exact() -> None:
    assert frozenset({"WIB", "WITA", "WIT", "SAW", "SWT", "HR", "QS", "MUI", "NU", "UTC", "SAMAWI"}) == ACRONYMS


def test_acronyms_only_exempt_when_listed() -> None:
    assert is_shouting("SAMAWI KEREN") is False  # KEREN alone: run of 1
    assert is_shouting("KEREN SEKALI") is True
    assert is_shouting("WIB KEREN SEKALI") is True


# ── emoji ─────────────────────────────────────────────────────────────────────


def test_emoji_count() -> None:
    assert emoji_count("🙏🌙✨") == 3
    assert emoji_count("🙏🌙✨☀") == 4
    assert emoji_count("🀄🃏") == 2  # U+1F000 block
    assert emoji_count("🫶") == 1  # U+1FAF6, top of the range
    assert emoji_count("❤️") == 1
    assert emoji_count("no emoji ✓") == 1  # U+2713 is in Dingbats
    assert emoji_count("abc ب 123") == 0


# ── numbers ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("tok", "canon"),
    [
        ("6.236", "6236"),
        ("1.234.567", "1234567"),
        ("4,03", "4.03"),
        ("2,5", "2.5"),
        ("4.03", "4.03"),
        ("04:05", "04:05"),
        ("4:05", "4:05"),
        ("007", "7"),
        ("000", "0"),
        ("0", "0"),
        ("282", "282"),
        ("1,2,3", "1,2,3"),
        ("12.34.5", "12.34.5"),
        ("1234.567", "1234.567"),
        ("1.23", "1.23"),
    ],
)
def test_canonical_number(tok: str, canon: str) -> None:
    assert canonical_number(tok) == canon


def test_numbers_indonesian_formats() -> None:
    assert numbers("Al-Qur'an punya 6.236 ayat, 4,03 halaman per salat, Subuh 04:05 WIB.") == ["6236", "4.03", "04:05"]
    assert numbers("Al-Baqarah 282 (QS. 2:282)") == ["282", "2:282"]
    assert numbers("tidak ada angka") == []
    assert numbers("2\u200b82") == ["282"]
    assert numbers("Rp19.000") == ["19000"]


def test_off_by_one_and_rounding_are_different_numbers() -> None:
    assert numbers("283") != numbers("282")
    assert numbers("4,1") == ["4.1"]
    assert numbers("4,1") != numbers("4,03")
    assert numbers("6.236") == numbers("6236")
    assert numbers("4,03") == numbers("4.03")


# ── properties ────────────────────────────────────────────────────────────────

_text = st.text(
    alphabet=st.one_of(
        st.characters(),
        st.sampled_from(list("0123456789.,:%°@$!") + INVISIBLES + ["ｖ", "Á", "ب", "🙂", " "]),
    ),
    max_size=80,
)
_NORMAL = re.compile(r"^[0-9a-z% ]*$")


@given(_text)
def test_normalize_never_crashes_and_is_idempotent(s: str) -> None:
    for leet in (False, True):
        n = normalize(s, leet=leet)
        assert _NORMAL.match(n)
        assert "  " not in n
        assert n == n.strip()
        assert normalize(n, leet=leet) == n


@given(_text)
def test_variants_property(s: str) -> None:
    v = variants(s)
    assert v[0] == normalize(s)
    assert 1 <= len(v) <= 2
    assert v[-1] == normalize(s, leet=True)


@given(_text)
def test_numbers_never_crash_and_are_canonical(s: str) -> None:
    for n in numbers(s):
        assert n
        # A canonical token never starts with a leading zero unless it is a time or "0".
        if ":" not in n and "." not in n and "," not in n:
            assert n == "0" or not n.startswith("0")


@given(st.from_regex(r"\d{1,6}([.,:]\d{1,4}){0,3}", fullmatch=True))
def test_canonical_number_never_crashes(tok: str) -> None:
    c = canonical_number(tok)
    assert c
    assert set(c) <= set("0123456789.,:") or not c.isascii()


@given(st.integers(min_value=0, max_value=10**9))
def test_canonical_number_integer_formats_agree(n: int) -> None:
    indo = f"{n:,}".replace(",", ".")
    assert canonical_number(indo) == str(n)
    assert canonical_number(str(n)) == str(n)
    assert numbers(f"ada {indo} ayat") == [str(n)]


@given(st.integers(min_value=0, max_value=10**6), st.integers(min_value=1, max_value=99))
def test_canonical_number_decimal_comma_equals_decimal_point(whole: int, frac: int) -> None:
    f = f"{frac:02d}"
    assert canonical_number(f"{whole},{f}") == canonical_number(f"{whole}.{f}") == f"{whole}.{f}"


@given(st.integers(min_value=0, max_value=10**6), st.integers(min_value=1, max_value=99))
def test_canonical_number_idempotent_on_its_output(whole: int, frac: int) -> None:
    # Idempotent for everything numbers() can produce except a 3-digit decimal fraction, which
    # canonicalises to the thousands form ("4,030" -> "4.030" -> "4030"). The checks only ever
    # canonicalise once, so this is documented rather than relied on.
    for tok in (str(whole), f"{whole},{frac:02d}", f"{whole:,}".replace(",", ".")):
        c = canonical_number(tok)
        assert canonical_number(c) == c
    assert canonical_number(canonical_number("4,030")) == "4030"


@given(_text)
def test_counters_never_crash(s: str) -> None:
    assert count_chars(s) == len(s)
    assert utf8_len(s) >= count_chars(s)
    assert word_count(s) >= 0
    assert emoji_count(s) >= 0
    assert isinstance(is_shouting(s), bool)
    assert strip_invisible(strip_invisible(s)) == strip_invisible(s)
    assert not T.ZERO_WIDTH.search(strip_invisible(s))
