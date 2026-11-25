"""WCAG contrast maths and the pixel check that blocks publishing (spec §7)."""

from __future__ import annotations

from collections.abc import Sequence
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from samawi_bot.render.contrast import (
    AA_LARGE,
    AA_NORMAL,
    LARGE_BOLD_PX,
    LARGE_PX,
    ContrastFailure,
    TextBox,
    check,
    hex_rgb,
    luminance,
    ratio,
    required,
)

BLACK, WHITE = (0, 0, 0), (255, 255, 255)


def png(size: tuple[int, int], fill: tuple[int, int, int], rects: Sequence[tuple[tuple[int, int, int, int], tuple[int, int, int]]] = ()) -> bytes:
    img = Image.new("RGB", size, fill)
    draw = ImageDraw.Draw(img)
    for box, colour in rects:
        draw.rectangle(box, fill=colour)
    out = BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def box(color: tuple[int, int, int], x: int, y: int, w: int, h: int, px: float = 40, weight: int = 400, label: str = "t") -> TextBox:
    return TextBox(label, color, px, weight, x, y, w, h)


# ── luminance / ratio ─────────────────────────────────────────────────────────────────────────


def test_luminance_extremes() -> None:
    assert luminance(BLACK) == 0.0
    assert luminance(WHITE) == pytest.approx(1.0)


def test_luminance_uses_linear_segment_for_dark_channels() -> None:
    # 10/255 = 0.0392 ≤ 0.03928: the linear part of the sRGB curve.
    assert luminance((10, 10, 10)) == pytest.approx((10 / 255) / 12.92)


def test_black_on_white_is_21() -> None:
    assert ratio(BLACK, WHITE) == pytest.approx(21.0)
    assert ratio(WHITE, BLACK) == pytest.approx(21.0)  # symmetric


def test_same_colour_is_1() -> None:
    assert ratio((119, 119, 119), (119, 119, 119)) == pytest.approx(1.0)


def test_777_on_white_just_fails_aa() -> None:
    r = ratio(hex_rgb("#777777"), WHITE)
    assert r == pytest.approx(4.48, abs=0.01)
    assert r < AA_NORMAL


def test_brand_pairs() -> None:
    # On-dark text on panelDark, ink on paper: both comfortably AA.
    assert ratio(hex_rgb("#EEF2F9"), hex_rgb("#0F1626")) > 7
    assert ratio(hex_rgb("#070B14"), hex_rgb("#F6F4EE")) > 7


def test_hex_rgb() -> None:
    assert hex_rgb("#C79A55") == (0xC7, 0x9A, 0x55)
    assert hex_rgb("0b1430") == (0x0B, 0x14, 0x30)


# ── required() thresholds ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("px", "weight", "need"),
    [
        (LARGE_PX, 400, AA_LARGE),  # exactly large
        (LARGE_PX - 0.1, 400, AA_NORMAL),  # just under, regular
        (LARGE_PX - 0.1, 700, AA_LARGE),  # bold makes it large
        (LARGE_BOLD_PX, 700, AA_LARGE),  # exactly large-bold
        (LARGE_BOLD_PX - 0.1, 800, AA_NORMAL),  # bold but too small
        (LARGE_BOLD_PX, 699, AA_NORMAL),  # not quite bold
        (26, 700, AA_NORMAL),
        (200, 800, AA_LARGE),
    ],
)
def test_required(px: float, weight: int, need: float) -> None:
    assert required(px, weight) == need


# ── check() on synthetic backdrops ────────────────────────────────────────────────────────────


def test_check_passes_black_text_on_white() -> None:
    assert check(png((200, 100), WHITE), [box(BLACK, 10, 10, 100, 50)]) == []


def test_check_fails_low_contrast_and_reports_ratio() -> None:
    grey = hex_rgb("#777777")
    failures = check(png((200, 100), WHITE), [box(grey, 10, 10, 100, 50, label="body")])
    assert len(failures) == 1
    f = failures[0]
    assert isinstance(f, ContrastFailure)
    assert f.label == "body"
    assert f.ratio == pytest.approx(4.48, abs=0.01)
    assert f.required == AA_NORMAL
    assert str(f) == "body: contrast 4.48:1 < 4.5:1"


def test_large_text_is_held_to_3_to_1() -> None:
    grey = hex_rgb("#777777")
    assert check(png((200, 100), WHITE), [box(grey, 10, 10, 100, 50, px=LARGE_PX)]) == []


def test_worst_pixel_decides() -> None:
    # A white text box mostly over black, but a white patch under one corner: fails.
    backdrop = png((200, 200), BLACK, [((0, 0, 30, 30), WHITE)])
    failures = check(backdrop, [box(WHITE, 0, 0, 150, 150)])
    assert failures and failures[0].ratio == pytest.approx(1.0)
    # The same box shifted off the patch passes.
    assert check(backdrop, [box(WHITE, 60, 60, 100, 100)]) == []


def test_small_box_is_not_downsampled_and_still_checked() -> None:
    # 6×6 px box (≤ 2·step): measured at full resolution, a single bad pixel fails it.
    backdrop = png((50, 50), BLACK, [((12, 12, 12, 12), WHITE)])
    assert check(backdrop, [box(WHITE, 10, 10, 6, 6)])
    assert check(backdrop, [box(WHITE, 20, 20, 6, 6)]) == []


def test_each_box_reported_independently() -> None:
    backdrop = png((300, 100), WHITE, [((150, 0, 299, 99), BLACK)])
    failures = check(backdrop, [box(WHITE, 10, 10, 100, 50, label="on-white"), box(WHITE, 160, 10, 100, 50, label="on-black")])
    assert [f.label for f in failures] == ["on-white"]


@pytest.mark.parametrize(
    "b",
    [
        box(WHITE, 10, 10, 0, 50),  # zero width
        box(WHITE, 10, 10, 50, 0),  # zero height
        box(WHITE, 500, 10, 50, 50),  # entirely right of the image
        box(WHITE, 10, 500, 50, 50),  # entirely below
        box(WHITE, -100, 10, 50, 50),  # entirely left
        box(WHITE, 10, -100, 50, 50),  # entirely above
    ],
)
def test_empty_or_out_of_bounds_boxes_are_skipped(b: TextBox) -> None:
    assert check(png((200, 100), WHITE), [b]) == []


def test_partially_out_of_bounds_box_is_clipped_not_skipped() -> None:
    assert check(png((200, 100), WHITE), [box(WHITE, -20, -20, 100, 100)])


def test_rgba_backdrop_is_converted() -> None:
    img = Image.new("RGBA", (100, 100), (255, 255, 255, 255))
    out = BytesIO()
    img.save(out, "PNG")
    assert check(out.getvalue(), [box(BLACK, 0, 0, 100, 100)]) == []
