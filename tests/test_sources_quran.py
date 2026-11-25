"""Quran text and reference parsing (spec §6.1-6.2, §10.3 "Quran and references")."""

from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import SNAPSHOT
from samawi_bot.sources import Sources
from samawi_bot.sources.quran import (
    MAX_RANGE,
    TOTAL_AYAHS,
    TOTAL_SURAHS,
    AyahRange,
    AyahRef,
    InvalidReference,
    Quran,
    Surah,
    from_parts,
    nfc,
    parse_reference,
    parse_surahs,
    parse_tanzil,
    skeleton,
)
from samawi_bot.sources.snapshot import QURAN_TEXT, SURAHS, TRANSLATION_KEMENAG, Snapshot


@pytest.fixture(scope="module")
def quran(sources: Sources) -> Quran:
    return sources.quran


@pytest.fixture(scope="module")
def raw_parts() -> tuple[dict[int, list[str]], dict[str, dict[str, str]], list[Surah]]:
    snap = Snapshot(SNAPSHOT)
    return (
        parse_tanzil(snap.read_text(QURAN_TEXT)),
        json.loads(snap.read_text(TRANSLATION_KEMENAG)),
        parse_surahs(snap.read_text(SURAHS)),
    )


# ── Loading ──────────────────────────────────────────────────────────────────────────────────


def test_load_shape(quran: Quran) -> None:
    assert len(quran.surahs) == TOTAL_SURAHS
    assert len(quran.all_refs()) == TOTAL_AYAHS
    assert quran.all_refs()[0] == AyahRef(1, 1)
    assert quran.all_refs()[-1] == AyahRef(114, 6)
    fatihah = quran.surahs[1]
    assert (fatihah.number, fatihah.latin, fatihah.ayah_count, fatihah.meccan) == (1, "Al-Fatihah", 7, True)
    assert quran.surahs[2].meccan is False
    assert quran.surahs[2].ayah_count == 286


def test_parse_surahs_rejects_incomplete() -> None:
    with pytest.raises(ValueError, match="parsed to 0 surahs"):
        parse_surahs("")
    one = 'Surah( number: 1, name: "الفاتحة", latinName: "Al-Fatihah", meaning: "Pembuka", ayahCount: 7, isMeccan: true'
    with pytest.raises(ValueError, match="parsed to 1 surahs"):
        parse_surahs(one)


def test_parse_tanzil_errors() -> None:
    with pytest.raises(ValueError, match="Unexpected Tanzil line"):
        parse_tanzil("1|1|text\nnot a verse line")
    with pytest.raises(ValueError, match="Tanzil gap at 1:3"):
        parse_tanzil("1|1|a\n1|3|c")
    with pytest.raises(ValueError, match="Tanzil gap at 2:2"):
        parse_tanzil("1|1|a\n2|2|b")
    # Comments and blank lines are skipped, but an incomplete text is still rejected.
    with pytest.raises(ValueError, match="1 surahs / 2 ayahs"):
        parse_tanzil("# header\n\n1|1|a\n1|2|b\n# footer\n")


def test_parse_tanzil_rejects_wrong_total(raw_parts) -> None:
    arabic, _, _ = raw_parts
    lines = [f"{s}|{i + 1}|{t}" for s, vs in arabic.items() for i, t in enumerate(vs)]
    assert sum(len(v) for v in parse_tanzil("\n".join(lines)).values()) == TOTAL_AYAHS
    with pytest.raises(ValueError, match="114 surahs / 6235 ayahs"):
        parse_tanzil("\n".join(lines[:-1]))


def test_quran_rejects_ayah_count_mismatch(raw_parts) -> None:
    arabic, translation, surahs = raw_parts
    broken = {**arabic, 2: arabic[2][:-1]}
    with pytest.raises(ValueError, match="2 has 286 ayat; text has 285"):
        Quran(broken, translation, surahs)


def test_quran_rejects_translation_mismatch(raw_parts) -> None:
    arabic, translation, surahs = raw_parts
    broken = {**translation, "3": {k: v for k, v in translation["3"].items() if k != "200"}}
    with pytest.raises(ValueError, match="Translation of surah 3 has 199 entries"):
        Quran(arabic, broken, surahs)
    missing = {k: v for k, v in translation.items() if k != "114"}
    with pytest.raises(ValueError, match="Translation of surah 114 has 0 entries"):
        Quran(arabic, missing, surahs)


# ── Text lookup ──────────────────────────────────────────────────────────────────────────────


def test_translation_and_citation(quran: Quran) -> None:
    assert quran.translation(AyahRef(1, 2)) == "Segala puji bagi Allah, Tuhan semesta alam"
    assert quran.surah_name(18) == "Al-Kahf"
    assert quran.citation(AyahRange(2, 282, 282)) == "(QS. Al-Baqarah: 282)"
    assert quran.citation(AyahRange(18, 1, 5)) == "(QS. Al-Kahf: 1–5)"
    with pytest.raises(InvalidReference):
        quran.surah_name(0)
    with pytest.raises(InvalidReference):
        quran.citation(AyahRange(115, 1, 1))


@pytest.mark.parametrize("ref", [AyahRef(0, 1), AyahRef(115, 1), AyahRef(1, 8), AyahRef(2, 287), AyahRef(1, 0), AyahRef(-1, 1), AyahRef(1, -1)])
def test_validate_rejects_non_ayat(quran: Quran, ref: AyahRef) -> None:
    with pytest.raises(InvalidReference, match="is not an ayah"):
        quran.validate(ref)
    with pytest.raises(InvalidReference):
        quran.raw(ref)
    with pytest.raises(InvalidReference):
        quran.arabic(ref)
    with pytest.raises(InvalidReference):
        quran.translation(ref)


def test_ref_and_range_basics() -> None:
    assert str(AyahRef(2, 255)) == "2:255"
    assert AyahRef(2, 255) < AyahRef(2, 256) < AyahRef(3, 1)
    rng = AyahRange(18, 1, 5)
    assert len(rng) == 5
    assert rng.refs == [AyahRef(18, n) for n in range(1, 6)]


def test_al_fatihah_1_is_the_basmala(quran: Quran) -> None:
    ref = AyahRef(1, 1)
    assert quran.raw(ref) == quran.basmala
    assert quran.arabic(ref) == quran.basmala
    assert not quran.has_basmala_prefix(ref)
    assert quran.translation(ref).startswith("Dengan nama Allah")


def test_at_taubah_has_no_basmala(quran: Quran) -> None:
    ref = AyahRef(9, 1)
    assert not quran.has_basmala_prefix(ref)
    assert quran.arabic(ref) == quran.raw(ref)
    assert not skeleton(quran.arabic(ref)).startswith(skeleton(quran.basmala))


def test_an_naml_inner_basmala_is_kept(quran: Quran) -> None:
    ref = AyahRef(27, 30)
    assert not quran.has_basmala_prefix(ref)
    assert quran.arabic(ref) == quran.raw(ref)
    assert skeleton(quran.basmala) in skeleton(quran.arabic(ref))
    # The surah-opening basmala of 27:1 is stripped as usual.
    assert quran.has_basmala_prefix(AyahRef(27, 1))


def test_at_tin_extra_shadda_basmala(quran: Quran) -> None:
    ref = AyahRef(95, 1)
    raw = quran.raw(ref)
    assert not raw.startswith(quran.basmala)  # the Tanzil text carries an extra shadda on the ba
    assert quran.has_basmala_prefix(ref)
    assert nfc(quran.arabic(ref)) == nfc("وَٱلتِّينِ وَٱلزَّيْتُونِ")
    assert skeleton(quran.arabic(ref)) == "وٱلتين وٱلزيتون"
    assert raw.endswith(quran.arabic(ref))


def test_every_surah_opening_basmala_is_stripped(quran: Quran) -> None:
    target = skeleton(quran.basmala)
    for s in range(2, TOTAL_SURAHS + 1):
        ref = AyahRef(s, 1)
        if s == 9:
            continue
        assert quran.has_basmala_prefix(ref), f"{ref} should open with a basmala"
        shown = quran.arabic(ref)
        assert shown and not skeleton(shown).startswith(target), f"{ref} still shows the basmala: {shown[:30]!r}"
        assert shown == shown.strip()


def test_only_ayah_one_can_have_a_basmala_prefix(quran: Quran) -> None:
    assert not quran.has_basmala_prefix(AyahRef(2, 2))


def test_arabic_is_verbatim_substring_after_nfc(quran: Quran) -> None:
    for ref in quran.all_refs():
        raw, shown = quran.raw(ref), quran.arabic(ref)
        assert shown, f"{ref} displays empty"
        assert nfc(shown) in nfc(raw), f"{ref} is not a verbatim substring of the source"
        if not quran.has_basmala_prefix(ref):
            assert shown == raw, f"{ref} was modified"
        else:
            assert nfc(raw).endswith(nfc(shown)), f"{ref} lost text after the basmala"


def test_basmala_end_edge_cases(quran: Quran) -> None:
    b = quran.basmala
    surahs = [Surah(1, "a", "A", "a", 1, True), Surah(2, "b", "B", "b", 1, True), Surah(3, "c", "C", "c", 1, True)]
    mini = Quran({1: [b], 2: [b], 3: ["بسم ٱلله"]}, {"1": {"1": "x"}, "2": {"1": "y"}, "3": {"1": "z"}}, surahs)
    # The whole verse is the basmala, ending in a diacritic: everything is consumed.
    assert mini._basmala_end(b) == len(b)
    assert mini.has_basmala_prefix(AyahRef(2, 1))
    assert mini.arabic(AyahRef(2, 1)) == ""
    # A verse shorter than the basmala is not a basmala.
    assert mini._basmala_end("بسم ٱلله") == -1
    assert not mini.has_basmala_prefix(AyahRef(3, 1))
    assert mini._basmala_end("") == -1
    assert mini._basmala_end("قل") == -1


def test_skeleton_and_nfc() -> None:
    assert skeleton("بِسْمِ") == "بسم"
    assert skeleton("ٱلرَّحْمَٰنِ") == "ٱلرحمن"
    assert skeleton("وَرَسُولِهِۦٓ") == "ورسوله"  # small high waw and maddah are marks, not letters
    assert skeleton("فِيهِۚ") == "فيه"  # pause mark
    decomposed = "آ"  # alef + maddah above
    assert nfc(decomposed) == "آ"


# ── Reference parsing ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2:255", AyahRange(2, 255, 255)),
        (" 2 : 255 ", AyahRange(2, 255, 255)),
        ("18:1-5", AyahRange(18, 1, 5)),
        ("18:1–5", AyahRange(18, 1, 5)),
        ("18:1–18:5", AyahRange(18, 1, 5)),
        ("18:1 - 18:5", AyahRange(18, 1, 5)),
        ("1:7", AyahRange(1, 7, 7)),
        ("114:6", AyahRange(114, 6, 6)),
        ("2:282-286", AyahRange(2, 282, 286)),
    ],
)
def test_parse_reference_valid(quran: Quran, text: str, expected: AyahRange) -> None:
    assert parse_reference(text) == expected
    assert parse_reference(text, quran) == expected


@pytest.mark.parametrize(
    ("value", "match"),
    [
        (None, "Not a reference"),
        ("", "Not a reference"),
        ("   ", "Not a reference"),
        (255, "Not a reference"),
        (2.5, "Not a reference"),
        (["2:255"], "Not a reference"),
        ("2:255a", "Malformed"),
        ("2:255:1", "Malformed"),
        ("-1:1", "Malformed"),
        ("2:-1", "Malformed"),
        ("2.5:1", "Malformed"),
        ("2:1.5", "Malformed"),
        ("QS 2:255", "Malformed"),
        ("2", "Malformed"),
        ("1000:1", "Malformed"),
        ("2:286-3:1", "spans surahs"),
        ("2:286–3:1", "spans surahs"),
        ("2:5-1", "reversed"),
        ("2:5-2:4", "reversed"),
        ("2:1-6", "longer than 5"),
        ("2:255-286", "longer than 5"),
        ("0:1", "is not an ayah"),
        ("2:0", "is not an ayah"),
        ("0:0-1", "is not an ayah"),
    ],
)
def test_parse_reference_invalid(value: object, match: str) -> None:
    with pytest.raises(InvalidReference, match=match):
        parse_reference(value)


@pytest.mark.parametrize("text", ["115:1", "1:8", "2:287", "1:6-8", "114:7", "999:1"])
def test_parse_reference_with_quran_rejects_missing_ayat(quran: Quran, text: str) -> None:
    parse_reference(text)  # well-formed
    with pytest.raises(InvalidReference, match="is not an ayah"):
        parse_reference(text, quran)


def test_max_range_boundary() -> None:
    assert len(parse_reference(f"2:1-{MAX_RANGE}")) == MAX_RANGE
    with pytest.raises(InvalidReference):
        parse_reference(f"2:1-{MAX_RANGE + 1}")


def test_from_parts(quran: Quran) -> None:
    assert from_parts(2, 282) == AyahRange(2, 282, 282)
    assert from_parts(18, 1, 5, quran) == AyahRange(18, 1, 5)
    assert from_parts(18, 1, None) == AyahRange(18, 1, 1)
    with pytest.raises(InvalidReference, match="is not an ayah"):
        from_parts(115, 1, quran=quran)


@pytest.mark.parametrize(
    "parts",
    [(True, 1), (2, False), (2.0, 255), (2, 255.0), ("2", 255), (2, "255"), (None, 1), (2, 1, True), (2, 1, 3.0), (2, 1, "3")],
)
def test_from_parts_rejects_non_integers(parts: tuple) -> None:
    with pytest.raises(InvalidReference, match="must be integers"):
        from_parts(*parts)


@pytest.mark.parametrize("parts", [(-1, 1), (2, -1), (2, 5, 1), (2, 286, 300), (0, 1)])
def test_from_parts_rejects_bad_integers(parts: tuple) -> None:
    with pytest.raises(InvalidReference):
        from_parts(*parts)


@settings(max_examples=500, deadline=None)
@given(st.one_of(st.text(), st.text(alphabet="0123456789:-– a\t"), st.none(), st.integers(), st.floats(), st.booleans()))
def test_parse_reference_never_crashes(value: object) -> None:
    try:
        rng = parse_reference(value)
    except InvalidReference:
        return
    assert isinstance(rng, AyahRange)
    assert rng.surah >= 1 and 1 <= rng.start <= rng.end and len(rng) <= MAX_RANGE


@settings(max_examples=300, deadline=None)
@given(st.text(alphabet="0123456789:-– ", max_size=14))
def test_parse_reference_with_quran_only_returns_real_ayat(quran: Quran, value: str) -> None:
    try:
        rng = parse_reference(value, quran)
    except InvalidReference:
        return
    for ref in rng.refs:
        quran.validate(ref)


@settings(max_examples=300, deadline=None)
@given(st.integers(1, TOTAL_SURAHS), st.data())
def test_valid_references_round_trip(quran: Quran, surah: int, data: st.DataObject) -> None:
    count = quran.surahs[surah].ayah_count
    start = data.draw(st.integers(1, count))
    end = data.draw(st.integers(start, min(count, start + MAX_RANGE - 1)))
    single = parse_reference(str(AyahRef(surah, start)), quran)
    assert single == AyahRange(surah, start, start)
    assert str(single.refs[0]) == f"{surah}:{start}"
    sep = data.draw(st.sampled_from(["-", "–", " - "]))
    long_form = data.draw(st.booleans())
    text = f"{surah}:{start}{sep}{f'{surah}:' if long_form else ''}{end}"
    assert parse_reference(text, quran) == AyahRange(surah, start, end)
    assert from_parts(surah, start, end, quran) == AyahRange(surah, start, end)


@settings(max_examples=200, deadline=None)
@given(st.one_of(st.integers(), st.booleans(), st.floats(), st.text(max_size=5), st.none()), st.one_of(st.integers(), st.booleans(), st.floats()))
def test_from_parts_never_crashes(surah: object, ayah: object) -> None:
    try:
        rng = from_parts(surah, ayah)
    except InvalidReference:
        return
    assert type(surah) is int and type(ayah) is int
    assert rng == AyahRange(surah, ayah, ayah)
