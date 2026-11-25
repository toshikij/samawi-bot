"""One real write → check → judge run against a brief, printing the outcome (no rendering, no DB).

    ANTHROPIC_API_KEY=… uv run python scripts/trial_write.py fakta_quran 2026-11-26
"""

import json
import sys
from datetime import date
from pathlib import Path

import anthropic

from samawi_bot.brain.llm import ClaudeLLM
from samawi_bot.brain.planner import BRIDGES, SERIES_TITLES
from samawi_bot.brain.writer import Brief, write_verified
from samawi_bot.checks.draft_checks import CheckContext
from samawi_bot.config import LaunchMode
from samawi_bot.sources import Sources

pillar = sys.argv[1] if len(sys.argv) > 1 else "fakta_quran"
day = date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else date.today()
hadith_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("tests/fixtures/hadith")
sources = Sources.load(Path("snapshot"), hadith_dir)
llm = ClaudeLLM(anthropic.Anthropic(max_retries=3, timeout=600))
brief = Brief(day=day, slot="siang_feed", pillar=pillar, format="carousel", hook_formula="ternyata",
              bridge_options=BRIDGES[pillar], sky_moment="dzuhur",
              series={"id": f"{pillar}-{day}", "title": SERIES_TITLES[pillar], "part": 1, "total": 5})
attempts = write_verified(llm, sources, brief, CheckContext(today=day, launch_mode=LaunchMode.PRELAUNCH))
out = Path("out/trial"); out.mkdir(parents=True, exist_ok=True)
for i, a in enumerate(attempts, 1):
    print(f"--- attempt {i}: passed={a.passed}")
    print(a.notes() or "(no issues)")
    if a.draft:
        (out / f"{pillar}-{i}.json").write_text(a.draft.model_dump_json(indent=1))
        print("hooks:", *a.draft.hooks, sep="\n  ")
    if a.judge:
        print("judge:", a.judge.model_dump_json())
print("usage:", llm.usage)
