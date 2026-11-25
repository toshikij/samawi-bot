"""Sky palettes, parsed from the app's `sky_palette.dart` rather than copied by hand (spec §7)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .snapshot import SKY_PALETTE, Snapshot

MOMENTS = ("subuh", "syuruq", "dzuhur", "ashar", "maghrib", "isya")


@dataclass(frozen=True)
class SkyPalette:
    name: str
    top: str
    bottom: str
    mid: str | None
    on_sky: str
    on_sky_muted: str
    accent: str
    is_dark: bool
    requires_scrim: bool

    @property
    def stops(self) -> list[str]:
        return [self.top, self.mid, self.bottom] if self.mid else [self.top, self.bottom]


@dataclass(frozen=True)
class Brand:
    palettes: dict[str, SkyPalette]
    ink: str
    panel_dark: str
    paper: str
    muted: str
    on_dark: str
    brass: str
    deep_ink: str
    on_panel_muted_light: str

    def panel(self, p: SkyPalette) -> str:
        return self.panel_dark if p.is_dark else self.paper

    def on_panel(self, p: SkyPalette) -> str:
        return self.on_dark if p.is_dark else self.ink

    def on_panel_muted(self, p: SkyPalette) -> str:
        return self.muted if p.is_dark else self.on_panel_muted_light

    def panel_accent(self, p: SkyPalette) -> str:
        """Brass on dark panels, deep ink on paper (brand v3)."""
        return self.brass if p.is_dark else self.deep_ink


class PaletteParseError(ValueError):
    pass


def _hex(argb: str) -> str:
    return "#" + argb[-6:].upper()


_CONST_RE = re.compile(r"static const Color (\w+) = Color\(0x([0-9A-Fa-f]{8})\);")
_PALETTE_RE = re.compile(r"static const (\w+) = SkyPalette\((.*?)\);", re.S)
_FIELD_RE = re.compile(r"(\w+):\s*(Color\(0x[0-9A-Fa-f]{8}\)|[\w]+)")


def parse_brand(dart_source: str) -> Brand:
    src = re.sub(r"//[^\n]*", "", dart_source)
    consts = {name: _hex(v) for name, v in _CONST_RE.findall(src)}

    def resolve(token: str) -> str:
        m = re.match(r"Color\(0x([0-9A-Fa-f]{8})\)", token)
        if m:
            return _hex(m[1])
        if token in consts:
            return consts[token]
        raise PaletteParseError(f"Unknown colour {token!r} in sky_palette.dart")

    palettes: dict[str, SkyPalette] = {}
    for name, body in _PALETTE_RE.findall(src):
        fields = dict(_FIELD_RE.findall(body))
        try:
            palettes[name] = SkyPalette(
                name=name,
                top=resolve(fields["top"]),
                bottom=resolve(fields["bottom"]),
                mid=resolve(fields["mid"]) if "mid" in fields else None,
                on_sky=resolve(fields["onSky"]),
                on_sky_muted=resolve(fields["onSkyMuted"]),
                accent=resolve(fields["accent"]),
                is_dark=fields["isDark"] == "true",
                requires_scrim=fields.get("requiresScrim") == "true",
            )
        except KeyError as exc:
            raise PaletteParseError(f"Palette {name} is missing {exc}") from exc
    if set(palettes) != set(MOMENTS):
        raise PaletteParseError(f"Expected palettes {MOMENTS}, found {sorted(palettes)}")
    try:
        return Brand(
            palettes=palettes,
            ink=consts["ink"],
            panel_dark=consts["panelDark"],
            paper=consts["paper"],
            muted=consts["mutedText"],
            on_dark=consts["onDarkText"],
            brass=consts["_brass"],
            deep_ink=consts["_deepInk"],
            on_panel_muted_light=_hex(re.search(r"onPanelMuted =>.*?Color\(0x([0-9A-Fa-f]{8})\)", src, re.S)[1]),  # type: ignore[index]
        )
    except (KeyError, TypeError) as exc:
        raise PaletteParseError(f"sky_palette.dart is missing a brand constant: {exc}") from exc


def load_brand(snap: Snapshot) -> Brand:
    return parse_brand(snap.read_text(SKY_PALETTE))
