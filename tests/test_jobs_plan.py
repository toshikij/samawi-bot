"""`plan`: refresh sources, plan 7 days of slots, launch series, keep the evergreen bank topped up."""

from __future__ import annotations

import itertools
import json
import shutil
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from conftest import FIXTURES, NOW, SNAPSHOT, TODAY
from samawi_bot.app import App
from samawi_bot.brain.planner import INITIAL_HOOK_WEIGHTS
from samawi_bot.db import session_scope
from samawi_bot.db.models import ContentPlan, EvergreenPost, Flag, Post, Weight
from samawi_bot.jobs import evergreen as evergreen_mod
from samawi_bot.jobs.plan import (
    EVERGREEN_RESERVE,
    LAUNCH_FLAG,
    PILLARS_EVERGREEN,
    evergreen_reserve,
    plan_days,
    refresh_sources,
    run_plan,
    weights,
)
from samawi_bot.publisher.slots import SLOTS, WIB
from samawi_bot.sources.snapshot import MANIFEST

T0 = datetime(2026, 11, 1, tzinfo=UTC)


def _plans(app: App) -> list[ContentPlan]:
    with session_scope(app.db) as s:
        rows = s.scalars(select(ContentPlan).order_by(ContentPlan.date, ContentPlan.scheduled_at)).all()
        for r in rows:
            s.expunge(r)
        return list(rows)


def _feeds(app: App) -> list[ContentPlan]:
    return [p for p in _plans(app) if p.slot == "siang_feed"]


# ── plan_days ─────────────────────────────────────────────────────────────────────────────────


def test_plan_days_creates_every_slot_including_thursday_kahfi(make_app: Callable[..., App]) -> None:
    app = make_app()
    created = plan_days(app, TODAY, 7)
    assert len(created) == 7 * len(SLOTS) + 1  # TODAY is the only Thursday in the window
    plans = _plans(app)
    assert len(plans) == len(created)
    by_day: dict[date, set[str]] = {}
    for p in plans:
        by_day.setdefault(p.date, set()).add(p.slot)
    assert sorted(by_day) == [TODAY + timedelta(days=i) for i in range(7)]
    base = {s.name for s in SLOTS}
    for day, slots in by_day.items():
        assert slots == base | ({"kahfi_story"} if day.weekday() == 3 else set())
    for p in plans:
        assert p.status == "planned" and p.scheduled_at is not None
        assert p.sky_moment == {"siang_feed": "dzuhur", "kahfi_story": "maghrib"}.get(p.slot, p.sky_moment)
        if p.slot == "siang_feed":
            assert p.pillar and p.format in ("carousel", "reel") and p.hook_formula and p.series_id and p.bridge_options
            assert 1 <= (p.series_part or 0) <= (p.series_total or 0)
        else:
            assert p.pillar is None and p.bridge_options == []
    feeds = {p.date: p for p in plans if p.slot == "siang_feed"}
    assert feeds[TODAY + timedelta(days=1)].pillar == "sunnah_terlupa"  # Friday
    assert feeds[TODAY].format == "reel" and feeds[TODAY + timedelta(days=1)].format == "carousel"
    kahfi = next(p for p in plans if p.slot == "kahfi_story")
    assert kahfi.date == TODAY


def test_plan_days_is_idempotent(make_app: Callable[..., App]) -> None:
    app = make_app()
    first = plan_days(app, TODAY, 7)
    before = [(p.date, p.slot, p.pillar) for p in _plans(app)]
    assert plan_days(app, TODAY, 7) == []
    assert [(p.date, p.slot, p.pillar) for p in _plans(app)] == before
    # Overlapping windows only add the new days.
    more = plan_days(app, TODAY + timedelta(days=5), 3)
    assert {p.date for p in more} == {TODAY + timedelta(days=7)}
    assert len(_plans(app)) == len(first) + len(SLOTS) + 1  # TODAY+7 is a Thursday too


def test_series_continue_across_weeks(make_app: Callable[..., App]) -> None:
    app = make_app()
    plan_days(app, TODAY, 7)
    week1 = _feeds(app)
    plan_days(app, TODAY + timedelta(days=7), 7)
    week2 = [p for p in _feeds(app) if p.date >= TODAY + timedelta(days=7)]
    last: dict[str, ContentPlan] = {}
    for p in week1:
        last[p.pillar or ""] = p
    seen: set[str] = set()
    for p in week2:
        pillar = p.pillar or ""
        if pillar in seen or pillar not in last:
            continue
        seen.add(pillar)
        prev = last[pillar]
        if (prev.series_part or 0) < (prev.series_total or 0):
            assert (p.series_id, p.series_part) == (prev.series_id, (prev.series_part or 0) + 1)
        else:
            assert p.series_id != prev.series_id and p.series_part == 1
    assert seen  # at least one pillar recurs, so continuity was exercised
    # No pillar two days running, except Friday's fixed Jumat sunnah.
    feeds = _feeds(app)
    assert all(a.pillar != b.pillar for a, b in itertools.pairwise(feeds) if b.date.weekday() != 4)


def test_history_without_series_or_pillar_is_tolerated(make_app: Callable[..., App]) -> None:
    app = make_app()
    with session_scope(app.db) as s:
        s.add(ContentPlan(date=TODAY - timedelta(days=2), slot="siang_feed", pillar="musafir", bridge_options=["travelMode"], status="published"))
        s.add(ContentPlan(date=TODAY - timedelta(days=1), slot="siang_feed", pillar=None, bridge_options=[], status="fallback"))
    plan_days(app, TODAY, 2)
    feeds = [p for p in _feeds(app) if p.date >= TODAY]
    assert len(feeds) == 2 and all(p.series_part == 1 for p in feeds if p.pillar == "musafir")


def test_launch_series_overrides_next_three_feeds_once(make_app: Callable[..., App]) -> None:
    app = make_app(launch_mode="live", play_store_url="https://play.google.com/store/apps/details?id=x")
    with session_scope(app.db) as s:
        for i, bridge in enumerate(["adzan", "adzan", "adzan", "qibla", "qibla", "tafsir", "khatamPlanner", None, None, None, None]):
            s.add(Post(idempotency_key=f"k{i}", platform="instagram", media_type="carousel", bridge_feature=bridge))
    plan_days(app, TODAY, 7)
    feeds = _feeds(app)
    launch = feeds[:3]
    assert [(p.series_id, p.series_part, p.series_total) for p in launch] == [("launch", 1, 3), ("launch", 2, 3), ("launch", 3, 3)]
    assert [p.bridge_options for p in launch] == [["prayerTimes"], ["adzan", "qibla", "tafsir"], ["primaryTranslation"]]
    assert all(p.series_id != "launch" for p in feeds[3:])
    with session_scope(app.db) as s:
        flag = s.get(Flag, LAUNCH_FLAG)
        assert flag is not None and flag.value == TODAY.isoformat() and flag.updated_by == "plan"
    plan_days(app, TODAY + timedelta(days=7), 7)  # flag set: never again
    assert all(p.series_id != "launch" for p in _feeds(app)[3:])


def test_launch_series_without_post_history_uses_default_bridges(make_app: Callable[..., App]) -> None:
    app = make_app(launch_mode="live", play_store_url="https://play.google.com/x")
    plan_days(app, TODAY, 3)
    assert _feeds(app)[1].bridge_options == ["prayerTimes", "adzan", "quranText"]


@pytest.mark.parametrize("settings", [{"launch_mode": "live"}, {"launch_mode": "prelaunch", "play_store_url": "https://play.google.com/x"}])
def test_no_launch_series_unless_live_with_store_url(make_app: Callable[..., App], settings: dict[str, Any]) -> None:
    app = make_app(**settings)
    plan_days(app, TODAY, 3)
    assert all(p.series_id != "launch" for p in _feeds(app))
    with session_scope(app.db) as s:
        assert s.get(Flag, LAUNCH_FLAG) is None


def test_weights_override_defaults_only_for_known_keys(make_app: Callable[..., App]) -> None:
    app = make_app()
    default = {"a": 0.5, "b": 0.5}
    with session_scope(app.db) as s:
        assert weights(s, "hook_formula", default) == default
        assert weights(s, "hook_formula", default) is not default
        s.add(Weight(dimension="hook_formula", key="a", weight=0.9, updated_at=T0))
        s.add(Weight(dimension="hook_formula", key="zzz", weight=0.1, updated_at=T0))
        s.add(Weight(dimension="pillar", key="b", weight=0.2, updated_at=T0))
        s.flush()
        assert weights(s, "hook_formula", default) == {"a": 0.9, "b": 0.5}
        s.add(Weight(dimension="other", key="zzz", weight=0.1, updated_at=T0))
        s.flush()
        assert weights(s, "other", default) == default  # only unknown keys: defaults
    assert set(INITIAL_HOOK_WEIGHTS)


def test_learned_pillar_weights_steer_the_plan(make_app: Callable[..., App]) -> None:
    app = make_app()
    with session_scope(app.db) as s:
        for p in ("langit_waktu_salat", "fakta_quran", "sunnah_terlupa", "doa_dzikir", "musafir", "hitung_ibadah", "kalender_hijriah", "sejarah_kisah"):
            s.add(Weight(dimension="pillar", key=p, weight=1.0 if p in ("musafir", "doa_dzikir") else 1e-9, updated_at=T0))
    plan_days(app, TODAY, 7)
    pillars = {p.pillar for p in _feeds(app) if p.date.weekday() != 4}
    assert pillars <= {"musafir", "doa_dzikir"}


# ── refresh_sources ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def snap_dir(tmp_path: Path) -> Path:
    """A private snapshot copy; the hadith cache lands next to it (tmp_path/hadith)."""
    d = tmp_path / "snapshot"
    shutil.copytree(SNAPSHOT, d)
    return d


def _fawaz(name: str) -> dict[str, Any]:
    return {"hadiths": json.loads((FIXTURES / "hadith" / "fawaz" / f"{name}.json").read_text("utf-8"))}


def make_handler(
    github: int = 200, hadeethenc: int = 200, sha: str = "0123456789abcdef0123"
) -> tuple[Callable[[httpx.Request], httpx.Response], list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        url, path = str(req.url), req.url.path
        if req.url.host == "api.github.com":
            if github != 200:
                return httpx.Response(github, json={"message": "nope"})
            if "/commits/" in path:
                return httpx.Response(200, json={"sha": sha})
            rel = path.split("/contents/", 1)[1]
            return httpx.Response(200, content=(SNAPSHOT / rel).read_bytes())
        if req.url.host == "hadeethenc.test":
            if hadeethenc != 200:
                return httpx.Response(hadeethenc)
            if path.endswith("/hadeeths/list/"):
                return httpx.Response(200, json={"data": [{"id": "2962"}] if req.url.params["category_id"] == "1" else []})
            if path.endswith("/hadeeths/one/"):
                return httpx.Response(200, content=(FIXTURES / "hadith" / "hadeethenc" / f"{req.url.params['id']}.json").read_bytes())
        if req.url.host == "fawaz.test":
            name = path.rsplit("ara-", 1)[1].removesuffix(".min.json")
            return httpx.Response(200, json=_fawaz(name))
        return httpx.Response(404, json={"url": url})

    return handler, seen


def test_refresh_sources_without_repo_token(make_app: Callable[..., App], snap_dir: Path) -> None:
    handler, seen = make_handler()
    app = make_app(handler, snapshot_dir=snap_dir)
    _ = app.sources
    out = refresh_sources(app)
    assert out["snapshot"] == "bundled (no SAMAWI_REPO_TOKEN)"
    assert out["hadith"]["fetched"] == 1 and out["hadith"]["api_down"] is False and out["hadith"]["stale_days"] is None
    assert not any(r.url.host == "api.github.com" for r in seen)
    assert (snap_dir.parent / "hadith" / "hadeethenc" / "2962.json").is_file()
    assert {p.stem for p in (snap_dir.parent / "hadith" / "fawaz").glob("*.json")} == {"abudawud", "tirmidhi", "nasai", "ibnmajah"}
    assert app._sources is None  # reloaded on next use
    assert app.mailer.sent == []  # type: ignore[attr-defined]


def test_refresh_sources_with_repo_token_refreshes_snapshot(make_app: Callable[..., App], snap_dir: Path) -> None:
    handler, seen = make_handler(sha="feedfacecafebeef0011")
    app = make_app(handler, snapshot_dir=snap_dir, samawi_repo_token="ghp-test")
    (snap_dir / MANIFEST).write_text(json.dumps({"ref": "old", "files": {}}), "utf-8")
    out = refresh_sources(app)
    assert out["snapshot"] == "feedfacecafe"
    manifest = json.loads((snap_dir / MANIFEST).read_text("utf-8"))
    assert manifest["ref"] == "feedfacecafebeef0011" and len(manifest["files"]) == 11
    gh = [r for r in seen if r.url.host == "api.github.com"]
    assert gh and all(r.headers["Authorization"] == "Bearer ghp-test" for r in gh)
    assert all(r.url.params.get("ref") == "feedfacecafebeef0011" for r in gh if "/contents/" in r.url.path)
    assert app._sources is None


def test_refresh_sources_alerts_on_stale_hadith(make_app: Callable[..., App], snap_dir: Path) -> None:
    handler, _ = make_handler(hadeethenc=503)
    cache = snap_dir.parent / "hadith" / "hadeethenc"
    cache.mkdir(parents=True)
    row = json.loads((FIXTURES / "hadith" / "hadeethenc" / "2962.json").read_text("utf-8"))
    row["_fetched_at"] = (NOW - timedelta(days=40)).isoformat()
    (cache / "2962.json").write_text(json.dumps(row), "utf-8")
    app = make_app(handler, snapshot_dir=snap_dir)
    out = refresh_sources(app)
    assert out["hadith"]["api_down"] is True and out["hadith"]["stale_days"] == 40
    (subject, body), *_ = app.mailer.sent  # type: ignore[attr-defined]
    assert subject == "WARN hadith snapshot stale" and "40 days" in body


def test_refresh_sources_api_down_but_cache_fresh_does_not_alert(make_app: Callable[..., App], snap_dir: Path) -> None:
    handler, _ = make_handler(hadeethenc=503)
    cache = snap_dir.parent / "hadith" / "hadeethenc"
    cache.mkdir(parents=True)
    row = json.loads((FIXTURES / "hadith" / "hadeethenc" / "2962.json").read_text("utf-8"))
    row["_fetched_at"] = (NOW - timedelta(days=3)).isoformat()
    (cache / "2962.json").write_text(json.dumps(row), "utf-8")
    app = make_app(handler, snapshot_dir=snap_dir)
    assert refresh_sources(app)["hadith"]["stale_days"] is None
    assert app.mailer.sent == []  # type: ignore[attr-defined]


# ── run_plan ──────────────────────────────────────────────────────────────────────────────────


def _bank(app: App, n: int, retired: int = 0) -> None:
    with session_scope(app.db) as s:
        for i in range(n + retired):
            s.add(EvergreenPost(json={}, rendered_media_keys=[], pillar="fakta_quran", topic_hash=f"h{i}", hook_index=0, created_at=T0, retired=i >= n))


def test_run_plan_tops_up_evergreen_below_reserve(make_app: Callable[..., App], snap_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = make_handler()
    app = make_app(handler, snapshot_dir=snap_dir)
    _bank(app, 10, retired=5)
    asked: list[int] = []

    def fake_top_up(app: App, count: int, pillars: list[str] | None = None) -> dict[str, int]:
        asked.append(count)
        return {"added": count, "failed": 0}

    monkeypatch.setattr(evergreen_mod, "top_up", fake_top_up)
    summary = run_plan(app)
    tomorrow = NOW.astimezone(WIB).date() + timedelta(days=1)
    assert tomorrow == TODAY + timedelta(days=1)
    assert "sources" in summary and "sources_error" not in summary
    assert summary["planned_feed"][0].startswith(f"{tomorrow} ")
    assert len(summary["planned_feed"]) == 7
    assert summary["evergreen_reserve"] == 10
    assert asked == [EVERGREEN_RESERVE - 10]
    assert summary["evergreen_added"] == {"added": 4, "failed": 0}


def test_run_plan_full_reserve_does_not_top_up(make_app: Callable[..., App], snap_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = make_handler()
    app = make_app(handler, snapshot_dir=snap_dir)
    _bank(app, EVERGREEN_RESERVE)
    monkeypatch.setattr(evergreen_mod, "top_up", lambda *a, **k: pytest.fail("no top-up expected"))
    summary = run_plan(app)
    assert summary["evergreen_reserve"] == EVERGREEN_RESERVE == evergreen_reserve(app)
    assert "evergreen_added" not in summary


def test_run_plan_continues_when_source_refresh_fails(make_app: Callable[..., App], snap_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    handler, _ = make_handler(github=500)
    app = make_app(handler, snapshot_dir=snap_dir, samawi_repo_token="ghp-test")
    _bank(app, EVERGREEN_RESERVE)
    summary = run_plan(app)
    assert "500" in summary["sources_error"]
    assert app.mailer.sent[0][0] == "WARN source refresh failed"  # type: ignore[attr-defined]
    assert len(summary["planned_feed"]) == 7
    with session_scope(app.db) as s:
        assert s.scalar(select(func.count()).select_from(ContentPlan)) == 7 * len(SLOTS) + 1


def test_evergreen_pillars_exclude_the_calendar() -> None:
    assert "kalender_hijriah" not in PILLARS_EVERGREEN and len(PILLARS_EVERGREEN) == 7


def test_launch_series_completes_across_partial_plans(make_app: Callable[..., App]) -> None:
    """produce can plan a single day; the series must continue at part 2, and the flag waits for part 3."""
    app = make_app(launch_mode="live", play_store_url="https://play.google.com/x")
    plan_days(app, TODAY, 1)
    with session_scope(app.db) as s:
        assert s.get(Flag, LAUNCH_FLAG) is None
    plan_days(app, TODAY + timedelta(days=1), 7)
    parts = [(p.series_id, p.series_part) for p in _feeds(app) if p.series_id == "launch"]
    assert parts == [("launch", 1), ("launch", 2), ("launch", 3)]
    with session_scope(app.db) as s:
        assert s.get(Flag, LAUNCH_FLAG) is not None
