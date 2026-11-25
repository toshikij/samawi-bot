"""`plan` (Sun 20:00 WIB): refresh sources, plan the next 7 days, keep the evergreen bank topped up."""

from __future__ import annotations

from datetime import UTC, date, timedelta
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..alerts import safe_send
from ..app import App
from ..brain.models import Pillar
from ..brain.planner import INITIAL_HOOK_WEIGHTS, INITIAL_PILLAR_WEIGHTS, SeriesState, plan_week
from ..config import LaunchMode
from ..db import session_scope
from ..db.models import ContentPlan, EvergreenPost, Flag, Post, Weight
from ..publisher.slots import WIB, slots_for
from ..sources.hadith import HadithCache, ingest
from ..sources.snapshot import github_fetcher, refresh_snapshot

EVERGREEN_RESERVE = 14
LAUNCH_FLAG = "launch_series_planned"


def weights(s: Session, dimension: str, default: dict[str, float]) -> dict[str, float]:
    rows = s.scalars(select(Weight).where(Weight.dimension == dimension)).all()
    got = {r.key: r.weight for r in rows if r.key in default}
    return {**default, **got} if got else dict(default)


def _recent(s: Session, today: date) -> tuple[list[str], list[str], int, int, dict[str, SeriesState]]:
    rows = s.scalars(select(ContentPlan).where(ContentPlan.slot == "siang_feed", ContentPlan.date < today).order_by(ContentPlan.date.desc()).limit(60)).all()
    pillars = [r.pillar for r in rows if r.pillar]
    bridges = [r.bridge_feature or (r.bridge_options or [None])[0] for r in rows]
    bridges = [b for b in bridges if b]
    catalog_paid = 0
    series: dict[str, SeriesState] = {}
    for r in reversed(rows):
        if r.pillar and r.series_id and r.series_id != "launch" and r.series_part and r.series_total:
            series[r.pillar] = SeriesState(r.series_id, r.pillar, r.series_part, r.series_total)
    return pillars, bridges, catalog_paid, len(bridges), series


def plan_days(app: App, start: date, days: int = 7) -> list[ContentPlan]:
    sources = app.sources
    with session_scope(app.db) as s:
        pillars, bridges, _, total, series = _recent(s, start)
        paid = sum(1 for b in bridges if b in sources.features.features and sources.features.features[b].paid)
        plans = plan_week(
            start,
            sources.features,
            weights(s, "pillar", {str(k): v for k, v in INITIAL_PILLAR_WEIGHTS.items()}),
            weights(s, "hook_formula", INITIAL_HOOK_WEIGHTS),
            series,
            pillars,
            bridges,
            paid,
            total,
            seed=start.toordinal(),
            days=days,
        )
        launch = _launch_overrides(app, s, start)
        # Continue a launch series that a shorter earlier run only partly planned.
        launched = int(s.scalar(select(func.count()).select_from(ContentPlan).where(ContentPlan.series_id == "launch")) or 0) if launch else 0
        created: list[ContentPlan] = []
        for p in plans:
            times = {t.spec.name: t for t in slots_for(p.day)}
            for name, t in times.items():
                existing = s.scalar(select(ContentPlan).where(ContentPlan.date == p.day, ContentPlan.slot == name))
                if existing is not None:
                    continue
                row = ContentPlan(date=p.day, slot=name, scheduled_at=t.at.astimezone(UTC), sky_moment=t.spec.sky, status="planned", bridge_options=[])
                if name == "siang_feed":
                    row.pillar, row.format, row.hook_formula = p.pillar, p.format, p.hook_formula
                    row.series_id, row.series_part, row.series_total = p.series_id, p.series_part, p.series_total
                    row.bridge_options = p.bridge_options
                    if launched < len(launch):
                        row.series_id, row.series_part, row.series_total = "launch", launched + 1, len(launch)
                        row.bridge_options = launch[launched]
                        launched += 1
                s.add(row)
                created.append(row)
        if launch and launched == len(launch):
            # Only once all three launch posts are actually planned; otherwise retry next plan.
            s.add(Flag(name=LAUNCH_FLAG, value=start.isoformat(), updated_at=app.now(), updated_by="plan"))
        s.flush()
        for r in created:
            s.expunge(r)
        return created


def _launch_overrides(app: App, s: Session, start: date) -> list[list[str]]:
    """When LAUNCH_MODE flips to live, the next three feed posts are the launch series (§8.4)."""
    if app.settings.launch_mode is not LaunchMode.LIVE or not app.settings.play_store_url:
        return []
    if s.get(Flag, LAUNCH_FLAG) is not None:
        return []
    top = [
        k
        for k, _ in (
            s.execute(
                select(Post.bridge_feature, func.count())
                .where(Post.bridge_feature.is_not(None))
                .group_by(Post.bridge_feature)
                .order_by(func.count().desc())
                .limit(3)
            ).all()
        )
        if k
    ]
    defaults = ["prayerTimes", "adzan", "quranText"]
    return [["prayerTimes"], (top or defaults)[:3], ["primaryTranslation"]]


def refresh_sources(app: App) -> dict[str, Any]:
    out: dict[str, Any] = {}
    st = app.settings
    if st.samawi_repo_token:
        fetch, sha = github_fetcher(app.http, st.samawi_repo, st.samawi_repo_ref, st.samawi_repo_token)
        refresh_snapshot(st.snapshot_dir, fetch, sha)
        app._sources = None
        out["snapshot"] = sha[:12]
    else:
        out["snapshot"] = "bundled (no SAMAWI_REPO_TOKEN)"
    report = ingest(app.http, HadithCache(app.hadith_dir), st.hadeethenc_base_url, st.fawaz_hadith_base_url, now=app.now, sleep=app.sleep)
    out["hadith"] = report.as_dict()
    if report.stale_days is not None:
        safe_send(
            app.mailer,
            "WARN hadith snapshot stale",
            f"HadeethEnc was unreachable; the cached snapshot is {report.stale_days} days old. Production continues on it.",
        )
    app._sources = None
    return out


def evergreen_reserve(app: App) -> int:
    with session_scope(app.db) as s:
        return int(s.scalar(select(func.count()).select_from(EvergreenPost).where(EvergreenPost.retired.is_(False))) or 0)


def run_plan(app: App) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    try:
        summary["sources"] = refresh_sources(app)
    except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
        summary["sources_error"] = str(exc)
        safe_send(app.mailer, "WARN source refresh failed", f"{exc}\nPlanning continues on the existing snapshot.")
    tomorrow = app.now().astimezone(WIB).date() + timedelta(days=1)
    created = plan_days(app, tomorrow)
    summary["planned_feed"] = [f"{p.date} {p.pillar} {p.format}" for p in created if p.slot == "siang_feed"]
    reserve = evergreen_reserve(app)
    summary["evergreen_reserve"] = reserve
    if reserve < EVERGREEN_RESERVE:
        from .evergreen import top_up

        summary["evergreen_added"] = top_up(app, EVERGREEN_RESERVE - reserve)
    return summary


PILLARS_EVERGREEN = [p.value for p in Pillar if p is not Pillar.KALENDER]
