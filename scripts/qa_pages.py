"""Write every slide template × sky palette as static HTML under out/qa/ for browser QA (Playwright MCP).

Serve with:  python -m http.server 8765 -d out/qa   then open http://localhost:8765/
"""

import json
import shutil
from pathlib import Path

from samawi_bot.brain.models import Draft
from samawi_bot.config import LaunchMode, TranslationMode
from samawi_bot.render.compose import compose_carousel, story_kahfi, story_subuh
from samawi_bot.render.renderer import FEED, STORY, Renderer
from samawi_bot.sources import Sources
from samawi_bot.sources.palette import MOMENTS
from samawi_bot.sources.snapshot import FONT_AMIRI, FONT_JAKARTA, LOGO_BRASS
from samawi_bot.verify.facts import prayer_times_today

OUT = Path("out/qa")
shutil.rmtree(OUT, ignore_errors=True)
(OUT / "assets").mkdir(parents=True)
sources = Sources.load(Path("snapshot"), Path("tests/fixtures/hadith"))
snap = sources.snapshot
for rel, name in ((FONT_JAKARTA, "jakarta.ttf"), (FONT_AMIRI, "amiri.ttf"), (LOGO_BRASS, "logo.png")):
    shutil.copy(snap.path(rel), OUT / "assets" / name)
rewrites = {snap.path(FONT_JAKARTA).as_uri(): "assets/jakarta.ttf", snap.path(FONT_AMIRI).as_uri(): "assets/amiri.ttf",
            snap.path(LOGO_BRASS).as_uri(): "assets/logo.png"}

draft = Draft.model_validate(json.loads(Path("tests/fixtures/good_draft.json").read_text()))
extra = Draft.model_validate({**draft.model_dump(), "slides": [
    {"template": "comparison", "heading": "Subuh di dua ujung Indonesia", "left_label": "Sabang (WIB)", "left_value": "05:11", "right_label": "Merauke (WIT)", "right_value": "03:46"},
    {"template": "map", "heading": "Selisih waktu nyata", "body": "Matahari terbit di Merauke 3 jam 25 menit lebih dulu.", "left_label": "Merauke", "left_value": "03:46", "right_label": "Sabang", "right_value": "05:11"},
    {"template": "steps", "heading": "Khatam dalam 30 hari", "items": ["Baca 4 halaman setiap habis salat", "5 kali sehari", "30 hari"]},
    {"template": "dua", "heading": "Doa bangun tidur", "source": {"type": "dua", "ref": "harian.bangun.1"}},
]})
specs = compose_carousel(draft, 0, sources, LaunchMode.PRELAUNCH, TranslationMode.KEMENAG)
specs += compose_carousel(extra, 1, sources, LaunchMode.LIVE, TranslationMode.KEMENAG)[1:-1]
r = Renderer(sources, browser=None)  # type: ignore[arg-type]
links = []
for sky in MOMENTS:
    for i, spec in enumerate(specs):
        html = r.html(spec, sky, FEED)
        for a, b in rewrites.items():
            html = html.replace(a, b)
        name = f"{sky}-{i:02d}-{spec.kind}.html"
        (OUT / name).write_text(html, "utf-8")
        links.append(name)
    for j, spec in enumerate([story_subuh(sources, "94:5-6", "16 Jumadil Akhir 1448 H", prayer_times_today(sources, "Jakarta", __import__("datetime").date(2026, 11, 26))),
                              story_kahfi(sources, "Mode Jumat di Samawi mengingatkanmu setiap Jumat, tanpa iklan.")]):
        html = r.html(spec, sky, STORY)
        for a, b in rewrites.items():
            html = html.replace(a, b)
        name = f"{sky}-story-{j}.html"
        (OUT / name).write_text(html, "utf-8")
        links.append(name)
(OUT / "index.html").write_text("<!doctype html><meta charset=utf-8><title>QA</title><ul>" + "".join(f'<li><a href="{n}">{n}</a></li>' for n in links) + "</ul>", "utf-8")
print(len(links), "pages")
