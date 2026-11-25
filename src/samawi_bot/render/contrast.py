"""WCAG contrast, measured on rendered pixels (spec §7: failures block publishing).

For every text element the renderer captures the backdrop with text hidden, then checks the text
colour against every backdrop pixel under the element's box. The worst pixel decides. Gradients,
scrims and images are therefore measured as they actually render, not as declared.
"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image

AA_NORMAL = 4.5
AA_LARGE = 3.0
# Posts are shown ~1080 → ~390 pt wide on a phone (×0.36). WCAG "large" is 18 pt, or 14 pt bold;
# at that scale that is ≈ 50 px regular / 39 px bold on the 1080 canvas.
LARGE_PX = 50.0
LARGE_BOLD_PX = 39.0


def _channel(c: float) -> float:
    c /= 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def luminance(rgb: tuple[int, int, int]) -> float:
    r, g, b = rgb
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = luminance(a), luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def hex_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def required(font_px: float, weight: int) -> float:
    large = font_px >= LARGE_PX or (weight >= 700 and font_px >= LARGE_BOLD_PX)
    return AA_LARGE if large else AA_NORMAL


@dataclass(frozen=True)
class TextBox:
    label: str
    color: tuple[int, int, int]
    font_px: float
    weight: int
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class ContrastFailure:
    label: str
    ratio: float
    required: float

    def __str__(self) -> str:
        return f"{self.label}: contrast {self.ratio:.2f}:1 < {self.required}:1"


def check(backdrop_png: bytes, boxes: list[TextBox], step: int = 4) -> list[ContrastFailure]:
    img = Image.open(BytesIO(backdrop_png)).convert("RGB")
    failures = []
    for b in boxes:
        x0, y0 = max(0, b.x), max(0, b.y)
        x1, y1 = min(img.width, b.x + b.w), min(img.height, b.y + b.h)
        if x1 <= x0 or y1 <= y0:
            continue
        region = img.crop((x0, y0, x1, y1))
        if region.width > 2 * step and region.height > 2 * step:
            region = region.resize((max(1, region.width // step), max(1, region.height // step)), Image.Resampling.BOX)
        colours: set[tuple[int, int, int]] = {c for _, c in region.getcolors(maxcolors=region.width * region.height) or []}  # type: ignore[misc]
        worst = min((ratio(b.color, c) for c in colours), default=21.0)
        need = required(b.font_px, b.weight)
        if worst < need:
            failures.append(ContrastFailure(b.label, worst, need))
    return failures
