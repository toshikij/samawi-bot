"""Renders slide specs to PNG with headless Chromium, and refuses to output anything wrong.

Every render is checked before it is returned:
- both brand fonts actually loaded (a missing font is a hard failure, never a fallback font);
- nothing overflows its box (no clipped glyphs, no text running off the slide);
- every scripture element's rendered text equals its source after NFC normalisation;
- every text element meets WCAG AA against the pixels actually behind it.
"""

from __future__ import annotations

import re
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup, escape
from playwright.sync_api import Browser, Page, sync_playwright

from ..sources import Sources
from ..sources.snapshot import FONT_AMIRI, FONT_JAKARTA, LOGO_BRASS
from .compose import SlideSpec, number, paginate, theme
from .contrast import TextBox, check, hex_rgb

FEED = (1080, 1350)
STORY = (1080, 1920)
MAX_PAGES = 4


class RenderError(RuntimeError):
    pass


class Overflow(RenderError):
    pass


def nb(text: str) -> Markup:
    """Escape, and keep hyphenated words (Al-Baqarah, utang-piutang) from breaking at the hyphen."""
    return Markup(  # noqa: S704 — escaped first; only our own span is added
        re.sub(r"(\S*\w-\w\S*)", r'<span class="nb">\1</span>', str(escape(text)))
    )


def _env() -> Environment:
    path = Path(str(resources.files("samawi_bot.render").joinpath("templates")))
    env = Environment(loader=FileSystemLoader(path), undefined=StrictUndefined, autoescape=True)
    env.filters["nb"] = nb
    return env


_MEASURE_JS = """
() => {
  const out = [];
  for (const el of document.querySelectorAll('.txt')) {
    // The text's own glyph area, not the element's box: padding and rounded corners of a chip
    // must not count as "behind the text".
    const range = document.createRange(); range.selectNodeContents(el);
    const rects = [...range.getClientRects()].filter(q => q.width > 0 && q.height > 0);
    const er = el.getBoundingClientRect();
    const r = rects.length ? {x: Math.min(...rects.map(q => q.left)), y: Math.min(...rects.map(q => q.top)),
      right: Math.max(...rects.map(q => q.right)), bottom: Math.max(...rects.map(q => q.bottom))} : {x: er.x, y: er.y, right: er.right, bottom: er.bottom};
    r.width = r.right - r.x; r.height = r.bottom - r.y; r.left = r.x; r.top = r.y;
    const cs = getComputedStyle(el);
    out.push({label: el.dataset.label || el.className, color: cs.color, size: parseFloat(cs.fontSize),
              weight: parseInt(cs.fontWeight), x: r.x, y: r.y, w: r.width, h: r.height,
              text: el.textContent, expected: el.dataset.expected ?? null,
              overflow: el.scrollHeight > el.clientHeight + Math.max(2, 0.3 * parseFloat(cs.fontSize)) || el.scrollWidth > el.clientWidth + 2,
              outside: r.bottom > window.innerHeight - 40 || r.right > window.innerWidth || r.left < 0 || r.top < 0});
  }
  // Two text elements drawn over each other (neither inside the other) are always a layout fault,
  // whatever their contrast: e.g. long content running under the handle.
  const els = [...document.querySelectorAll('.txt')];
  const overlaps = [];
  for (let i = 0; i < out.length; i++) for (let j = i + 1; j < out.length; j++) {
    const a = out[i], b = out[j];
    if (!a.text.trim() || !b.text.trim() || els[i].contains(els[j]) || els[j].contains(els[i])) continue;
    const w = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
    const h = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
    if (w > 2 && h > 2) overlaps.push(a.label + ' × ' + b.label);
  }
  const fit = document.getElementById('fit');
  const canvas = document.getElementById('canvas');
  const fitOverflow = fit ? fit.scrollHeight > fit.clientHeight + 2 : false;
  const canvasOverflow = canvas.scrollHeight > canvas.clientHeight + 2;
  return {boxes: out, overlaps, fitOverflow, canvasOverflow};
}
"""

_FONTS_JS = """
async () => {
  try { await document.fonts.load('800 40px SamawiSans', 'Samawi'); await document.fonts.load('40px SamawiQuran', 'سماوي'); } catch (e) {}
  await document.fonts.ready;
  const sans = document.fonts.check('800 40px SamawiSans');
  const quran = document.fonts.check('40px SamawiQuran', 'سماوي');
  const loaded = [...document.fonts].filter(f => f.status === 'loaded').map(f => f.family.replaceAll('"',''));
  return {sans, quran, loaded};
}
"""


def _rgb(css: str) -> tuple[int, int, int]:
    nums = css[css.index("(") + 1 : css.index(")")].replace("/", " ").replace(",", " ").split()
    return int(float(nums[0])), int(float(nums[1])), int(float(nums[2]))


@dataclass
class Rendered:
    png: bytes
    spec: SlideSpec


@dataclass
class Renderer:
    sources: Sources
    browser: Browser

    def _assets(self) -> dict[str, Any]:
        snap = self.sources.snapshot
        return {
            "fonts": {"jakarta": snap.path(FONT_JAKARTA).as_uri(), "amiri": snap.path(FONT_AMIRI).as_uri()},
            "logo": snap.path(LOGO_BRASS).as_uri(),
        }

    def html(self, spec: SlideSpec, sky_name: str, size: tuple[int, int]) -> str:
        sky = self.sources.brand.palettes[sky_name]
        return _env().get_template("slide.html.j2").render(s=spec, width=size[0], height=size[1], **theme(self.sources, sky), **self._assets())

    def render_one(self, spec: SlideSpec, sky_name: str, size: tuple[int, int] = FEED) -> bytes:
        page: Page = self.browser.new_page(viewport={"width": size[0], "height": size[1]}, device_scale_factor=1)
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as fh:
                fh.write(self.html(spec, sky_name, size))
            page.goto(Path(fh.name).as_uri())
            Path(fh.name).unlink()
            fonts = page.evaluate(_FONTS_JS)
            if not (fonts["sans"] and fonts["quran"]) or not {"SamawiSans", "SamawiQuran"} <= set(fonts["loaded"]):
                raise RenderError(f"brand fonts did not load: {fonts}")
            measured = page.evaluate(_MEASURE_JS)
            if measured["fitOverflow"]:
                raise Overflow(f"{spec.kind} slide content does not fit")
            problems = [b["label"] for b in measured["boxes"] if b["overflow"] or b["outside"]]
            if problems or measured["canvasOverflow"]:
                raise Overflow(f"{spec.kind} slide overflows: {problems or 'canvas'}")
            if measured["overlaps"]:
                raise Overflow(f"{spec.kind} slide text overlaps: {measured['overlaps']}")
            for b in measured["boxes"]:
                if b["expected"] is not None and unicodedata.normalize("NFC", b["text"]) != unicodedata.normalize("NFC", b["expected"]):
                    raise RenderError(f"{b['label']} rendered text differs from source")
            for expected in spec.expected_texts:
                if not any(unicodedata.normalize("NFC", b["text"]) == unicodedata.normalize("NFC", expected) for b in measured["boxes"]):
                    raise RenderError(f"source text missing from {spec.kind} slide")
            png = page.screenshot(type="png")
            page.add_style_tag(content=".txt{color:transparent!important;text-shadow:none!important}")
            with_markers = page.screenshot(type="png")  # highlights' own brass marker still showing
            page.add_style_tag(content=".hl{background:transparent!important}")
            plain = page.screenshot(type="png")
            boxes = [
                TextBox(b["label"], _rgb(b["color"]), b["size"], b["weight"], int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"]))
                for b in measured["boxes"]
                if b["text"].strip()
            ]
            # Highlighted words are measured against their marker; everything else against the
            # backdrop without markers, since a parent's own colour never sits on the marker.
            failures = check(with_markers, [b for b in boxes if b.label == "highlight"])
            failures += check(plain, [b for b in boxes if b.label != "highlight"])
            if failures:
                raise RenderError(f"{spec.kind} slide contrast below WCAG AA: " + "; ".join(map(str, failures)))
            return png
        finally:
            page.close()

    def render_slides(self, specs: list[SlideSpec], sky_name: str, size: tuple[int, int] = FEED) -> list[Rendered]:
        """Renders each slide; a scripture slide that overflows is split, up to MAX_PAGES.

        Pagination changes the slide count, so pages are settled first and numbered, then rendered.
        """
        settled: list[SlideSpec] = []
        for spec in specs:
            for parts in range(1, MAX_PAGES + 1):
                pieces = [spec] if parts == 1 else paginate(spec, parts)
                try:
                    for p in pieces:
                        self.render_one(p, sky_name, size)
                    settled += pieces
                    break
                except Overflow:
                    if spec.kind not in ("ayat", "hadith", "dua") or parts == MAX_PAGES:
                        raise
        if any(s.pager for s in specs):
            number(settled)
        return [Rendered(self.render_one(p, sky_name, size), p) for p in settled]


@contextmanager
def chromium() -> Iterator[Browser]:
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--font-render-hinting=none", "--disable-gpu"])
        try:
            yield browser
        finally:
            browser.close()


def brand_hex(sources: Sources) -> tuple[int, int, int]:
    return hex_rgb(sources.brand.brass)
