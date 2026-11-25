"""Turns a verified draft into slide specs. Scripture text is pulled from sources here, never from the
draft, and long passages are split across slides rather than shrunk below the minimum size."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, replace
from typing import Any

from ..brain.models import Draft, Slide
from ..config import LaunchMode, TranslationMode
from ..sources import Sources
from ..sources.palette import SkyPalette
from ..sources.quran import parse_reference

ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
PILLAR_LABELS = {
    "langit_waktu_salat": "Langit & Waktu Salat",
    "fakta_quran": "Fakta Al-Qur'an",
    "sunnah_terlupa": "Sunnah yang Terlupa",
    "doa_dzikir": "Doa & Dzikir",
    "musafir": "Musafir",
    "hitung_ibadah": "Hitung-hitungan Ibadah",
    "kalender_hijriah": "Kalender Hijriah",
    "sejarah_kisah": "Sejarah & Kisah",
}
ARABIC_MIN_PX = 44
TRANSLATION_MIN_PX = 32


@dataclass
class SlideSpec:
    kind: str
    on_sky: bool = False
    kicker: str = ""
    heading: str = ""
    body: str = ""
    items: list[str] = field(default_factory=list)
    hook_html: str = ""
    hook_px: int = 112
    sub: str = ""
    arabic: str = ""
    translation: str = ""
    reference: str = ""
    arabic_px: int = 60
    translation_px: int = 38
    left_label: str = ""
    left_value: str = ""
    right_label: str = ""
    right_value: str = ""
    big: str = ""
    bridge: str = ""
    action: str = ""
    teaser: str = ""
    share: str = ""
    pager: str = ""
    times: list[tuple[str, str]] = field(default_factory=list)
    times_title: str = ""
    thumb: str = ""
    expected_texts: list[str] = field(default_factory=list)

    def ctx(self) -> dict[str, Any]:
        return self.__dict__


_HIGHLIGHT = re.compile(r"(\d[\d.,:]*(?:\s?(?:%|jam|menit|halaman|ayat|surah|hari|km))?|QS\.?\s?[\w'-]+(?::\s?\d+)?)")


def hook_html(hook: str) -> str:
    """Escape, mark the first number/reference as the highlight (the eye lands there first), and
    keep hyphenated words like Al-Qur'an on one line."""
    m = _HIGHLIGHT.search(hook)
    parts = [(hook, False)] if m is None else [(hook[: m.start()], False), (m.group(0), True), (hook[m.end() :], False)]
    out = []
    for text, highlighted in parts:
        esc = re.sub(r"(\S*\w-\w\S*)", r'<span class="nb">\1</span>', html.escape(text, quote=False))
        out.append(f'<span class="hl txt" data-label="highlight">{esc}</span>' if highlighted else esc)
    return "".join(out)


def hook_size(hook: str) -> int:
    n = len(hook)
    return 128 if n <= 32 else 112 if n <= 48 else 96 if n <= 64 else 84


def arabic_px(text: str) -> int:
    n = len(text)
    return 66 if n < 110 else 58 if n < 220 else 50 if n < 380 else ARABIC_MIN_PX


def translation_px(text: str) -> int:
    n = len(text)
    return 42 if n < 160 else 38 if n < 320 else 34 if n < 520 else TRANSLATION_MIN_PX


def action_line(mode: LaunchMode) -> str:
    if mode is LaunchMode.LIVE:
        return "Komentar SAMAWI, kami kirim link-nya."
    return "Segera hadir di Android. Follow biar kamu yang pertama tahu."


def ayah_marker(n: int) -> str:
    return f"﴿{str(n).translate(ARABIC_DIGITS)}﴾"


def scripture_slide(sources: Sources, slide: Slide, mode: TranslationMode) -> SlideSpec:
    assert slide.source is not None
    if slide.source.type == "ayat":
        rng = parse_reference(slide.source.ref, sources.quran)
        arabic = " ".join(f"{sources.quran.arabic(r)} {ayah_marker(r.ayah)}" for r in rng.refs)
        if mode is TranslationMode.KEMENAG:
            translation = " ".join((f"({r.ayah}) " if len(rng) > 1 else "") + sources.quran.translation(r) for r in rng.refs)
        else:
            translation = slide.body  # makna paraphrase written by the writer, checked like any copy
        spec = SlideSpec("ayat", heading=slide.heading, arabic=arabic, translation=translation, reference=sources.quran.citation(rng))
        spec.expected_texts = [arabic] + ([translation] if mode is TranslationMode.KEMENAG else [])
    elif slide.source.type == "hadith":
        h = sources.hadith.get(slide.source.ref)
        spec = SlideSpec("hadith", heading=slide.heading, translation=h.text_id, reference=f"({h.reference})")
        spec.expected_texts = [h.text_id]
    else:
        d = sources.duas.get(slide.source.ref)
        spec = SlideSpec("dua", heading=slide.heading, arabic=d.arabic, translation=d.indonesian, reference=f"({d.reference})")
        spec.expected_texts = [d.arabic, d.indonesian]
    spec.arabic_px = arabic_px(spec.arabic)
    spec.translation_px = translation_px(spec.translation)
    return spec


def split_words(text: str, parts: int) -> list[str]:
    words = text.split(" ")
    size = -(-len(words) // parts)
    return [" ".join(words[i : i + size]) for i in range(0, len(words), size)]


def paginate(spec: SlideSpec, parts: int) -> list[SlideSpec]:
    """Split a scripture slide into `parts` slides; the chunks rejoin to exactly the source text."""
    arabic = split_words(spec.arabic, parts) if spec.arabic else [""] * parts
    translation = split_words(spec.translation, parts) if spec.translation else [""] * parts
    n = max(len(arabic), len(translation))
    arabic += [""] * (n - len(arabic))
    translation += [""] * (n - len(translation))
    out = []
    for i in range(n):
        out.append(
            replace(
                spec,
                heading="",  # a split passage gets every pixel; the reference names it
                arabic=arabic[i],
                translation=translation[i],
                reference=spec.reference + ("" if i == n - 1 else " · bersambung"),
                arabic_px=max(ARABIC_MIN_PX, arabic_px(arabic[i])),
                translation_px=max(TRANSLATION_MIN_PX, translation_px(translation[i])),
                expected_texts=[t for t in (arabic[i], translation[i]) if t and any(t in e for e in spec.expected_texts)],
            )
        )
    return out


def content_slide(slide: Slide) -> SlideSpec:
    kind = slide.template if slide.template != "takeaway" else "list"
    if kind in ("map", "comparison") and not (slide.left_value and slide.right_value):
        kind = "fact"  # a map or comparison with an empty side would render blank pins or boxes
    spec = SlideSpec(
        kind=kind,
        heading=slide.heading,
        body=slide.body,
        items=list(slide.items),
        left_label=slide.left_label,
        left_value=slide.left_value,
        right_label=slide.right_label,
        right_value=slide.right_value,
    )
    if slide.template == "fact" and slide.left_value and not slide.right_value:
        spec.big, spec.left_value = slide.left_value, ""
    if slide.template == "takeaway":
        spec.kicker = "Lakukan hari ini"
    return spec


def compose_carousel(draft: Draft, hook_index: int, sources: Sources, launch_mode: LaunchMode, translation_mode: TranslationMode) -> list[SlideSpec]:
    hook = draft.hooks[hook_index]
    kicker = f"{draft.series.title} · {draft.series.part}/{draft.series.total}" if draft.series else PILLAR_LABELS.get(draft.pillar.value, "")
    slides = [SlideSpec("cover", on_sky=True, kicker=kicker, hook_html=hook_html(hook), hook_px=hook_size(hook))]
    for s in draft.slides:
        if s.template in ("ayat", "hadith", "dua") and s.source is not None:
            slides.append(scripture_slide(sources, s, translation_mode))
        else:
            slides.append(content_slide(s))
    slides.append(SlideSpec("cta", bridge=draft.bridge_line, action=action_line(launch_mode), teaser=draft.next_teaser, share=draft.share_prompt))
    return number(slides)


def number(slides: list[SlideSpec]) -> list[SlideSpec]:
    total = len(slides)
    for i, s in enumerate(slides):
        s.pager = "" if s.kind in ("cover", "cta") else f"{i + 1}/{total}"
    return slides


def theme(sources: Sources, sky: SkyPalette) -> dict[str, Any]:
    b = sources.brand
    return {
        "sky": sky,
        "brand": b,
        "panel": b.panel(sky),
        "on_panel": b.on_panel(sky),
        "on_panel_muted": b.on_panel_muted(sky),
        "panel_accent": b.panel_accent(sky),
        # Highlight: brass marker with ink text on both panels; it reads on either.
        "highlight_bg": b.brass,
        "highlight_fg": b.ink,
    }


# ── Stories (1080×1920). No captions on stories; the API cannot add stickers or polls. ──


def ayat_of_day(day_ordinal: int) -> str:
    from importlib import resources

    import yaml

    refs: list[str] = yaml.safe_load(resources.files("samawi_bot.content").joinpath("ayat_of_day.yaml").read_text("utf-8"))["refs"]
    return refs[day_ordinal % len(refs)]


def story_subuh(sources: Sources, ref: str, hijri_label: str, times: dict[str, str]) -> SlideSpec:
    rng = parse_reference(ref, sources.quran)
    arabic = " ".join(f"{sources.quran.arabic(r)} {ayah_marker(r.ayah)}" for r in rng.refs)
    translation = " ".join(sources.quran.translation(r) for r in rng.refs)
    return SlideSpec(
        "story",
        kicker=f"Ayat hari ini · {hijri_label}",
        arabic=arabic,
        translation=translation,
        reference=sources.quran.citation(rng),
        arabic_px=arabic_px(arabic) + 4,
        translation_px=translation_px(translation) + 2,
        times=list(times.items()),
        times_title="Waktu salat Jakarta hari ini (WIB)",
        expected_texts=[arabic, translation],
    )


def story_malam(cover_png_uri: str, teaser: str) -> SlideSpec:
    return SlideSpec("story", kicker="Post hari ini", heading="Sudah lihat yang ini?", thumb=cover_png_uri, body=teaser)


def story_kahfi(sources: Sources, bridge: str) -> SlideSpec:
    rng = parse_reference("18:10", sources.quran)
    arabic = sources.quran.arabic(rng.refs[0])
    translation = sources.quran.translation(rng.refs[0])
    return SlideSpec(
        "story",
        kicker="Malam Jumat",
        heading="Sudah baca Al-Kahfi pekan ini?",
        arabic=arabic,
        translation=translation,
        reference=sources.quran.citation(rng),
        arabic_px=arabic_px(arabic),
        translation_px=translation_px(translation),
        body=bridge,
        expected_texts=[arabic, translation],
    )
