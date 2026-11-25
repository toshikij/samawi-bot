"""`publish_plan` / `publish_due` end to end against the fake Meta server (spec §8.1, §10.3).

Order of checks: kill switch → late → payload → idempotency → quota → kill switch again → Meta.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from conftest import NOW, TODAY
from publish_fakes import IG_USER, TH_USER, Container, FakeMeta, Req, error
from samawi_bot.app import App
from samawi_bot.db import session_scope
from samawi_bot.db.models import ContentPlan, DraftRow, Post
from samawi_bot.jobs import publish as publish_mod
from samawi_bot.jobs.publish import LATE_LIMIT, publish_due, publish_plan
from samawi_bot.jobs.runner import JobSkipped, run_job
from samawi_bot.kill_switch import set_switch
from samawi_bot.timeutil import utc

DUE = NOW - timedelta(minutes=5)
MEDIA = [f"posts/{TODAY}/siang_feed/{i:02d}.png" for i in range(1, 7)]
CHAIN = ["Hook: ternyata...", "Satu ide.", "Ide kedua.", "Ide ketiga.", "Follow @samawi.indonesia"]


def carousel_payload(**kw: Any) -> dict[str, Any]:
    return {"kind": "carousel", "media": MEDIA, "cover": MEDIA[0], "caption": "Caption feed #samawi", "draft_id": 1, "hook": "Hook feed", **kw}


def reel_payload(cover: str | None = f"posts/{TODAY}/siang_feed/cover.png") -> dict[str, Any]:
    return {"kind": "reel", "media": [f"posts/{TODAY}/siang_feed/reel.mp4"], "cover": cover, "caption": "Reel caption", "hook": "Hook reel"}


def story_payload(slot: str = "subuh_story") -> dict[str, Any]:
    return {"kind": "story", "media": [f"posts/{TODAY}/{slot}.png"]}


def add_plan(app: App, slot: str, *, day: date = TODAY, at: datetime | None = DUE, status: str = "ready", **kw: Any) -> int:
    with session_scope(app.db) as s:
        p = ContentPlan(date=day, slot=slot, scheduled_at=at, status=status, sky_moment="dzuhur", **kw)
        s.add(p)
        s.flush()
        return p.id


def add_payload(app: App, plan_id: int, data: dict[str, Any]) -> None:
    with session_scope(app.db) as s:
        s.add(DraftRow(plan_id=plan_id, json=data, check_report={}, attempt=0, status="payload", created_at=NOW))


def add_passed_draft(app: App, plan_id: int) -> None:
    with session_scope(app.db) as s:
        s.add(
            DraftRow(
                plan_id=plan_id, json={"key_fact": "Surah terpendek: Al-Kautsar"}, judge_report={"novelty_pct": 72}, attempt=1, status="passed", created_at=NOW
            )
        )


def posts(app: App) -> list[Post]:
    with session_scope(app.db) as s:
        rows = list(s.scalars(select(Post).order_by(Post.id)))
        for r in rows:
            s.expunge(r)
        return rows


def plan_status(app: App, plan_id: int) -> str:
    with session_scope(app.db) as s:
        p = s.get(ContentPlan, plan_id)
        assert p is not None
        return p.status


def kill(app: App, on: bool = True) -> None:
    with session_scope(app.db) as s:
        set_switch(s, on, "owner@example.com", NOW)


def alerts(app: App) -> list[str]:
    return [subject for subject, _ in app.mailer.sent]  # type: ignore[attr-defined]


@pytest.fixture
def meta() -> FakeMeta:
    return FakeMeta()


@pytest.fixture
def app(make_app: Callable[..., App], meta: FakeMeta) -> App:
    return make_app(handler=meta)


def media_posts(meta: FakeMeta) -> list[Req]:
    return meta.calls("POST", f"/{IG_USER}/media")


def thread_posts(meta: FakeMeta) -> list[Req]:
    return meta.calls("POST", f"/{TH_USER}/threads")


# ---- preconditions ----------------------------------------------------------------------------


def test_unknown_plan(app: App) -> None:
    with pytest.raises(ValueError, match="No plan 999"):
        publish_plan(app, 999)


def test_payload_missing_alerts_and_fails(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed")
    assert publish_plan(app, pid) == {"plan": pid, "status": "no payload"}
    assert alerts(app) == [f"MISSING {TODAY} siang_feed"]
    assert plan_status(app, pid) == "failed"
    assert meta.requests == [] and posts(app) == []


def test_slot_more_than_3h_late_is_skipped(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed", at=NOW - LATE_LIMIT - timedelta(minutes=1))
    add_payload(app, pid, carousel_payload())
    assert publish_plan(app, pid) == {"plan": pid, "status": "skipped: too late"}
    assert plan_status(app, pid) == "skipped"
    assert meta.requests == [] and posts(app) == [] and alerts(app) == []


def test_slot_exactly_3h_late_still_publishes(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "subuh_story", at=NOW - LATE_LIMIT)
    add_payload(app, pid, story_payload())
    assert publish_plan(app, pid)["status"] == "published"


def test_unscheduled_plan_is_not_late(app: App) -> None:
    pid = add_plan(app, "subuh_story", at=None)
    add_payload(app, pid, story_payload())
    assert publish_plan(app, pid)["status"] == "published"


def test_dry_run_records_and_never_calls_meta(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta, publish_live=False)
    pid = add_plan(app, "siang_feed", pillar="fakta_quran", hook_formula="ternyata", format="carousel")
    add_payload(app, pid, carousel_payload())
    assert publish_plan(app, pid) == {"plan": pid, "status": "dry_run", "kind": "carousel"}
    (post,) = posts(app)
    assert post.status == "dry_run" and post.platform == "instagram" and post.media_type == "carousel"
    assert post.hook_text == "Hook feed" and post.pillar == "fakta_quran" and post.format == "carousel"
    assert post.platform_media_id is None
    assert plan_status(app, pid) == "published"
    assert meta.requests == []
    assert publish_plan(app, pid)["status"] == "already published"
    assert len(posts(app)) == 1


# ---- live publishes ---------------------------------------------------------------------------


def test_live_carousel(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed", pillar="fakta_quran", hook_formula="ternyata", bridge_feature="jumatMode", format="carousel")
    add_payload(app, pid, carousel_payload())
    add_passed_draft(app, pid)
    res = publish_plan(app, pid)
    assert res["status"] == "published"
    (pub,) = meta.ig_published
    assert res["id"] == pub.media_id and res["permalink"] == f"https://www.instagram.com/p/{pub.media_id}/"
    assert pub.params["media_type"] == "CAROUSEL" and len(pub.params["children"].split(",")) == 6
    assert pub.params["caption"] == "Caption feed #samawi"
    first = meta.requests[0]
    assert first.path == f"/{IG_USER}/content_publishing_limit"  # quota before any container
    assert [r.params["image_url"] for r in media_posts(meta)[:6]] == [app.storage.url(k) for k in MEDIA]
    (post,) = posts(app)
    assert post.idempotency_key == f"{TODAY}:siang_feed:instagram"
    assert post.status == "published" and post.platform_media_id == pub.media_id and post.permalink == res["permalink"]
    assert post.utm_campaign == "20261126-siang_feed" and post.slot == "siang_feed" and post.sky_moment == "dzuhur"
    assert (post.pillar, post.hook_formula, post.bridge_feature, post.format) == ("fakta_quran", "ternyata", "jumatMode", "carousel")
    assert post.key_fact == "Surah terpendek: Al-Kautsar" and post.novelty_pct == 72 and post.hook_text == "Hook feed"
    assert post.published_at is not None and post.error is None
    assert plan_status(app, pid) == "published"
    assert alerts(app) == []


def test_live_reel_with_cover(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, reel_payload())
    assert publish_plan(app, pid)["status"] == "published"
    (req,) = media_posts(meta)
    assert req.params["media_type"] == "REELS"
    assert req.params["video_url"] == app.storage.url(f"posts/{TODAY}/siang_feed/reel.mp4")
    assert req.params["cover_url"] == app.storage.url(f"posts/{TODAY}/siang_feed/cover.png")


def test_live_reel_without_cover(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, reel_payload(cover=None))
    assert publish_plan(app, pid)["status"] == "published"
    assert "cover_url" not in media_posts(meta)[0].params


def test_live_story(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "kahfi_story")
    add_payload(app, pid, story_payload("kahfi_story"))
    res = publish_plan(app, pid)
    assert res["status"] == "published"
    (req,) = media_posts(meta)
    assert req.params["media_type"] == "STORIES" and "caption" not in req.params
    (post,) = posts(app)
    assert post.media_type == "story" and post.pillar is None  # no feed plan that day


def test_live_threads_text(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "subuh_threads")
    add_payload(app, pid, {"kind": "text", "posts": ["Ayat pagi ini: QS 94:5"]})
    res = publish_plan(app, pid)
    assert res["status"] == "published"
    (req,) = thread_posts(meta)
    assert req.params["media_type"] == "TEXT" and "reply_to_id" not in req.params
    assert res["progress"] == [res["id"]]
    assert res["permalink"].endswith(f"/{res['id']}/")
    (post,) = posts(app)
    assert post.idempotency_key == f"{TODAY}:subuh_threads:threads" and post.progress == [res["id"]]


def test_live_chain_with_carousel_images(app: App, meta: FakeMeta) -> None:
    feed = add_plan(app, "siang_feed", pillar="sains")
    add_passed_draft(app, feed)
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN, "images": MEDIA})
    res = publish_plan(app, pid)
    assert res["status"] == "published"
    ids = res["progress"]
    assert len(ids) == len(CHAIN) and res["id"] == ids[0]
    creates = thread_posts(meta)
    assert len(creates) == len(MEDIA) + 1 + len(CHAIN) - 1
    parent = creates[len(MEDIA)]
    assert parent.params["media_type"] == "CAROUSEL" and parent.params["text"] == CHAIN[0] and "reply_to_id" not in parent.params
    replies = creates[len(MEDIA) + 1 :]
    assert [r.params["reply_to_id"] for r in replies] == ids[:-1]
    assert [r.params["text"] for r in replies] == CHAIN[1:]
    (post,) = posts(app)
    assert post.progress == ids and post.platform_media_id == ids[0] and post.pillar == "sains" and post.key_fact


def test_live_chain_single_image_first(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN[:2], "images": [f"posts/{TODAY}/siang_feed/cover.png"]})
    publish_plan(app, pid)
    creates = thread_posts(meta)
    assert creates[0].params["media_type"] == "IMAGE" and creates[0].params["text"] == CHAIN[0]
    assert creates[1].params["media_type"] == "TEXT"


def test_chain_images_capped_at_20(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN[:1], "images": [f"i/{n}.png" for n in range(25)]})
    assert publish_plan(app, pid)["status"] == "published"
    assert len(meta.threads_published[0].params["children"].split(",")) == 20


def test_unknown_instagram_kind_fails(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "malam_story")
    add_payload(app, pid, {"kind": "text", "posts": ["x"]})
    res = publish_plan(app, pid)
    assert res["status"] == "failed" and "Unknown Instagram payload text" in res["error"]
    assert "retry" not in res
    assert media_posts(meta) == []
    assert alerts(app) == [f"PUBLISH FAILED {TODAY} malam_story"]


def test_expired_token_fails_with_alert(app: App, meta: FakeMeta, clock: Any) -> None:
    pid = add_plan(app, "subuh_story", at=None)
    add_payload(app, pid, story_payload())
    from samawi_bot.jobs.tokens import access_token

    access_token(app, "instagram")
    clock.now = NOW + timedelta(days=61)
    res = publish_plan(app, pid)
    assert res["status"] == "failed" and "token expired" in res["error"]
    assert meta.requests == []


# ---- idempotency ------------------------------------------------------------------------------


def test_duplicate_publish_protection(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    assert publish_plan(app, pid)["status"] == "published"
    n = len(meta.requests)
    assert publish_plan(app, pid) == {"plan": pid, "status": "already published"}
    assert len(meta.requests) == n  # the second call never reached Meta
    assert len(meta.ig_published) == 1
    assert len(posts(app)) == 1


def test_concurrent_insert_loses_on_unique_key(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two runners racing on a new slot: the loser's existence check misses, its INSERT hits the unique key."""
    pid = add_plan(app, "subuh_story")
    add_payload(app, pid, story_payload())
    with session_scope(app.db) as s:
        s.add(Post(idempotency_key=f"{TODAY}:subuh_story:instagram", platform="instagram", media_type="story", status="failed", progress=[]))
    real = Session.execute
    missed = {"n": 0}

    class _Empty:
        def first(self) -> None:
            return None

    def execute(self: Session, statement: Any, *a: Any, **k: Any) -> Any:
        if isinstance(statement, Select) and "FROM posts" in str(statement) and not missed["n"]:
            missed["n"] += 1
            return _Empty()
        return real(self, statement, *a, **k)

    monkeypatch.setattr(Session, "execute", execute)
    assert publish_plan(app, pid) == {"plan": pid, "status": "already published"}
    assert missed["n"] == 1
    assert meta.requests == []
    assert len(posts(app)) == 1


def test_second_runner_while_first_in_flight_does_nothing(app: App, meta: FakeMeta) -> None:
    """The one-off schedule and the hourly sweep firing together: only one publishes."""
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN})
    second: list[dict[str, Any]] = []

    def on_publish(media_id: str, c: Container) -> None:
        if len(meta.threads_published) == 2 and not second:
            second.append(publish_plan(app, pid))  # runs while the first holds the lease

    meta.on_publish = on_publish
    assert publish_plan(app, pid)["status"] == "published"
    assert second == [{"plan": pid, "status": "already published"}]
    assert [c.params["text"] for c in meta.threads_published] == CHAIN


def _claim_now(app: App, pid: int) -> publish_mod.Claim | None:
    with session_scope(app.db) as s:
        plan = s.get(ContentPlan, pid)
        assert plan is not None
        s.expunge(plan)
    return publish_mod._claim(app, plan, "instagram", "story", None)


def test_two_concurrent_claims_only_one_proceeds(app: App) -> None:
    pid = add_plan(app, "subuh_story")
    first = _claim_now(app, pid)
    assert first is not None and first.previous_error is None
    assert _claim_now(app, pid) is None
    (post,) = posts(app)
    assert post.status == "pending" and post.claimed_at is not None and utc(post.claimed_at) == NOW


def _existing(app: App, status: str, claimed_ago: timedelta | None, error: str | None = None) -> int:
    pid = add_plan(app, "subuh_story")
    with session_scope(app.db) as s:
        s.add(
            Post(
                idempotency_key=f"{TODAY}:subuh_story:instagram",
                plan_id=pid,
                platform="instagram",
                media_type="story",
                status=status,
                error=error,
                progress=[],
                claimed_at=None if claimed_ago is None else NOW - claimed_ago,
            )
        )
    return pid


@pytest.mark.parametrize("ago", [timedelta(minutes=20, seconds=1), timedelta(hours=2)])
def test_stale_lease_can_be_reclaimed(app: App, ago: timedelta) -> None:
    pid = _existing(app, "pending", ago, error="kill switch")
    claim = _claim_now(app, pid)
    assert claim is not None and claim.previous_error == "kill switch"
    assert claim.post.status == "pending" and claim.post.error is None and utc(claim.post.claimed_at or NOW) == NOW
    (post,) = posts(app)
    assert utc(post.claimed_at or NOW - ago) == NOW and post.error is None


@pytest.mark.parametrize("ago", [timedelta(0), timedelta(minutes=19), timedelta(minutes=20)])
def test_fresh_lease_blocks_claim(app: App, ago: timedelta) -> None:
    pid = _existing(app, "pending", ago)
    assert _claim_now(app, pid) is None


@pytest.mark.parametrize("status", ["published", "dry_run"])
@pytest.mark.parametrize("ago", [None, timedelta(hours=5)])
def test_published_row_never_reclaimed(app: App, status: str, ago: timedelta | None) -> None:
    pid = _existing(app, status, ago)
    assert _claim_now(app, pid) is None
    assert posts(app)[0].status == status


def test_failed_row_without_lease_is_reclaimed(app: App) -> None:
    pid = _existing(app, "failed", None, error="boom")
    claim = _claim_now(app, pid)
    assert claim is not None and claim.previous_error == "boom"


def test_dead_run_resume_waits_for_lease(app: App, meta: FakeMeta, clock: Any) -> None:
    pid = add_plan(app, "subuh_story")
    add_payload(app, pid, story_payload())

    def on_request(req: Req) -> None:
        if req.path.endswith("/media"):
            raise Crash

    meta.on_request = on_request
    with pytest.raises(Crash):
        publish_plan(app, pid)
    meta.on_request = None
    assert publish_plan(app, pid)["status"] == "already published"  # lease still held by the dead run
    clock.now = NOW + publish_mod.LEASE + timedelta(seconds=1)
    assert publish_plan(app, pid)["status"] == "published"
    assert len(meta.ig_published) == 1


def test_failed_post_is_retried_on_next_run(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "subuh_story")
    add_payload(app, pid, story_payload())
    meta.fail("POST", f"/{IG_USER}/media", error(400, 100, "Only photo or video can be accepted as media type."))
    assert publish_plan(app, pid)["status"] == "failed"
    (post,) = posts(app)
    assert post.status == "failed" and "Only photo or video" in (post.error or "")
    assert post.claimed_at is None
    assert publish_plan(app, pid)["status"] == "published"
    (post,) = posts(app)
    assert post.status == "published" and post.error is None
    assert len(meta.ig_published) == 1


# ---- kill switch ------------------------------------------------------------------------------


def test_kill_switch_env_skips_before_any_meta_call(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta, kill_switch=True)
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    with pytest.raises(JobSkipped, match="Kill switch is ON"):
        publish_plan(app, pid)
    assert meta.requests == [] and posts(app) == [] and alerts(app) == []
    assert plan_status(app, pid) == "ready"


def test_kill_switch_flag_skips_and_run_job_records_skipped(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    kill(app)
    run = run_job(app, "publish", lambda a: publish_plan(a, pid))
    assert run.status == "skipped" and "Kill switch is ON" in run.summary["reason"]
    assert meta.requests == [] and posts(app) == [] and alerts(app) == []


def test_kill_switch_stops_the_due_sweep(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    kill(app)
    with pytest.raises(JobSkipped):
        publish_due(app)
    assert meta.requests == []


def test_kill_switch_flipped_after_quota_check_stops_instagram(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())

    def on_request(req: Req) -> None:
        if req.path.endswith("/content_publishing_limit"):
            kill(app)

    meta.on_request = on_request
    with pytest.raises(JobSkipped, match="mid-publish"):
        publish_plan(app, pid)
    assert media_posts(meta) == []
    (post,) = posts(app)
    assert post.status == "pending" and post.error == "kill switch" and post.claimed_at is None
    assert plan_status(app, pid) == "ready"
    assert alerts(app) == []


def test_kill_switch_flipped_mid_chain_then_resume(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN})

    def on_publish(media_id: str, c: Container) -> None:
        if len(meta.threads_published) == 2:
            kill(app)

    meta.on_publish = on_publish
    with pytest.raises(JobSkipped):
        publish_plan(app, pid)
    (post,) = posts(app)
    first_two = [c.media_id for c in meta.threads_published]
    assert post.status == "pending" and post.error == "kill switch" and post.progress == first_two
    assert post.claimed_at is None  # released: the resume below needs no lease wait
    assert plan_status(app, pid) == "ready"
    assert len(thread_posts(meta)) == 2

    # Still on: a retry stops before anything leaves.
    with pytest.raises(JobSkipped):
        publish_plan(app, pid)
    assert len(meta.threads_published) == 2

    meta.on_publish = None
    kill(app, on=False)
    res = publish_plan(app, pid)
    assert res["status"] == "published"
    ids = [c.media_id for c in meta.threads_published]
    assert len(ids) == len(CHAIN) == len(set(ids))
    assert res["progress"] == ids and res["id"] == first_two[0]
    texts = [r.params["text"] for r in thread_posts(meta)]
    assert texts == CHAIN  # each post exactly once
    assert [r.params.get("reply_to_id") for r in thread_posts(meta)] == [None, *ids[:-1]]
    (post,) = posts(app)
    assert post.status == "published" and post.progress == ids and post.error is None


class Crash(BaseException):
    """The process dying (OOM, task stopped): nothing gets to catch it."""


def test_resume_after_crash_mid_chain(app: App, meta: FakeMeta, clock: Any) -> None:
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN})

    def on_request(req: Req) -> None:
        if req.method == "POST" and req.path == f"/{TH_USER}/threads" and len(meta.threads_published) == 3:
            raise Crash

    meta.on_request = on_request
    with pytest.raises(Crash):
        publish_plan(app, pid)
    (post,) = posts(app)
    done = [c.media_id for c in meta.threads_published]
    assert post.status == "pending" and post.progress == done and len(done) == 3
    assert post.claimed_at is not None  # the dead run never released its lease

    meta.on_request = None
    clock.now = NOW + publish_mod.LEASE + timedelta(minutes=1)
    res = publish_plan(app, pid)
    assert res["status"] == "published"
    published = meta.threads_published
    assert [c.params["text"] for c in published] == CHAIN  # nothing re-posted
    assert published[3].params["reply_to_id"] == done[-1]


def test_meta_error_mid_chain_keeps_progress_and_resumes(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN})
    meta.fail("POST", f"/{TH_USER}/threads", error(400, 100, "Param text is too long"), when=lambda r: r.params.get("text") == CHAIN[2])
    res = publish_plan(app, pid)
    assert res["status"] == "failed" and "too long" in res["error"] and "retry" not in res  # no evergreen for Threads
    (post,) = posts(app)
    assert post.status == "failed" and len(post.progress) == 2
    assert plan_status(app, pid) == "failed"
    assert alerts(app) == [f"PUBLISH FAILED {TODAY} siang_threads"]
    assert publish_plan(app, pid)["status"] == "published"
    assert len(meta.threads_published) == len(CHAIN)


# ---- quota ------------------------------------------------------------------------------------


def test_quota_80_holds_story_but_publishes_feed(app: App, meta: FakeMeta) -> None:
    meta.ig_quota = {"quota_usage": 40, "config": {"quota_total": 50}}
    story = add_plan(app, "malam_story")
    add_payload(app, story, story_payload("malam_story"))
    feed = add_plan(app, "siang_feed")
    add_payload(app, feed, carousel_payload())
    res = publish_plan(app, story)
    assert res == {"plan": story, "status": "held", "error": "Instagram quota 40/50"}
    assert media_posts(meta) == []
    assert plan_status(app, story) == "ready"  # not failed: the sweep retries it
    (post,) = posts(app)
    assert post.status == "pending" and post.error == "quota held" and post.claimed_at is None
    assert alerts(app) == [f"QUOTA ≥80% {TODAY} malam_story"]
    assert publish_plan(app, feed)["status"] == "published"
    assert len(meta.ig_published) == 1


def test_quota_hold_alerts_once_then_sweep_publishes(app: App, meta: FakeMeta, clock: Any) -> None:
    meta.ig_quota = {"quota_usage": 45, "config": {"quota_total": 50}}
    pid = add_plan(app, "subuh_story", at=NOW - timedelta(minutes=10))
    add_payload(app, pid, story_payload())
    assert publish_due(app)["published"][0]["status"] == "held"
    clock.now = NOW + timedelta(hours=1)
    assert publish_due(app)["published"][0]["status"] == "held"  # held again, no second alert
    assert alerts(app) == [f"QUOTA ≥80% {TODAY} subuh_story"]
    meta.ig_quota = {"quota_usage": 10, "config": {"quota_total": 50}}
    clock.now = NOW + timedelta(hours=2)
    (res,) = publish_due(app)["published"]
    assert res["status"] == "published"
    (post,) = posts(app)
    assert post.status == "published" and post.error is None
    assert plan_status(app, pid) == "published"
    assert publish_due(app) == {"published": []}


def test_quota_held_past_late_limit_is_skipped(app: App, meta: FakeMeta, clock: Any) -> None:
    meta.ig_quota = {"quota_usage": 50, "config": {"quota_total": 50}}
    pid = add_plan(app, "subuh_story", at=NOW - timedelta(minutes=10))
    add_payload(app, pid, story_payload())
    assert publish_plan(app, pid)["status"] == "held"
    clock.now = NOW + LATE_LIMIT
    assert publish_plan(app, pid)["status"] == "skipped: too late"
    assert plan_status(app, pid) == "skipped" and meta.ig_published == []


def test_quota_79_publishes_story(app: App, meta: FakeMeta) -> None:
    meta.ig_quota = {"quota_usage": 79, "config": {"quota_total": 100}}
    pid = add_plan(app, "subuh_story")
    add_payload(app, pid, story_payload())
    assert publish_plan(app, pid)["status"] == "published"


def test_quota_100_holds_feed_without_evergreen(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    meta.ig_quota = {"quota_usage": 50, "config": {"quota_total": 50}}
    calls = _patch_evergreen(monkeypatch)
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    res = publish_plan(app, pid)
    assert res["status"] == "held" and "retry" not in res
    assert calls == []
    assert media_posts(meta) == []
    assert alerts(app) == [f"QUOTA ≥80% {TODAY} siang_feed"]
    assert plan_status(app, pid) == "ready"


def test_hold_after_other_error_alerts(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "subuh_story")
    add_payload(app, pid, story_payload())
    meta.fail("POST", f"/{IG_USER}/media", error(400, 100, "bad"))
    assert publish_plan(app, pid)["status"] == "failed"
    meta.ig_quota = {"quota_usage": 49, "config": {"quota_total": 50}}
    assert publish_plan(app, pid)["status"] == "held"
    assert alerts(app) == [f"PUBLISH FAILED {TODAY} subuh_story", f"QUOTA ≥80% {TODAY} subuh_story"]


def test_threads_reply_quota_too_small_for_chain(app: App, meta: FakeMeta) -> None:
    meta.th_quota = {"quota_usage": 0, "config": {"quota_total": 250}, "reply_quota_usage": 997, "reply_config": {"quota_total": 1000}}
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN})
    res = publish_plan(app, pid)
    assert res["status"] == "held" and "997/1000 replies" in res["error"]
    assert thread_posts(meta) == []


@pytest.mark.parametrize("slot", ["subuh_threads", "siang_threads"])
def test_lone_threads_post_ignores_full_reply_quota(app: App, meta: FakeMeta, slot: str) -> None:
    meta.th_quota = {"quota_usage": 0, "config": {"quota_total": 250}, "reply_quota_usage": 1000, "reply_config": {"quota_total": 1000}}
    pid = add_plan(app, slot)
    add_payload(app, pid, {"kind": "text", "posts": ["Satu post saja."]})
    assert publish_plan(app, pid)["status"] == "published"
    assert alerts(app) == []


def test_resumed_chain_needs_only_reply_quota(app: App, meta: FakeMeta) -> None:
    """After two posts went out, the rest are replies: a full *posts* quota does not hold them."""
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN})
    meta.on_publish = lambda mid, c: kill(app) if len(meta.threads_published) == 2 else None
    with pytest.raises(JobSkipped):
        publish_plan(app, pid)
    meta.on_publish = None
    kill(app, on=False)
    meta.th_quota = {"quota_usage": 250, "config": {"quota_total": 250}, "reply_quota_usage": 997, "reply_config": {"quota_total": 1000}}
    assert publish_plan(app, pid)["status"] == "published"  # exactly 3 replies left, 3 fit
    assert len(meta.threads_published) == len(CHAIN)


def test_resumed_chain_reply_quota_counts_remaining_only(app: App, meta: FakeMeta) -> None:
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN})
    meta.on_publish = lambda mid, c: kill(app) if len(meta.threads_published) == 2 else None
    with pytest.raises(JobSkipped):
        publish_plan(app, pid)
    meta.on_publish = None
    kill(app, on=False)
    meta.th_quota = {"quota_usage": 0, "config": {"quota_total": 250}, "reply_quota_usage": 998, "reply_config": {"quota_total": 1000}}
    assert publish_plan(app, pid)["status"] == "held"  # 3 replies needed, 2 left


def test_fully_published_chain_row_completes_without_quota(app: App, meta: FakeMeta) -> None:
    """A run that died after the last post but before marking the row: the resume just records it."""
    pid = add_plan(app, "siang_threads")
    add_payload(app, pid, {"kind": "chain", "posts": CHAIN[:2]})
    with session_scope(app.db) as s:
        s.add(
            Post(
                idempotency_key=f"{TODAY}:siang_threads:threads",
                plan_id=pid,
                platform="threads",
                media_type="chain",
                status="pending",
                progress=["th_a", "th_b"],
            )
        )
    meta.th_quota = {"quota_usage": 250, "config": {"quota_total": 250}, "reply_quota_usage": 1000, "reply_config": {"quota_total": 1000}}
    res = publish_plan(app, pid)
    assert res["status"] == "published" and res["progress"] == ["th_a", "th_b"] and res["permalink"] is None
    assert thread_posts(meta) == []


def test_threads_posts_quota_80_holds_non_essential(app: App, meta: FakeMeta) -> None:
    meta.th_quota = {"quota_usage": 200, "config": {"quota_total": 250}}
    text = add_plan(app, "malam_threads")
    add_payload(app, text, {"kind": "text", "posts": ["Pertanyaan malam ini?"]})
    chain = add_plan(app, "siang_threads")
    add_payload(app, chain, {"kind": "chain", "posts": CHAIN[:2]})
    assert publish_plan(app, text)["status"] == "held"
    assert publish_plan(app, chain)["status"] == "published"
    assert len(meta.threads_published) == 2


# ---- feed failure → evergreen -----------------------------------------------------------------


def _patch_evergreen(monkeypatch: pytest.MonkeyPatch, fail: Exception | None = None) -> list[int]:
    calls: list[int] = []

    def fake(app: App, plan: ContentPlan) -> tuple[Any, int, dict[str, Any], bool]:
        calls.append(plan.id)
        if fail:
            raise fail
        media = [f"posts/{plan.date}/{plan.slot}-evergreen/{i:02d}.png" for i in range(1, 4)]
        return None, 0, carousel_payload(media=media, cover=media[0], evergreen_id=7, hook="Evergreen hook", caption="Evergreen caption"), True

    monkeypatch.setattr("samawi_bot.jobs.produce._evergreen_feed", fake)
    return calls


def test_feed_failure_falls_back_to_evergreen(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_evergreen(monkeypatch)
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    meta.fail("POST", f"/{IG_USER}/media_publish", error(400, 9004, "The media could not be fetched from this URI"))
    res = publish_plan(app, pid)
    assert res["status"] == "failed" and res["retry"] == "published" and "could not be fetched" in res["error"]
    assert calls == [pid]
    (pub,) = meta.ig_published
    assert pub.params["caption"] == "Evergreen caption"
    (post,) = posts(app)  # the failed row was replaced, one row per key
    assert post.status == "published" and post.hook_text == "Evergreen hook" and post.platform_media_id == pub.media_id
    assert plan_status(app, pid) == "published"
    assert alerts(app) == [f"PUBLISH FAILED {TODAY} siang_feed"]
    with session_scope(app.db) as s:
        payloads = s.scalars(select(DraftRow).where(DraftRow.plan_id == pid, DraftRow.status == "payload")).all()
        assert [p.json.get("evergreen_id") for p in payloads] == [None, 7]


def test_stuck_container_times_out_then_evergreen(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_evergreen(monkeypatch)
    slept: list[float] = []
    app.sleep = slept.append
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, reel_payload())
    meta.statuses_for_next(["IN_PROGRESS"])  # the reel container never finishes
    res = publish_plan(app, pid)
    assert "still IN_PROGRESS after 60 polls" in res["error"]
    assert res["retry"] == "published"
    assert slept.count(5.0) >= 60
    assert len(meta.ig_published) == 1 and meta.ig_published[0].params["media_type"] == "CAROUSEL"


@pytest.mark.parametrize("state", ["ERROR", "EXPIRED"])
def test_container_error_states_fall_back(state: str, app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_evergreen(monkeypatch)
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    meta.statuses_for_next(["FINISHED"], [state])
    res = publish_plan(app, pid)
    assert state in res["error"] and res["retry"] == "published"


def test_evergreen_also_fails_no_infinite_loop(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_evergreen(monkeypatch)
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    meta.fail("POST", f"/{IG_USER}/media_publish", error(400, 9004, "fetch failed"), times=99)
    res = publish_plan(app, pid)
    assert res["status"] == "failed" and res["retry"] == "failed"
    assert calls == [pid]
    assert len(meta.calls("POST", f"/{IG_USER}/media_publish")) == 2
    assert alerts(app) == [f"PUBLISH FAILED {TODAY} siang_feed"] * 2
    (post,) = posts(app)
    assert post.status == "failed"
    assert plan_status(app, pid) == "failed"


def test_evergreen_bank_error_is_alerted(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_evergreen(monkeypatch, fail=RuntimeError("evergreen bank empty"))
    pid = add_plan(app, "siang_feed")
    add_payload(app, pid, carousel_payload())
    meta.fail("POST", f"/{IG_USER}/media_publish", error(400, 9004, "fetch failed"))
    res = publish_plan(app, pid)
    assert res["retry"] == "evergreen failed: evergreen bank empty"
    assert alerts(app) == [f"PUBLISH FAILED {TODAY} siang_feed", f"EVERGREEN FALLBACK FAILED {TODAY}"]


def test_evergreen_payload_failure_does_not_retry_again(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_evergreen(monkeypatch)
    pid = add_plan(app, "siang_feed", status="fallback")
    add_payload(app, pid, carousel_payload(evergreen_id=3))
    meta.fail("POST", f"/{IG_USER}/media_publish", error(400, 9004, "fetch failed"))
    res = publish_plan(app, pid)
    assert res["status"] == "failed" and "retry" not in res and calls == []


def test_non_feed_failure_has_no_evergreen_retry(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _patch_evergreen(monkeypatch)
    pid = add_plan(app, "subuh_story")
    add_payload(app, pid, story_payload())
    meta.statuses_for_next(["ERROR"])
    res = publish_plan(app, pid)
    assert res["status"] == "failed" and "retry" not in res and calls == []


def test_transient_errors_retried_then_published(app: App, meta: FakeMeta) -> None:
    slept: list[float] = []
    app.sleep = slept.append
    pid = add_plan(app, "subuh_story")
    add_payload(app, pid, story_payload())
    meta.fail("POST", f"/{IG_USER}/media_publish", error(500, 2, "Service temporarily unavailable"), times=2)
    assert publish_plan(app, pid)["status"] == "published"
    assert slept[-2:] == [2.0, 4.0]


# ---- feed meta --------------------------------------------------------------------------------


def test_feed_meta_without_passed_draft(app: App) -> None:
    add_plan(app, "siang_feed", pillar="doa", format="reel")
    pid = add_plan(app, "malam_story")
    add_payload(app, pid, story_payload("malam_story"))
    with session_scope(app.db) as s:
        plan = s.get(ContentPlan, pid)
        assert plan is not None
        meta = publish_mod._feed_meta(app, plan)
    assert meta == {"pillar": "doa", "hook_formula": None, "bridge_feature": None, "format": "reel"}


def test_evergreen_retry_without_a_failed_row(app: App, meta: FakeMeta, monkeypatch: pytest.MonkeyPatch) -> None:
    """The fallback works even when there is no failed post row to clear (e.g. it was removed)."""
    _patch_evergreen(monkeypatch)
    pid = add_plan(app, "siang_feed")
    with session_scope(app.db) as s:
        plan = s.get(ContentPlan, pid)
        assert plan is not None
        s.expunge(plan)
    assert publish_mod._retry_with_evergreen(app, plan) == "published"
    assert len(meta.ig_published) == 1 and len(posts(app)) == 1


# ---- publish_due ------------------------------------------------------------------------------


def test_publish_due_picks_ready_and_fallback_inside_window(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta, publish_live=False)
    ready = add_plan(app, "subuh_story", at=NOW - timedelta(minutes=30))
    fallback = add_plan(app, "siang_feed", at=NOW, status="fallback")
    edge = add_plan(app, "subuh_threads", at=NOW - LATE_LIMIT)
    planned = add_plan(app, "siang_threads", at=NOW - timedelta(minutes=10), status="planned")
    future = add_plan(app, "malam_story", at=NOW + timedelta(minutes=1))
    too_late = add_plan(app, "malam_threads", at=NOW - LATE_LIMIT - timedelta(seconds=1))
    published = add_plan(app, "kahfi_story", at=NOW - timedelta(minutes=1), status="published")
    no_time = add_plan(app, "subuh_story", day=TODAY + timedelta(days=1), at=None)
    for pid, data in [
        (ready, story_payload()),
        (fallback, carousel_payload()),
        (edge, {"kind": "text", "posts": ["x"]}),
        (planned, {"kind": "chain", "posts": ["x"]}),
        (future, story_payload()),
        (too_late, {"kind": "text", "posts": ["x"]}),
        (published, story_payload()),
        (no_time, story_payload()),
    ]:
        add_payload(app, pid, data)
    res = publish_due(app)
    assert sorted(r["plan"] for r in res["published"]) == sorted([ready, fallback, edge])
    assert all(r["status"] == "dry_run" for r in res["published"])
    assert plan_status(app, future) == "ready" and plan_status(app, too_late) == "ready"
    assert meta.requests == []
    assert publish_due(app) == {"published": []}


def test_publish_due_live_sweep_is_idempotent(app: App, meta: FakeMeta) -> None:
    a = add_plan(app, "subuh_story", at=NOW - timedelta(minutes=30))
    add_payload(app, a, story_payload())
    b = add_plan(app, "subuh_threads", at=NOW - timedelta(minutes=28))
    add_payload(app, b, {"kind": "text", "posts": ["Refleksi pagi."]})
    assert [r["status"] for r in publish_due(app)["published"]] == ["published", "published"]
    assert publish_due(app) == {"published": []}
    assert len(meta.published) == 2
