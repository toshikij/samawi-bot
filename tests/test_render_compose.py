"""Draft → slide specs. Scripture comes from sources, never the draft; long passages split, never shrink."""

from __future__ import annotations

import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from conftest import load_draft
from samawi_bot.brain.models import Slide, SourceRef
from samawi_bot.config import LaunchMode, TranslationMode
from samawi_bot.render.compose import (
    ARABIC_MIN_PX,
    PILLAR_LABELS,
    TRANSLATION_MIN_PX,
    SlideSpec,
    action_line,
    arabic_px,
    ayah_marker,
    ayat_of_day,
    compose_carousel,
    content_slide,
    hook_html,
    hook_size,
    number,
    paginate,
    scripture_slide,
    split_words,
    story_kahfi,
    story_malam,
    story_subuh,
    theme,
    translation_px,
)
from samawi_bot.sources import Sources
from samawi_bot.sources.quran import parse_reference

# ── hook_html ─────────────────────────────────────────────────────────────────────────────────


def test_hook_html_never_turns_an_apostrophe_into_an_entity() -> None:
    out = hook_html("Ternyata ayat terpanjang di Al-Qur'an bukan soal salat")
    assert "Al-Qur'an" in out
    assert "&#x27;" not in out and "&#39;" not in out and "&apos;" not in out


def test_hook_html_escapes_markup() -> None:
    out = hook_html("Ada <script>alert(x)</script> & 3 hal")
    assert "<script>" not in out
    assert "&lt;script&gt;alert(x)&lt;/script&gt;" in out
    assert "&amp;" in out


def test_hook_html_escapes_markup_inside_the_highlight() -> None:
    out = hook_html("<b>12 jam</b>")
    assert "<b>" not in out
    assert '<span class="hl txt" data-label="highlight">12 jam</span>' in out


def test_hook_html_highlights_only_the_first_number() -> None:
    out = hook_html("128 kata dan 282 ayat")
    assert out.count('class="hl') == 1
    assert out.startswith('<span class="hl txt" data-label="highlight">128</span>')
    assert out.endswith(" kata dan 282 ayat")


def test_hook_html_highlight_includes_unit() -> None:
    out = hook_html("Ternyata 13 jam lebih lama")
    assert '<span class="hl txt" data-label="highlight">13 jam</span>' in out


def test_hook_html_highlights_qs_reference_before_a_later_number() -> None:
    out = hook_html("Baca QS. Al-Baqarah:282 dan 128 kata")
    assert out.count('class="hl') == 1
    assert '<span class="hl txt" data-label="highlight">QS. <span class="nb">Al-Baqarah:282</span></span>' in out
    assert out.endswith(" dan 128 kata")


def test_hook_html_without_number_has_no_highlight() -> None:
    out = hook_html("Kenapa ayat terpanjang bahas utang?")
    assert "hl" not in out
    assert out == "Kenapa ayat terpanjang bahas utang?"


def test_hook_html_keeps_hyphenated_words_on_one_line() -> None:
    out = hook_html("Satu ayat soal utang-piutang di Al-Qur'an")
    assert '<span class="nb">utang-piutang</span>' in out
    assert '<span class="nb">Al-Qur\'an</span>' in out
    # A lone dash between spaces is not a hyphenated word.
    assert "nb" not in hook_html("Subuh - Dzuhur")


# ── size thresholds ───────────────────────────────────────────────────────────────────────────


def test_hook_size_thresholds() -> None:
    assert [hook_size("x" * n) for n in (1, 32, 33, 48, 49, 64, 65, 200)] == [128, 128, 112, 112, 96, 96, 84, 84]


def test_arabic_px_thresholds() -> None:
    assert [arabic_px("ا" * n) for n in (0, 109, 110, 219, 220, 379, 380, 5000)] == [66, 66, 58, 58, 50, 50, ARABIC_MIN_PX, ARABIC_MIN_PX]


def test_translation_px_thresholds() -> None:
    assert [translation_px("a" * n) for n in (0, 159, 160, 319, 320, 519, 520, 5000)] == [42, 42, 38, 38, 34, 34, TRANSLATION_MIN_PX, TRANSLATION_MIN_PX]


def test_action_line_by_launch_mode() -> None:
    live, pre = action_line(LaunchMode.LIVE), action_line(LaunchMode.PRELAUNCH)
    assert "Komentar SAMAWI" in live
    assert "Segera hadir di Android" in pre
    assert "SAMAWI" not in pre  # no comment-for-link before launch


def test_ayah_marker_uses_arabic_digits() -> None:
    assert ayah_marker(282) == "﴿٢٨٢﴾"


# ── scripture slides ──────────────────────────────────────────────────────────────────────────


def _slide(t: str, ref: str, heading: str = "Awal ayatnya", body: str = "") -> Slide:
    return Slide(template=t, heading=heading, body=body, source=SourceRef(type=t, ref=ref))  # type: ignore[arg-type]


def test_ayat_kemenag_single(sources: Sources) -> None:
    spec = scripture_slide(sources, _slide("ayat", "2:282"), TranslationMode.KEMENAG)
    rng = parse_reference("2:282", sources.quran)
    ref = rng.refs[0]
    assert spec.kind == "ayat"
    assert spec.arabic == f"{sources.quran.arabic(ref)} ﴿٢٨٢﴾"
    assert spec.translation == sources.quran.translation(ref)  # no "(282)" prefix for one ayah
    assert spec.reference == sources.quran.citation(rng)
    assert spec.expected_texts == [spec.arabic, spec.translation]
    assert spec.arabic_px == ARABIC_MIN_PX  # longest ayah: minimum size, then paginated by the renderer
    assert spec.translation_px == TRANSLATION_MIN_PX


def test_ayat_kemenag_range_numbers_each_translation(sources: Sources) -> None:
    spec = scripture_slide(sources, _slide("ayat", "94:5-6"), TranslationMode.KEMENAG)
    rng = parse_reference("94:5-6", sources.quran)
    assert spec.translation == " ".join(f"({r.ayah}) {sources.quran.translation(r)}" for r in rng.refs)
    assert spec.arabic.count("﴿") == 2
    assert spec.reference == "(QS. Asy-Syarh: 5–6)"


def test_ayat_makna_uses_the_writers_paraphrase_and_only_checks_arabic(sources: Sources) -> None:
    spec = scripture_slide(sources, _slide("ayat", "2:282", body="Maknanya: catatlah utang."), TranslationMode.MAKNA)
    assert spec.translation == "Maknanya: catatlah utang."
    assert spec.expected_texts == [spec.arabic]


def test_hadith_slide(sources: Sources) -> None:
    h = sources.hadith.get("2962")
    spec = scripture_slide(sources, _slide("hadith", "2962", heading="Perkara pertama"), TranslationMode.KEMENAG)
    assert spec.kind == "hadith"
    assert spec.arabic == ""
    assert spec.translation == h.text_id
    assert spec.reference == f"({h.reference})"
    assert spec.expected_texts == [h.text_id]
    assert spec.arabic_px == 66


def test_dua_slide(sources: Sources) -> None:
    d = sources.duas.get("harian.tidur.1")
    spec = scripture_slide(sources, _slide("dua", "harian.tidur.1"), TranslationMode.KEMENAG)
    assert spec.kind == "dua"
    assert (spec.arabic, spec.translation, spec.reference) == (d.arabic, d.indonesian, f"({d.reference})")
    assert spec.expected_texts == [d.arabic, d.indonesian]


# ── pagination ────────────────────────────────────────────────────────────────────────────────


def test_split_words_rejoins() -> None:
    assert split_words("a b c d e", 2) == ["a b c", "d e"]
    assert split_words("a", 3) == ["a"]
    assert split_words("", 2) == [""]


@settings(max_examples=200, deadline=None)
@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=400), st.integers(min_value=1, max_value=8))
def test_split_words_property(text: str, parts: int) -> None:
    chunks = split_words(text, parts)
    assert " ".join(chunks) == text
    assert 1 <= len(chunks) <= parts


_word = st.text(alphabet=st.characters(blacklist_categories=("Cs", "Zs", "Zl", "Zp", "Cc")), min_size=1, max_size=12)


@settings(max_examples=200, deadline=None)
@given(
    st.lists(_word, min_size=1, max_size=80).map(" ".join),
    st.lists(_word, min_size=0, max_size=120).map(" ".join),
    st.integers(min_value=1, max_value=6),
)
def test_paginate_chunks_rejoin_exactly(arabic: str, translation: str, parts: int) -> None:
    spec = SlideSpec("ayat", heading="H", arabic=arabic, translation=translation, reference="(QS. X: 1)", expected_texts=[arabic, translation])
    pages = paginate(spec, parts)
    assert 1 <= len(pages) <= parts
    assert " ".join(p.arabic for p in pages if p.arabic) == arabic
    if translation:
        assert " ".join(p.translation for p in pages if p.translation) == translation
    else:
        assert all(p.translation == "" for p in pages)
    for i, p in enumerate(pages):
        assert p.heading == ""
        assert p.arabic_px >= ARABIC_MIN_PX and p.translation_px >= TRANSLATION_MIN_PX
        last = i == len(pages) - 1
        assert p.reference == "(QS. X: 1)" + ("" if last else " · bersambung")
        assert all(t and (t in arabic or t in translation) for t in p.expected_texts)


def test_paginate_real_ayah(sources: Sources) -> None:
    spec = scripture_slide(sources, _slide("ayat", "2:282"), TranslationMode.KEMENAG)
    pages = paginate(spec, 3)
    assert len(pages) == 3
    assert " ".join(p.arabic for p in pages) == spec.arabic
    assert " ".join(p.translation for p in pages) == spec.translation
    assert [p.expected_texts for p in pages] == [[p.arabic, p.translation] for p in pages]
    assert all(p.kind == "ayat" for p in pages)


def test_paginate_makna_translation_is_not_an_expected_text(sources: Sources) -> None:
    spec = scripture_slide(sources, _slide("ayat", "2:282", body="satu dua tiga empat"), TranslationMode.MAKNA)
    pages = paginate(spec, 2)
    assert [p.expected_texts for p in pages] == [[p.arabic] for p in pages]


def test_paginate_hadith_without_arabic(sources: Sources) -> None:
    spec = scripture_slide(sources, _slide("hadith", "3350"), TranslationMode.KEMENAG)
    pages = paginate(spec, 2)
    assert [p.arabic for p in pages] == ["", ""]
    assert " ".join(p.translation for p in pages) == spec.translation


# ── content slides and the carousel ───────────────────────────────────────────────────────────


def test_fact_with_single_value_becomes_big_number() -> None:
    spec = content_slide(Slide(template="fact", heading="H", body="B", left_value="282"))
    assert (spec.kind, spec.big, spec.left_value) == ("fact", "282", "")


def test_fact_with_two_values_keeps_them() -> None:
    spec = content_slide(Slide(template="fact", left_value="1", right_value="2"))
    assert (spec.big, spec.left_value, spec.right_value) == ("", "1", "2")


def test_takeaway_renders_as_list_with_kicker() -> None:
    spec = content_slide(Slide(template="takeaway", heading="Catat", items=["a", "b"]))
    assert (spec.kind, spec.kicker, spec.items) == ("list", "Lakukan hari ini", ["a", "b"])


def test_comparison_and_map_keep_labels() -> None:
    for t in ("comparison", "map"):
        spec = content_slide(Slide(template=t, left_label="Sabang", left_value="18:45", right_label="Merauke", right_value="17:40"))  # type: ignore[arg-type]
        assert (spec.kind, spec.left_label, spec.left_value, spec.right_label, spec.right_value) == (t, "Sabang", "18:45", "Merauke", "17:40")
        assert spec.kicker == "" and spec.big == ""


def test_compose_carousel_adds_cover_cta_and_pagers(sources: Sources) -> None:
    draft = load_draft()
    specs = compose_carousel(draft, 1, sources, LaunchMode.PRELAUNCH, TranslationMode.KEMENAG)
    assert len(specs) == len(draft.slides) + 2
    assert [s.kind for s in specs] == ["cover", "fact", "ayat", "list", "hadith", "list", "cta"]
    cover, cta = specs[0], specs[-1]
    assert cover.on_sky and cover.kicker == "Seri Ayat yang Jarang Dibahas · 1/5"
    assert cover.hook_html == hook_html(draft.hooks[1])
    assert cover.hook_px == hook_size(draft.hooks[1])
    assert (cta.bridge, cta.teaser, cta.share) == (draft.bridge_line, draft.next_teaser, draft.share_prompt)
    assert cta.action == action_line(LaunchMode.PRELAUNCH)
    assert [s.pager for s in specs] == ["", "2/7", "3/7", "4/7", "5/7", "6/7", ""]
    # Scripture from sources, not the draft.
    assert specs[2].expected_texts and specs[4].translation == sources.hadith.get("2962").text_id


def test_compose_carousel_without_series_uses_pillar_label_and_live_action(sources: Sources) -> None:
    draft = load_draft(series=None)
    specs = compose_carousel(draft, 0, sources, LaunchMode.LIVE, TranslationMode.KEMENAG)
    assert specs[0].kicker == PILLAR_LABELS["fakta_quran"]
    assert specs[-1].action == action_line(LaunchMode.LIVE)


def test_compose_carousel_scripture_template_without_source_is_content() -> None:
    from samawi_bot.render import compose

    draft = load_draft(slides=[{"template": "ayat", "heading": "Tanpa sumber", "body": "teks"}])
    specs = compose.compose_carousel(draft, 0, None, LaunchMode.PRELAUNCH, TranslationMode.KEMENAG)  # type: ignore[arg-type]
    assert specs[1].kind == "ayat" and specs[1].arabic == "" and specs[1].body == "teks"


def test_number_skips_cover_and_cta() -> None:
    specs = number([SlideSpec("cover"), SlideSpec("fact"), SlideSpec("cta")])
    assert [s.pager for s in specs] == ["", "2/3", ""]


# ── theme ─────────────────────────────────────────────────────────────────────────────────────


def test_theme_dark_sky(sources: Sources) -> None:
    b = sources.brand
    sky = b.palettes["isya"]
    t = theme(sources, sky)
    assert t["sky"] is sky and t["brand"] is b
    assert (t["panel"], t["on_panel"], t["on_panel_muted"], t["panel_accent"]) == (b.panel_dark, b.on_dark, b.muted, b.brass)
    assert (t["highlight_bg"], t["highlight_fg"]) == (b.brass, b.ink)


def test_theme_light_sky(sources: Sources) -> None:
    b = sources.brand
    t = theme(sources, b.palettes["dzuhur"])
    assert (t["panel"], t["on_panel"], t["on_panel_muted"], t["panel_accent"]) == (b.paper, b.ink, b.on_panel_muted_light, b.deep_ink)
    assert (t["highlight_bg"], t["highlight_fg"]) == (b.brass, b.ink)


# ── stories ───────────────────────────────────────────────────────────────────────────────────


def _aod_refs() -> list[str]:
    from importlib import resources

    return yaml.safe_load(resources.files("samawi_bot.content").joinpath("ayat_of_day.yaml").read_text("utf-8"))["refs"]


def test_ayat_of_day_rotates_by_ordinal() -> None:
    refs = _aod_refs()
    assert [ayat_of_day(i) for i in range(len(refs))] == refs
    assert ayat_of_day(len(refs)) == refs[0]
    assert ayat_of_day(len(refs) * 7 + 3) == refs[3]


def test_every_ayat_of_day_ref_resolves(sources: Sources) -> None:
    for ref in _aod_refs():
        assert parse_reference(ref, sources.quran).refs


def test_story_subuh(sources: Sources) -> None:
    times = {"Subuh": "04:05", "Dzuhur": "11:42"}
    spec = story_subuh(sources, "94:5-6", "25 Jumadil Awal 1448 H", times)
    rng = parse_reference("94:5-6", sources.quran)
    assert spec.kind == "story"
    assert spec.kicker == "Ayat hari ini · 25 Jumadil Awal 1448 H"
    assert spec.arabic == " ".join(f"{sources.quran.arabic(r)} {ayah_marker(r.ayah)}" for r in rng.refs)
    assert spec.translation == " ".join(sources.quran.translation(r) for r in rng.refs)  # no (n) prefixes on stories
    assert spec.reference == sources.quran.citation(rng)
    assert spec.arabic_px == arabic_px(spec.arabic) + 4
    assert spec.translation_px == translation_px(spec.translation) + 2
    assert spec.times == [("Subuh", "04:05"), ("Dzuhur", "11:42")]
    assert spec.times_title.startswith("Waktu salat Jakarta")
    assert spec.expected_texts == [spec.arabic, spec.translation]


def test_story_malam() -> None:
    spec = story_malam("data:image/png;base64,AAAA", "Besok: dua Bismillah.")
    assert (spec.kind, spec.kicker, spec.thumb, spec.body) == ("story", "Post hari ini", "data:image/png;base64,AAAA", "Besok: dua Bismillah.")
    assert spec.expected_texts == []


def test_story_kahfi(sources: Sources) -> None:
    spec = story_kahfi(sources, "Mode Jumat di Samawi, tanpa iklan.")
    ref = parse_reference("18:10", sources.quran).refs[0]
    assert spec.kicker == "Malam Jumat"
    assert spec.arabic == sources.quran.arabic(ref)
    assert spec.translation == sources.quran.translation(ref)
    assert spec.reference == "(QS. Al-Kahf: 10)"
    assert spec.body == "Mode Jumat di Samawi, tanpa iklan."
    assert spec.expected_texts == [spec.arabic, spec.translation]


def test_spec_ctx_is_its_fields() -> None:
    spec = SlideSpec("fact", heading="H")
    assert spec.ctx()["heading"] == "H" and spec.ctx()["kind"] == "fact"


def test_map_or_comparison_with_an_empty_side_becomes_a_fact_slide() -> None:
    from samawi_bot.brain.models import Slide
    from samawi_bot.render.compose import content_slide

    assert content_slide(Slide(template="map", heading="Timur lebih dulu", body="x")).kind == "fact"
    assert content_slide(Slide(template="comparison", heading="h", left_label="a", left_value="1")).kind == "fact"
    full = Slide(template="map", heading="h", left_label="A", left_value="03:46", right_label="B", right_value="05:11")
    assert content_slide(full).kind == "map"
