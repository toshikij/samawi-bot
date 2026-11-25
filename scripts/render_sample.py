"""Render the sample draft into out/sample/ for a visual check (no Claude, no Meta)."""
import json
import sys
from pathlib import Path

from samawi_bot.brain.models import Draft
from samawi_bot.config import LaunchMode, TranslationMode
from samawi_bot.render.compose import compose_carousel
from samawi_bot.render.renderer import Renderer, chromium
from samawi_bot.sources import Sources

sky = sys.argv[1] if len(sys.argv) > 1 else "dzuhur"
out = Path("out/sample") / sky
out.mkdir(parents=True, exist_ok=True)
sources = Sources.load(Path("snapshot"), Path(".cache/hadith"))
draft = Draft.model_validate(json.loads(Path("scripts/sample_draft.json").read_text()))
specs = compose_carousel(draft, 0, sources, LaunchMode.PRELAUNCH, TranslationMode.KEMENAG)
with chromium() as browser:
    rendered = Renderer(sources, browser).render_slides(specs, sky)
for i, r in enumerate(rendered, 1):
    (out / f"{i:02d}-{r.spec.kind}.png").write_bytes(r.png)
print(f"{len(rendered)} slides → {out}")
