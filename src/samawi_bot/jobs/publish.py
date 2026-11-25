"""`publish`: one slot (from its one-off schedule) or every due slot (the hourly sweep).

Order of checks before anything leaves: kill switch → payload exists → idempotency → quota. A
Threads chain checks the kill switch again before every post and resumes where it stopped.

Idempotency is a row per `date:slot:platform` plus a lease (`claimed_at`): a run claims the row
atomically, and a second run finds it in flight and does nothing. Any run that ends without
publishing releases the lease so the next run can resume at once; a run that died holding it
loses it after `LEASE`. Quota at ≥80% (or 100%) holds a post rather than failing it: the plan
stays `ready`, so the hourly sweep retries it while it is still inside `LATE_LIMIT`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.exc import IntegrityError

from ..alerts import safe_send
from ..app import App
from ..db import session_scope
from ..db.models import ContentPlan, DraftRow, Post
from ..kill_switch import KillSwitchOn, ensure_off
from ..publisher.meta import MetaApiError
from ..publisher.quota import QuotaStatus, allowed
from ..publisher.slots import slot_spec
from ..timeutil import utc
from .runner import JobSkipped
from .tokens import access_token

LATE_LIMIT = timedelta(hours=3)  # a slot more than 3 h late is skipped rather than posted out of context
LEASE = timedelta(minutes=20)  # a claim older than this belongs to a run that died; it may be taken over
QUOTA_HELD = "quota held"


class QuotaHeld(RuntimeError):
    """Quota does not allow this post right now. Not a failure: the sweep retries it later."""


@dataclass
class Claim:
    post: Post
    previous_error: str | None


def _payload(app: App, plan_id: int) -> dict[str, Any] | None:
    with session_scope(app.db) as s:
        row = s.scalars(select(DraftRow).where(DraftRow.plan_id == plan_id, DraftRow.status == "payload").order_by(DraftRow.id.desc())).first()
        return dict(row.json) if row else None


def _claim(app: App, plan: ContentPlan, platform: str, kind: str, pillar: str | None) -> Claim | None:
    """Take the idempotency row for this slot; None if it is already published or in flight.

    Atomic: a new row is an INSERT against the unique key; an existing row is taken with one
    conditional UPDATE that only matches when nothing holds a fresh lease on it.
    """
    key = f"{plan.date}:{plan.slot}:{platform}"
    now = utc(app.now())
    with session_scope(app.db) as s:
        existing = s.execute(select(Post.id, Post.error).where(Post.idempotency_key == key)).first()
        if existing is not None:
            res = cast(
                CursorResult[Any],
                s.execute(
                    update(Post)
                    .where(
                        Post.id == existing.id,
                        Post.status.not_in(("published", "dry_run")),
                        or_(Post.claimed_at.is_(None), Post.claimed_at < now - LEASE),
                    )
                    .values(status="pending", error=None, claimed_at=now)
                    .execution_options(synchronize_session=False)
                ),
            )
            if res.rowcount != 1:
                return None
            row = s.get(Post, existing.id)
            assert row is not None
            s.expunge(row)
            return Claim(row, existing.error)
        post = Post(
            idempotency_key=key,
            plan_id=plan.id,
            platform=platform,
            media_type=kind,
            status="pending",
            progress=[],
            slot=plan.slot,
            sky_moment=plan.sky_moment,
            pillar=pillar,
            utm_campaign=f"{plan.date:%Y%m%d}-{plan.slot}",
            claimed_at=now,
        )
        s.add(post)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            return None
        s.expunge(post)
        return Claim(post, None)


def _finish(app: App, post_id: int, **fields: Any) -> None:
    with session_scope(app.db) as s:
        post = s.get(Post, post_id)
        assert post is not None
        for k, v in fields.items():
            setattr(post, k, v)


def _feed_meta(app: App, plan: ContentPlan) -> dict[str, Any]:
    with session_scope(app.db) as s:
        feed = s.scalar(select(ContentPlan).where(ContentPlan.date == plan.date, ContentPlan.slot == "siang_feed"))
        if feed is None:
            return {}
        d = s.scalars(select(DraftRow).where(DraftRow.plan_id == feed.id, DraftRow.status == "passed")).first()
        meta = {"pillar": feed.pillar, "hook_formula": feed.hook_formula, "bridge_feature": feed.bridge_feature, "format": feed.format}
        if d is not None:
            meta["key_fact"] = d.json.get("key_fact")
            meta["novelty_pct"] = (d.judge_report or {}).get("novelty_pct")
        return meta


def publish_plan(app: App, plan_id: int) -> dict[str, Any]:
    with session_scope(app.db) as s:
        plan = s.get(ContentPlan, plan_id)
        if plan is None:
            raise ValueError(f"No plan {plan_id}")
        try:
            ensure_off(s, app.settings.kill_switch)
        except KillSwitchOn as exc:
            # A deliberate halt: recorded as skipped by run_job, not alerted as a failure.
            raise JobSkipped(str(exc)) from None
        s.expunge(plan)
    if plan.scheduled_at and app.now() - utc(plan.scheduled_at) > LATE_LIMIT:
        _mark(app, plan_id, "skipped")
        return {"plan": plan_id, "status": "skipped: too late"}
    data = _payload(app, plan_id)
    if data is None:
        safe_send(app.mailer, f"MISSING {plan.date} {plan.slot}", "No produced content for this slot; nothing was published.")
        _mark(app, plan_id, "failed")
        return {"plan": plan_id, "status": "no payload"}
    spec = slot_spec(plan.slot)
    meta = _feed_meta(app, plan)
    claim = _claim(app, plan, spec.platform, data["kind"], meta.get("pillar"))
    if claim is None:
        return {"plan": plan_id, "status": "already published"}
    post = claim.post
    if not app.settings.publish_live:
        _finish(app, post.id, status="dry_run", published_at=app.now(), hook_text=data.get("hook"), **_post_meta(meta))
        _mark(app, plan_id, "published")
        return {"plan": plan_id, "status": "dry_run", "kind": data["kind"]}
    try:
        result = _send(app, spec.platform, spec.essential, data, post)
    except KillSwitchOn:
        _finish(app, post.id, status="pending", error="kill switch", claimed_at=None)
        raise JobSkipped("kill switch turned on mid-publish") from None
    except QuotaHeld as exc:
        _finish(app, post.id, status="pending", error=QUOTA_HELD, claimed_at=None)
        if claim.previous_error != QUOTA_HELD:
            safe_send(
                app.mailer,
                f"QUOTA ≥80% {plan.date} {plan.slot}",
                f"{exc}. Holding this post; the hourly sweep retries it while quota allows, up to {LATE_LIMIT} after its slot.",
            )
        return {"plan": plan_id, "status": "held", "error": str(exc)}
    except (MetaApiError, ValueError, RuntimeError) as exc:
        _finish(app, post.id, status="failed", error=str(exc), claimed_at=None)
        _mark(app, plan_id, "failed")
        safe_send(app.mailer, f"PUBLISH FAILED {plan.date} {plan.slot}", f"{type(exc).__name__}: {exc}")
        if plan.slot == "siang_feed" and "evergreen_id" not in data:
            return {"plan": plan_id, "status": "failed", "error": str(exc), "retry": _retry_with_evergreen(app, plan)}
        return {"plan": plan_id, "status": "failed", "error": str(exc)}
    _finish(
        app,
        post.id,
        status="published",
        published_at=app.now(),
        platform_media_id=result["id"],
        permalink=result.get("permalink"),
        progress=result.get("progress", []),
        hook_text=data.get("hook"),
        **_post_meta(meta),
    )
    _mark(app, plan_id, "published")
    return {"plan": plan_id, "status": "published", **result}


def _post_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {k: meta.get(k) for k in ("pillar", "hook_formula", "bridge_feature", "format", "key_fact", "novelty_pct")}


def _mark(app: App, plan_id: int, status: str) -> None:
    with session_scope(app.db) as s:
        plan = s.get(ContentPlan, plan_id)
        assert plan is not None
        plan.status = status


def _send(app: App, platform: str, essential: bool, data: dict[str, Any], post: Post) -> dict[str, Any]:
    urls = [app.storage.url(k) for k in data.get("media", [])]
    if platform == "instagram":
        ig = app.instagram(access_token(app, "instagram"))
        used, total = ig.publishing_limit()
        if not allowed(QuotaStatus(used, total), essential):
            raise QuotaHeld(f"Instagram quota {used}/{total}")
        with session_scope(app.db) as s:
            ensure_off(s, app.settings.kill_switch)
        if data["kind"] == "carousel":
            pub = ig.carousel(urls, data["caption"])
        elif data["kind"] == "reel":
            pub = ig.reel(urls[0], data["caption"], app.storage.url(data["cover"]) if data.get("cover") else None)
        elif data["kind"] == "story":
            pub = ig.story(urls[0])
        else:
            raise ValueError(f"Unknown Instagram payload {data['kind']}")
        return {"id": pub.media_id, "permalink": pub.permalink}
    th = app.threads(access_token(app, "threads"))
    used, total, rused, rtotal = th.publishing_limit()
    posts: list[str] = data["posts"]
    done = len(post.progress or [])
    remaining = max(0, len(posts) - done)
    top_level = 1 if done == 0 and remaining else 0  # a resumed chain posts replies only
    replies = remaining - top_level
    posts_ok = not top_level or allowed(QuotaStatus(used, total), essential)
    replies_ok = not replies or allowed(QuotaStatus(rused, rtotal), essential, replies)
    if not (posts_ok and replies_ok):
        raise QuotaHeld(f"Threads quota {used}/{total} posts, {rused}/{rtotal} replies")

    def save(i: int, pid: str) -> None:
        with session_scope(app.db) as s:
            row = s.get(Post, post.id)
            assert row is not None
            row.progress = [*row.progress, pid]

    def guard() -> None:
        with session_scope(app.db) as s:
            ensure_off(s, app.settings.kill_switch)

    images = [app.storage.url(k) for k in data.get("images", [])][:20]
    ids = th.chain(posts, list(post.progress or []), save, first_images=images or None, before_each=guard)
    return {"id": ids[0], "permalink": th.permalink(ids[0]), "progress": ids}


def _retry_with_evergreen(app: App, plan: ContentPlan) -> str:
    """A feed publish that failed after retries falls back to an evergreen post (spec §10.3)."""
    from .produce import _evergreen_feed, _save_payload

    try:
        _, _, feed, _ = _evergreen_feed(app, plan)
        _save_payload(app, plan.id, feed, "fallback")
        with session_scope(app.db) as s:
            row = s.scalar(select(Post).where(Post.idempotency_key == f"{plan.date}:{plan.slot}:instagram"))
            if row is not None:
                s.delete(row)
        return str(publish_plan(app, plan.id).get("status"))
    except Exception as exc:  # noqa: BLE001
        safe_send(app.mailer, f"EVERGREEN FALLBACK FAILED {plan.date}", str(exc))
        return f"evergreen failed: {exc}"


def publish_due(app: App) -> dict[str, Any]:
    """The sweep: every planned slot whose time has come (and is not too late) and has content."""
    now = app.now()
    with session_scope(app.db) as s:
        rows = s.scalars(select(ContentPlan).where(ContentPlan.status.in_(("ready", "fallback")))).all()
        due = [p.id for p in rows if p.scheduled_at is not None and now - LATE_LIMIT <= utc(p.scheduled_at) <= now]
    return {"published": [publish_plan(app, pid) for pid in due]}
