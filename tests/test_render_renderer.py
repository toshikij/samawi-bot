"""The renderer refuses to output anything wrong (spec §6.5, §7, §10.3 "Rendering").

Browser tests share one Chromium per module. Golden PNGs live in tests/golden/ (stored at ¼ scale);
regenerate them after an intended visual change with `UPDATE_GOLDEN=1 uv run pytest tests/test_render_renderer.py`.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import sys
import unicodedata
from collections.abc import Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
import yaml
from PIL import Image, ImageChops, ImageStat
from playwright.sync_api import Browser

from conftest import SNAPSHOT, TODAY, load_draft
from samawi_bot.brain.models import Slide, SourceRef
from samawi_bot.config import LaunchMode, TranslationMode
from samawi_bot.render import renderer as renderer_mod
from samawi_bot.render.compose import (
    ARABIC_MIN_PX,
    SlideSpec,
    action_line,
    ayah_marker,
    compose_carousel,
    content_slide,
    hook_html,
    hook_size,
    scripture_slide,
    story_kahfi,
    story_malam,
    story_subuh,
)
from samawi_bot.render.renderer import FEED, MAX_PAGES, STORY, Overflow, Renderer, RenderError, _rgb, brand_hex, chromium, nb
from samawi_bot.sources import Sources
from samawi_bot.sources.palette import MOMENTS
from samawi_bot.sources.quran import parse_reference
from samawi_bot.sources.snapshot import FONT_AMIRI, FONT_JAKARTA, SNAPSHOT_FILES, SnapshotIntegrityError, write_snapshot

# Font rasterisation differs between macOS and Linux, so each platform keeps its own reference set
# (Linux is generated inside the production image: see scripts/update_golden_linux.sh).
GOLDEN = Path(__file__).parent / "golden" / ("darwin" if sys.platform == "darwin" else "linux")
UPDATE_GOLDEN = os.environ.get("UPDATE_GOLDEN") == "1"
GOLDEN_SCALE = 4
GOLDEN_MEAN_TOLERANCE = 2.0  # mean |Δ| per channel, 0–255
GOLDEN_PIXEL_TOLERANCE = 0.01  # share of pixels allowed to differ by more than 48


# ── fixtures ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with chromium() as b:
        yield b


@pytest.fixture(scope="module")
def renderer(sources: Sources, browser: Browser) -> Renderer:
    return Renderer(sources, browser)


def _png(size: tuple[int, int], colour: tuple[int, int, int]) -> bytes:
    out = BytesIO()
    Image.new("RGB", size, colour).save(out, "PNG")
    return out.getvalue()


def _size(png: bytes) -> tuple[int, int]:
    return Image.open(BytesIO(png)).size


# ── the text bank: shortest / typical / longest copy the writer may produce per template ──────
# Longest follows the writer's limits (heading ≤ 8 words, body ≤ 45 words, ≤ 5 short items).

H8 = "Delapan kata judul yang cukup panjang untuk diuji"
B45 = (
    "Ayat terpanjang di Al-Qur'an adalah Al-Baqarah ayat 282, isinya tentang cara mencatat utang-piutang dengan adil. "
    "Ayat terpanjang di Al-Qur'an adalah Al-Baqarah ayat 282, isinya tentang cara mencatat utang-piutang dengan adil. "
    "Ayat terpanjang di Al-Qur'an adalah Al-Baqarah ayat"
)
B30 = " ".join(B45.split()[:30])
I5 = [
    "Tulis siapa yang berutang dan berapa jumlahnya",
    "Catat kapan utang itu jatuh tempo",
    "Hadirkan dua saksi yang adil",
    "Jangan malas mencatat, kecil atau besar",
    "Simpan catatan di tempat yang aman",
]
TIMES = {"Subuh": "04:05", "Syuruq": "05:25", "Dzuhur": "11:42", "Ashar": "15:06", "Maghrib": "17:55", "Isya": "19:10"}
KINDS = ("cover", "fact", "list", "steps", "comparison", "map", "takeaway", "ayat", "hadith", "dua", "cta", "story")
SCRIPTURE = ("ayat", "hadith", "dua")


def _cover(kicker: str, hook: str) -> SlideSpec:
    return SlideSpec("cover", on_sky=True, kicker=kicker, hook_html=hook_html(hook), hook_px=hook_size(hook))


def _content(**kw: Any) -> SlideSpec:
    return content_slide(Slide(**kw))


def _scripture(sources: Sources, t: str, ref: str, mode: TranslationMode = TranslationMode.KEMENAG, body: str = "", heading: str = "Awal ayatnya") -> SlideSpec:
    return scripture_slide(sources, Slide(template=t, heading=heading, body=body, source=SourceRef(type=t, ref=ref)), mode)  # type: ignore[arg-type]


def spec_for(sources: Sources, kind: str, length: str) -> SlideSpec:
    s = sources
    bank: dict[str, dict[str, Any]] = {
        "cover": {
            "short": lambda: _cover("Fakta Al-Qur'an", "Satu ayat, 128 kata"),
            "typical": lambda: _cover("Seri Ayat yang Jarang Dibahas · 1/5", "Ternyata ayat terpanjang di Al-Qur'an bukan soal salat"),
            "long": lambda: _cover("Seri Kisah yang Jarang Diceritakan · 5/5", "Satu ayat terpanjang di Al-Qur'an, isinya ternyata catatan utang-piutang"),
        },
        "fact": {
            "short": lambda: _content(template="fact", heading="Jawabannya", left_value="282"),
            "typical": lambda: _content(
                template="fact",
                heading="Jawabannya: soal utang-piutang",
                body="Ayat terpanjang di Al-Qur'an adalah Al-Baqarah ayat 282. Isinya cara mencatat utang.",
                left_value="282",
            ),
            "long": lambda: _content(template="fact", heading=H8, body=B45, left_value="6.236"),
        },
        "list": {
            "short": lambda: _content(template="list", heading="Dua hal", items=["Tulis", "Saksi"]),
            "typical": lambda: _content(template="list", heading="Yang diatur ayat ini", items=I5[:4]),
            "long": lambda: _content(template="list", heading=H8, items=I5),
        },
        "steps": {
            "short": lambda: _content(template="steps", heading="Caranya", items=["Tulis", "Simpan"]),
            "typical": lambda: _content(template="steps", heading="Cara mencatat utang", items=I5[:3]),
            "long": lambda: _content(template="steps", heading=H8, items=I5),
        },
        "comparison": {
            "short": lambda: _content(template="comparison", heading="Beda", left_label="Subuh", left_value="2", right_label="Dzuhur", right_value="4"),
            "typical": lambda: _content(
                template="comparison",
                heading="Rakaat salat wajib",
                body="Subuh paling sedikit rakaatnya.",
                left_label="Subuh",
                left_value="2 rakaat",
                right_label="Dzuhur",
                right_value="4 rakaat",
            ),
            "long": lambda: _content(
                template="comparison", heading=H8, body=B30, left_label="Sabang, Aceh", left_value="19:01", right_label="Merauke, Papua", right_value="17:33"
            ),
        },
        "map": {
            "short": lambda: _content(template="map", heading="Jarak", left_label="Sabang", left_value="0", right_label="Merauke", right_value="5"),
            "typical": lambda: _content(
                template="map",
                heading="Maghrib di ujung Indonesia",
                body="Selisihnya hampir tiga jam.",
                left_label="Sabang",
                left_value="18:45",
                right_label="Merauke",
                right_value="17:40",
            ),
            "long": lambda: _content(
                template="map", heading=H8, body=B30, left_label="Sabang, Aceh", left_value="18:45", right_label="Merauke, Papua", right_value="17:40"
            ),
        },
        "takeaway": {
            "short": lambda: _content(template="takeaway", heading="Catat", items=["Tulis"]),
            "typical": lambda: _content(template="takeaway", heading="Catat utangmu hari ini", items=I5[:2]),
            "long": lambda: _content(template="takeaway", heading=H8, items=I5),
        },
        "ayat": {
            "short": lambda: _scripture(s, "ayat", "112:1"),
            "typical": lambda: _scripture(s, "ayat", "94:5-6"),
            "long": lambda: _scripture(s, "ayat", "2:286"),  # too long for one slide: paginates
        },
        "hadith": {
            "short": lambda: _scripture(s, "hadith", "2962", heading="Perkara pertama"),
            "typical": lambda: _scripture(s, "hadith", "3567", heading="Hadis hari ini"),
            "long": lambda: _scripture(s, "hadith", "3350", heading=H8),
        },
        "dua": {
            "short": lambda: _scripture(s, "dua", "keseharian.makan.sebelum", heading="Sebelum makan"),
            "typical": lambda: _scripture(s, "dua", "harian.tidur.1", heading="Doa sebelum tidur"),
            "long": lambda: _scripture(s, "dua", "harian.pagi.sayyidul-istighfar", heading="Sayyidul istighfar"),
        },
        "cta": {
            "short": lambda: SlideSpec("cta", bridge="Tafsir di Samawi, tanpa iklan.", action=action_line(LaunchMode.LIVE), teaser="Besok: satu fakta lagi."),
            "typical": lambda: SlideSpec(
                "cta",
                bridge="Mau tahu penjelasan lengkap ayat ini? Tafsir di Samawi bisa dibaca kapan saja, tanpa iklan.",
                action=action_line(LaunchMode.PRELAUNCH),
                teaser="Besok: surah yang punya dua Bismillah. Follow @samawi.indonesia biar nggak ketinggalan.",
                share="Kirim ke teman yang lagi pinjam-meminjam.",
            ),
            "long": lambda: SlideSpec(
                "cta",
                bridge="Mau rutin baca Al-Kahfi tiap Jumat tanpa lupa? Mode Jumat di Samawi mengingatkanmu setiap pekan, tanpa iklan dan tanpa gangguan.",
                action=action_line(LaunchMode.PRELAUNCH),
                teaser="Besok: surah yang punya dua Bismillah dan kenapa itu terjadi. Follow @samawi.indonesia biar nggak ketinggalan.",
                share="Kirim ke teman yang lagi belajar mencatat utang-piutang dengan rapi.",
            ),
        },
        "story": {
            "short": lambda: story_malam("data:image/png;base64," + base64.b64encode(_png((1080, 1350), (40, 60, 120))).decode(), "Besok: dua Bismillah."),
            "typical": lambda: story_kahfi(s, "Mode Jumat di Samawi mengingatkanmu setiap Jumat, tanpa iklan."),
            "long": lambda: story_subuh(s, "57:4", "25 Jumadil Awal 1448 H (perkiraan)", TIMES),  # longest ayat-of-day that fits
        },
    }
    spec: SlideSpec = bank[kind][length]()
    if kind not in ("cover", "cta", "story"):
        spec.pager = "3/8"
    return spec


# ── golden snapshots ──────────────────────────────────────────────────────────────────────────

GOLDEN_SET = {
    *((k, "typical", "dzuhur") for k in KINDS),  # light panels
    *((k, "typical", "isya") for k in KINDS),  # dark panels
    *(("cover", "typical", sky) for sky in ("subuh", "syuruq", "ashar", "maghrib")),  # every sky gradient
    ("ayat", "long", "maghrib"),  # first page of a paginated ayat
    ("list", "long", "dzuhur"),
}


def _thumb(png: bytes) -> Image.Image:
    img = Image.open(BytesIO(png)).convert("RGB")
    return img.resize((img.width // GOLDEN_SCALE, img.height // GOLDEN_SCALE), Image.Resampling.BOX)


def assert_matches_golden(name: str, png: bytes) -> None:
    path = GOLDEN / f"{name}.png"
    got = _thumb(png)
    if UPDATE_GOLDEN:
        GOLDEN.mkdir(exist_ok=True)
        got.save(path, "PNG", optimize=True)
        return
    if not path.is_file():
        pytest.fail(f"no golden {path.name}; run with UPDATE_GOLDEN=1 to create it")
    want = Image.open(path).convert("RGB")
    assert got.size == want.size, f"{name}: size {got.size} != golden {want.size}"
    diff = ImageChops.difference(got, want)
    mean = sum(ImageStat.Stat(diff).mean) / 3
    big = diff.convert("L").point(lambda v: 255 if v > 48 else 0).histogram()[255] / (got.width * got.height)
    assert mean <= GOLDEN_MEAN_TOLERANCE and big <= GOLDEN_PIXEL_TOLERANCE, f"{name}: differs from golden (mean Δ {mean:.2f}, {big:.2%} pixels changed)"


def test_golden_comparison_catches_a_regression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{__name__}.GOLDEN", tmp_path)
    monkeypatch.setattr(f"{__name__}.UPDATE_GOLDEN", True)
    assert_matches_golden("x", _png((400, 400), (255, 255, 255)))
    monkeypatch.setattr(f"{__name__}.UPDATE_GOLDEN", False)
    assert_matches_golden("x", _png((400, 400), (254, 255, 255)))  # within tolerance
    with pytest.raises(AssertionError, match="differs from golden"):
        assert_matches_golden("x", _png((400, 400), (0, 0, 0)))
    with pytest.raises(AssertionError, match="size"):
        assert_matches_golden("x", _png((400, 800), (255, 255, 255)))
    with pytest.raises(pytest.fail.Exception, match="UPDATE_GOLDEN"):
        assert_matches_golden("missing", _png((40, 40), (0, 0, 0)))


# ── every template × 6 palettes × shortest/typical/longest ────────────────────────────────────


# Inner slides, CTA and stories sit on a panel that only depends on whether the sky is dark, so
# shortest/longest copy runs on one dark and one light sky; typical copy and the cover (the only
# gradient) run on all six.
CASES = [
    *((k, "typical", sky) for k in KINDS for sky in MOMENTS),
    *(("cover", length, sky) for length in ("short", "long") for sky in MOMENTS),
    *((k, length, sky) for k in KINDS if k != "cover" for length in ("short", "long") for sky in ("maghrib", "dzuhur")),
]


@pytest.mark.browser
@pytest.mark.parametrize(("kind", "length", "sky"), CASES, ids=["-".join(c) for c in CASES])
def test_template_renders_and_passes_every_check(renderer: Renderer, sources: Sources, kind: str, length: str, sky: str) -> None:
    spec = spec_for(sources, kind, length)
    size = STORY if kind == "story" else FEED
    if kind in SCRIPTURE:
        rendered = renderer.render_slides([spec], sky, size)
        pages = [r.spec for r in rendered]
        assert " ".join(p.arabic for p in pages if p.arabic) == spec.arabic
        assert " ".join(p.translation for p in pages if p.translation) == spec.translation
        assert all(p.arabic_px >= ARABIC_MIN_PX for p in pages)
        if (kind, length) == ("ayat", "long"):
            assert len(pages) > 1
        pngs = [r.png for r in rendered]
    else:
        pngs = [renderer.render_one(spec, sky, size)]
    assert all(_size(p) == size for p in pngs)
    if (kind, length, sky) in GOLDEN_SET:
        assert_matches_golden(f"{kind}-{length}-{sky}", pngs[0])


def _aod_refs() -> list[str]:
    from importlib import resources

    return yaml.safe_load(resources.files("samawi_bot.content").joinpath("ayat_of_day.yaml").read_text("utf-8"))["refs"]


TOO_LONG_FOR_A_STORY: set[str] = set()  # 2:286, 65:2-3, 73:20 and 3:159 were replaced for this reason


@pytest.mark.browser
@pytest.mark.parametrize(
    "ref",
    [
        pytest.param(r, marks=pytest.mark.xfail(raises=Overflow, strict=True, reason="too long for one Subuh story; replace it in ayat_of_day.yaml"))
        if r in TOO_LONG_FOR_A_STORY
        else r
        for r in _aod_refs()
    ],
)
def test_every_ayat_of_day_fits_the_subuh_story(renderer: Renderer, sources: Sources, ref: str) -> None:
    spec = story_subuh(sources, ref, "25 Jumadil Awal 1448 H (perkiraan)", TIMES)
    assert _size(renderer.render_one(spec, "subuh", STORY)) == STORY


# ── Arabic: verbatim, never shrunk, paginated ─────────────────────────────────────────────────


def _rendered_text(browser: Browser, html: str, selector: str, tmp_path: Path, size: tuple[int, int] = FEED) -> list[str]:
    page = browser.new_page(viewport={"width": size[0], "height": size[1]})
    try:
        f = tmp_path / "slide.html"
        f.write_text(html, "utf-8")
        page.goto(f.as_uri())
        return list(page.eval_on_selector_all(selector, "els => els.map(e => e.textContent)"))
    finally:
        page.close()


@pytest.mark.browser
@pytest.mark.parametrize("ref", ["18:1-2", "96:19", "27:30", "1:1", "9:1", "2:255"])
def test_arabic_renders_byte_for_byte(renderer: Renderer, browser: Browser, sources: Sources, ref: str, tmp_path: Path) -> None:
    """Saktah (18:1), sajdah sign (96:19), inner Bismillah (27:30), Bismillah as an ayah, At-Taubah."""
    spec = _scripture(sources, "ayat", ref)
    rng = parse_reference(ref, sources.quran)
    source = " ".join(f"{sources.quran.arabic(r)} {ayah_marker(r.ayah)}" for r in rng.refs)
    assert spec.arabic == source
    renderer.render_slides([spec], "isya")  # the renderer's own NFC assertion passes
    (text,) = _rendered_text(browser, renderer.html(spec, "isya", FEED), ".arabic", tmp_path)
    assert text == source
    assert unicodedata.normalize("NFC", text).encode() == unicodedata.normalize("NFC", source).encode()


@pytest.mark.browser
def test_arabic_only_latin_only_and_mixed_slides(renderer: Renderer, sources: Sources) -> None:
    arabic_only = _scripture(sources, "ayat", "112:1-4", mode=TranslationMode.MAKNA, body="", heading="")
    assert arabic_only.arabic and not arabic_only.translation
    latin_only = _scripture(sources, "hadith", "2962")
    assert latin_only.translation and not latin_only.arabic
    mixed = _scripture(sources, "dua", "harian.bangun.1")
    for spec in (arabic_only, latin_only, mixed):
        for sky in ("dzuhur", "isya"):
            assert _size(renderer.render_one(spec, sky)) == FEED


@pytest.mark.browser
def test_al_baqarah_282_paginates_and_never_shrinks(renderer: Renderer, sources: Sources) -> None:
    spec = _scripture(sources, "ayat", "2:282")
    with pytest.raises(Overflow):
        renderer.render_one(spec, "dzuhur")  # does not fit one slide
    rendered = renderer.render_slides([spec], "dzuhur")
    pages = [r.spec for r in rendered]
    assert 2 <= len(pages) <= MAX_PAGES
    assert " ".join(p.arabic for p in pages) == spec.arabic
    assert " ".join(p.translation for p in pages) == spec.translation
    assert all(p.arabic_px >= ARABIC_MIN_PX for p in pages)
    assert [p.reference.endswith("bersambung") for p in pages] == [True] * (len(pages) - 1) + [False]
    assert all(p.pager == "" for p in pages)  # a lone spec without a pager is not numbered


@pytest.mark.browser
def test_carousel_with_paginated_ayat_is_renumbered(renderer: Renderer, sources: Sources) -> None:
    specs = compose_carousel(load_draft(), 0, sources, LaunchMode.PRELAUNCH, TranslationMode.KEMENAG)
    rendered = renderer.render_slides(specs, "maghrib")
    kinds = [r.spec.kind for r in rendered]
    n = len(rendered)
    assert n > len(specs) and kinds.count("ayat") == n - len(specs) + 1
    assert [r.spec.pager for r in rendered] == [""] + [f"{i}/{n}" for i in range(2, n)] + [""]
    assert all(_size(r.png) == FEED for r in rendered)


# ── failures that must block ──────────────────────────────────────────────────────────────────


@pytest.mark.browser
def test_overflow_of_a_content_slide_is_not_paginated(renderer: Renderer) -> None:
    spec = _content(template="list", heading=H8, body=B45, items=[*I5, *I5])
    with pytest.raises(Overflow, match="list slide overflows"):
        renderer.render_slides([spec], "dzuhur")


@pytest.mark.browser
def test_overflowing_cover_hook_is_refused(renderer: Renderer) -> None:
    hook = "Satu ayat terpanjang di Al-Qur'an ternyata isinya catatan utang-piutang yang panjang sekali"
    spec = SlideSpec("cover", on_sky=True, kicker="Fakta", hook_html=hook_html(hook * 3), hook_px=128)
    with pytest.raises(Overflow):
        renderer.render_one(spec, "subuh")


@pytest.mark.browser
def test_scripture_beyond_max_pages_raises(renderer: Renderer, sources: Sources) -> None:
    base = _scripture(sources, "ayat", "2:282")
    huge = SlideSpec("ayat", heading="x", arabic=" ".join([base.arabic] * 6), translation=" ".join([base.translation] * 6), reference=base.reference)
    with pytest.raises(Overflow):
        renderer.render_slides([huge], "dzuhur")


@pytest.mark.browser
def test_tampered_expected_text_raises(renderer: Renderer, sources: Sources) -> None:
    spec = _scripture(sources, "ayat", "112:1")
    spec.expected_texts = [spec.arabic + "ً"]  # one extra harakah
    with pytest.raises(RenderError, match="source text missing"):
        renderer.render_one(spec, "dzuhur")


@pytest.mark.browser
def test_rendered_text_differing_from_its_source_attribute_raises(sources: Sources, browser: Browser) -> None:
    class Tampered(Renderer):
        def html(self, spec: SlideSpec, sky_name: str, size: tuple[int, int]) -> str:
            out = super().html(spec, sky_name, size)
            tampered, n = re.subn(r'(data-label="translation" data-expected=")[^"]*', r"\1teks lain", out)
            assert n == 1
            return tampered

    spec = _scripture(sources, "hadith", "2962")
    with pytest.raises(RenderError, match="rendered text differs from source"):
        Tampered(sources, browser).render_one(spec, "dzuhur")


@pytest.mark.browser
def test_nfc_equivalent_text_is_accepted(renderer: Renderer) -> None:
    # "é" decomposed in the source vs composed in the expectation: equal after NFC.
    spec = SlideSpec("hadith", heading="", translation="Café dan doa.", reference="(HR. Muslim)", expected_texts=["Café dan doa."])
    assert _size(renderer.render_one(spec, "dzuhur")) == FEED


@pytest.mark.browser
def test_contrast_failure_blocks_render(renderer: Renderer, monkeypatch: pytest.MonkeyPatch) -> None:
    real_theme = renderer_mod.theme

    def unreadable(sources: Sources, sky: Any) -> dict[str, Any]:
        t = real_theme(sources, sky)
        t["on_panel"] = t["panel"]  # text the colour of the panel it sits on
        return t

    monkeypatch.setattr(renderer_mod, "theme", unreadable)
    spec = _content(template="fact", heading="Jawabannya", body="Isinya cara mencatat utang.", left_value="282")
    with pytest.raises(RenderError, match="contrast below WCAG AA") as exc:
        renderer.render_one(spec, "isya")
    assert not isinstance(exc.value, Overflow)
    assert "heading" in str(exc.value)


@pytest.mark.browser
def test_highlight_contrast_is_measured_against_its_marker(renderer: Renderer, monkeypatch: pytest.MonkeyPatch) -> None:
    real_theme = renderer_mod.theme

    def brass_on_brass(sources: Sources, sky: Any) -> dict[str, Any]:
        t = real_theme(sources, sky)
        t["highlight_fg"] = t["highlight_bg"]
        return t

    monkeypatch.setattr(renderer_mod, "theme", brass_on_brass)
    with pytest.raises(RenderError, match="highlight"):
        renderer.render_one(spec_for(renderer.sources, "cover", "short"), "dzuhur")  # "128" is highlighted


@pytest.fixture
def corrupt_font_sources(tmp_path: Path, hadith_dir: Path) -> Sources:
    """A snapshot whose manifest is consistent but whose Jakarta font is garbage."""
    contents = [(rel, b"\x00not a font\x00" * 64 if rel == FONT_JAKARTA else (SNAPSHOT / rel).read_bytes()) for rel in SNAPSHOT_FILES]
    write_snapshot(tmp_path / "snap", contents, "test")
    return Sources.load(tmp_path / "snap", hadith_dir)


@pytest.mark.browser
def test_corrupt_font_is_a_hard_failure(corrupt_font_sources: Sources, browser: Browser) -> None:
    spec = SlideSpec("fact", heading="Jawabannya", body="Isinya.")
    with pytest.raises(RenderError, match="brand fonts did not load") as exc:
        Renderer(corrupt_font_sources, browser).render_one(spec, "dzuhur")
    assert not isinstance(exc.value, Overflow)


@pytest.mark.browser
def test_missing_arabic_font_is_a_hard_failure(renderer: Renderer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = Renderer._assets

    def missing(self: Renderer) -> dict[str, Any]:
        assets = real(self)
        assets["fonts"]["amiri"] = (tmp_path / "gone.ttf").as_uri()
        return assets

    monkeypatch.setattr(Renderer, "_assets", missing)
    with pytest.raises(RenderError, match="brand fonts did not load"):
        renderer.render_one(SlideSpec("fact", heading="Jawabannya"), "dzuhur")


def test_tampered_font_in_snapshot_stops_before_rendering(tmp_path: Path, hadith_dir: Path) -> None:
    snap = tmp_path / "snap"
    shutil.copytree(SNAPSHOT, snap)
    sources = Sources.load(snap, hadith_dir)
    (snap / FONT_AMIRI).write_bytes(b"corrupt")  # checksum no longer matches the manifest
    with pytest.raises(SnapshotIntegrityError):
        Renderer(sources, None).html(SlideSpec("fact"), "dzuhur", FEED)  # type: ignore[arg-type]


# ── template details ──────────────────────────────────────────────────────────────────────────


def test_html_escapes_copy_and_keeps_apostrophes(renderer: Renderer) -> None:
    spec = _content(template="list", heading="Al-Qur'an <i>x</i> & y", items=["<script>alert(x)</script>"])
    html = renderer.html(spec, "dzuhur", FEED)
    assert "<script>alert" not in html and "&lt;script&gt;" in html
    assert "&lt;i&gt;x&lt;/i&gt; &amp; y" in html
    assert '<span class="nb">Al-Qur&#39;an</span>' in html  # an entity in the source is fine: the browser shows '
    assert 'class="canvas panel list"' in html


@pytest.mark.browser
def test_apostrophe_renders_as_an_apostrophe(renderer: Renderer, browser: Browser, tmp_path: Path) -> None:
    spec = _content(template="fact", heading="Al-Qur'an & As-Sunnah")
    (text,) = _rendered_text(browser, renderer.html(spec, "dzuhur", FEED), "h2", tmp_path)
    assert text == "Al-Qur'an & As-Sunnah"
    cover = spec_for(renderer.sources, "cover", "typical")
    (hook,) = _rendered_text(browser, renderer.html(cover, "dzuhur", FEED), ".hook", tmp_path)
    assert hook == "Ternyata ayat terpanjang di Al-Qur'an bukan soal salat"


def test_cover_is_the_only_gradient(renderer: Renderer, sources: Sources) -> None:
    assert 'class="canvas sky cover"' in renderer.html(spec_for(sources, "cover", "typical"), "maghrib", FEED)
    for kind in ("fact", "ayat", "cta"):
        assert 'class="canvas panel' in renderer.html(spec_for(sources, kind, "typical"), "maghrib", FEED)


def test_nb_filter() -> None:
    out = nb("Al-Baqarah & <b>utang-piutang</b>")
    assert str(out) == '<span class="nb">Al-Baqarah</span> &amp; <span class="nb">&lt;b&gt;utang-piutang&lt;/b&gt;</span>'
    assert str(nb("tanpa tanda")) == "tanpa tanda"


@pytest.mark.parametrize(
    ("css", "rgb"),
    [("rgb(238, 242, 249)", (238, 242, 249)), ("rgba(7, 11, 20, 0.5)", (7, 11, 20)), ("rgb(1 2 3 / 0.4)", (1, 2, 3)), ("rgb(10.6, 0, 255)", (10, 0, 255))],
)
def test_rgb_parse(css: str, rgb: tuple[int, int, int]) -> None:
    assert _rgb(css) == rgb


def test_brand_hex(sources: Sources) -> None:
    assert brand_hex(sources) == (0xC7, 0x9A, 0x55)


@pytest.mark.browser
@pytest.mark.parametrize("sky", ["subuh", "dzuhur"])
def test_text_running_under_the_handle_is_refused_on_any_sky(renderer: Renderer, sources: Sources, sky: str) -> None:
    """3:159 pushes the prayer-time grid under the handle. On a dark sky the contrast still passes, so
    only the overlap check stops it."""
    spec = story_subuh(sources, "3:159", "25 Jumadil Awal 1448 H (perkiraan)", TIMES)
    with pytest.raises(Overflow, match="overlaps"):
        renderer.render_one(spec, sky, STORY)


@pytest.mark.browser
def test_map_slide_fills_the_whole_canvas(renderer: Renderer, sources: Sources) -> None:
    """Regression: the canvas carries the kind as a class, and `.map {height: 330px}` once collapsed it."""
    png = renderer.render_one(spec_for(sources, "map", "typical"), "dzuhur")
    img = Image.open(BytesIO(png)).convert("RGB")
    paper = tuple(int(sources.brand.paper[i : i + 2], 16) for i in (1, 3, 5))
    assert img.getpixel((540, 1000)) == paper
    assert img.getpixel((1070, 1340)) == paper


def test_feed_and_story_sizes() -> None:
    assert FEED == (1080, 1350) and STORY == (1080, 1920)
    assert TODAY.weekday() == 3  # the conftest day is a Thursday (kahfi story day)
