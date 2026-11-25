"""Sky palettes parsed from `sky_palette.dart` (spec §7)."""

from __future__ import annotations

import pytest

from conftest import SNAPSHOT
from samawi_bot.sources import Sources
from samawi_bot.sources.palette import MOMENTS, Brand, PaletteParseError, SkyPalette, _hex, load_brand, parse_brand
from samawi_bot.sources.snapshot import SKY_PALETTE, Snapshot


@pytest.fixture(scope="module")
def real_source() -> str:
    return Snapshot(SNAPSHOT).read_text(SKY_PALETTE)


@pytest.fixture(scope="module")
def brand(sources: Sources) -> Brand:
    return sources.brand


def _replace_once(src: str, old: str, new: str) -> str:
    assert old in src, old
    return src.replace(old, new, 1)


def test_real_file(brand: Brand) -> None:
    assert set(brand.palettes) == set(MOMENTS)
    assert len(brand.palettes) == 6
    m = brand.palettes["maghrib"]
    assert m == SkyPalette("maghrib", "#E79A44", "#4E356E", "#D06A78", "#FFFFFF", "#F0E6EE", "#E6C68B", True, True)
    assert m.stops == ["#E79A44", "#D06A78", "#4E356E"]
    assert [p.name for p in brand.palettes.values() if p.mid] == ["maghrib"]
    assert {p.name for p in brand.palettes.values() if p.requires_scrim} == {"syuruq", "dzuhur", "maghrib"}
    assert {p.name for p in brand.palettes.values() if p.is_dark} == {"subuh", "maghrib", "isya"}
    subuh = brand.palettes["subuh"]
    assert subuh.on_sky == "#EEF2F9"  # resolved through the `onDarkText` constant
    assert subuh.on_sky_muted == "#A8B6CE"  # literal Color(0x…) wins over the commented-out brand value
    assert subuh.stops == ["#0E1B44", "#24407F"]
    assert brand.palettes["syuruq"].accent == "#0B1430"  # _deepInk
    assert brand.palettes["isya"].on_sky_muted == "#93A0BA"  # mutedText
    assert (brand.ink, brand.panel_dark, brand.paper, brand.muted, brand.on_dark, brand.brass, brand.deep_ink, brand.on_panel_muted_light) == (
        "#070B14",
        "#0F1626",
        "#F6F4EE",
        "#93A0BA",
        "#EEF2F9",
        "#C79A55",
        "#0B1430",
        "#5A6478",
    )
    assert load_brand(Snapshot(SNAPSHOT)) == brand


def test_panel_helpers_dark_and_light(brand: Brand) -> None:
    dark, light = brand.palettes["isya"], brand.palettes["dzuhur"]
    assert (brand.panel(dark), brand.on_panel(dark), brand.on_panel_muted(dark), brand.panel_accent(dark)) == ("#0F1626", "#EEF2F9", "#93A0BA", "#C79A55")
    assert (brand.panel(light), brand.on_panel(light), brand.on_panel_muted(light), brand.panel_accent(light)) == ("#F6F4EE", "#070B14", "#5A6478", "#0B1430")
    maghrib = brand.palettes["maghrib"]
    assert brand.panel(maghrib) == brand.panel_dark  # a dark sky gets a dark panel, never the horizon colour


def test_hex() -> None:
    assert _hex("FFc79a55") == "#C79A55"
    assert _hex("00000000") == "#000000"


def test_unknown_colour_token(real_source: str) -> None:
    src = _replace_once(real_source, "onSky: ink, // 3.33:1", "onSky: inkk,")
    with pytest.raises(PaletteParseError, match="Unknown colour 'inkk'"):
        parse_brand(src)


def test_commented_constant_is_not_a_constant(real_source: str) -> None:
    src = _replace_once(real_source, "  static const Color _deepInk = Color(0xFF0B1430);", "  // static const Color _deepInk = Color(0xFF0B1430);")
    with pytest.raises(PaletteParseError, match="Unknown colour '_deepInk'"):
        parse_brand(src)


@pytest.mark.parametrize("field", ["top", "bottom", "onSky", "onSkyMuted", "accent", "isDark"])
def test_missing_field(real_source: str, field: str) -> None:
    start = real_source.index("static const ashar = SkyPalette(")
    end = real_source.index(");", start)
    body = real_source[start:end]
    lines = [ln for ln in body.splitlines() if not ln.strip().startswith(f"{field}:")]
    assert len(lines) == len(body.splitlines()) - 1
    src = real_source[:start] + "\n".join(lines) + real_source[end:]
    with pytest.raises(PaletteParseError, match=f"Palette ashar is missing '{field}'"):
        parse_brand(src)


def test_optional_fields_default(real_source: str) -> None:
    src = _replace_once(real_source, "    mid: Color(0xFFD06A78),\n", "")
    src = _replace_once(src, "    requiresScrim: true,\n  );\n\n  /// Night", "  );\n\n  /// Night")
    m = parse_brand(src).palettes["maghrib"]
    assert m.mid is None and not m.requires_scrim
    assert m.stops == [m.top, m.bottom]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("static const isya = SkyPalette(", "static const night = SkyPalette("),
        ("static const isya = SkyPalette(", "static const isyaOld = SkyPalette("),
        ("  static const subuh = SkyPalette(", "  static const dawn = SkyPalette("),
    ],
)
def test_wrong_palette_set(real_source: str, old: str, new: str) -> None:
    with pytest.raises(PaletteParseError, match="Expected palettes"):
        parse_brand(_replace_once(real_source, old, new))


def test_extra_palette_rejected(real_source: str) -> None:
    extra = "  static const tahajjud = SkyPalette(top: ink, bottom: ink, onSky: paper, onSkyMuted: paper, accent: _brass, isDark: true);\n}"
    src = real_source.rstrip()[:-1] + extra
    with pytest.raises(PaletteParseError, match="tahajjud"):
        parse_brand(src)


def test_no_palettes_rejected() -> None:
    with pytest.raises(PaletteParseError, match="Expected palettes"):
        parse_brand("")


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("  static const Color paper = Color(0xFFF6F4EE);", "paper"),
        ("  static const Color panelDark = Color(0xFF0F1626);", "panelDark"),
        ("  static const Color _brass = Color(0xFFC79A55);", "_brass"),
    ],
)
def test_missing_brand_constant(real_source: str, line: str, name: str) -> None:
    src = _replace_once(real_source, line, "")
    if name == "_brass":
        src = src.replace("accent: _brass,", "accent: _brassLight,")
    with pytest.raises(PaletteParseError, match=f"missing a brand constant: '{name}'"):
        parse_brand(src)


def test_missing_on_panel_muted(real_source: str) -> None:
    src = _replace_once(real_source, "Color get onPanelMuted => isDark ? mutedText : const Color(0xFF5A6478);", "")
    with pytest.raises(PaletteParseError, match="missing a brand constant"):
        parse_brand(src)
