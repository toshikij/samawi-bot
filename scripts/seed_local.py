"""Seed a local SQLite database with a planned week, sample renders, runs and posts, for dashboard QA.

    uv run python scripts/seed_local.py && DATABASE_URL=sqlite:///out/local.db uv run samawi-bot admin
"""

import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

from samawi_bot.alerts import LogMailer
from samawi_bot.app import App
from samawi_bot.config import Settings
from samawi_bot.db import make_engine, make_sessionmaker, session_scope
from samawi_bot.db.migrate import upgrade
from samawi_bot.db.models import ContentPlan, DraftRow, EvergreenPost, JobRun, Metric, Post
from samawi_bot.jobs.plan import plan_days
from samawi_bot.publisher.slots import WIB
from samawi_bot.publisher.storage import LocalStorage
from samawi_bot.sources import Sources
import httpx, json

db = Path("out/local.db")
db.unlink(missing_ok=True)
url = f"sqlite:///{db}"
upgrade(url)
settings = Settings(_env_file=None, database_url=url, snapshot_dir=Path("snapshot"))
sources = Sources.load(Path("snapshot"), Path("tests/fixtures/hadith"))
now = datetime.now(UTC)
app = App(settings=settings, db=make_sessionmaker(make_engine(url)), storage=LocalStorage(Path("out/media")), mailer=LogMailer(),
          http=httpx.Client(), llm_factory=lambda: None, sources_factory=lambda: sources, now=lambda: now)  # type: ignore[arg-type,return-value]
today = now.astimezone(WIB).date()
plan_days(app, today, 7)
media = []
for i, p in enumerate(sorted(Path("out/sample/dzuhur").glob("*.png"))):
    key = f"posts/{today}/siang_feed/slide-{i + 1:02d}.png"
    (Path("out/media") / key).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(p, Path("out/media") / key)
    media.append(key)
draft = json.loads(Path("tests/fixtures/good_draft.json").read_text())
with session_scope(app.db) as s:
    feed = s.query(ContentPlan).filter_by(date=today, slot="siang_feed").one()
    feed.status, feed.bridge_feature = "ready", "tafsir"
    s.add(DraftRow(plan_id=feed.id, json=draft, check_report={"errors": []}, judge_report={"novelty_pct": 78}, attempt=1, status="passed", hook_index=0, created_at=now))
    s.add(DraftRow(plan_id=feed.id, json={"kind": "carousel", "media": media, "cover": media[0], "caption": draft["caption"], "hook": draft["hooks"][0]},
                   check_report={}, attempt=0, status="payload", created_at=now))
    tomorrow = s.query(ContentPlan).filter_by(date=today + timedelta(days=1), slot="siang_feed").one()
    s.add(DraftRow(plan_id=tomorrow.id, json=draft, check_report={"errors": ["number 283 is not backed by any cited source or computation"]}, attempt=1, status="failed", created_at=now))
    for job, status, err in (("plan", "ok", None), ("produce", "ok", None), ("publish-due", "ok", None), ("metrics", "failed", "MetaApiError: token expired")):
        s.add(JobRun(job=job, started_at=now - timedelta(hours=2), finished_at=now - timedelta(hours=2) + timedelta(minutes=3), status=status, summary={"demo": True}, error=err))
    post = Post(idempotency_key="demo", plan_id=feed.id, platform="instagram", media_type="carousel", status="published", platform_media_id="1",
                permalink="https://www.instagram.com/samawi.indonesia/", published_at=now - timedelta(days=3), hook_text=draft["hooks"][0])
    s.add(post)
    s.flush()
    s.add(Metric(post_id=post.id, captured_at=now, age_hours=72, reach=4200, saves=310, shares=145, comments=22, likes=380, follows=37, profile_visits=120))
    s.add(EvergreenPost(json=draft, pillar="fakta_quran", topic_hash="demo", hook_index=0, created_at=now))
print("seeded", db)
