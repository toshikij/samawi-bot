"""metrics (6 h), learn (Sun 21:00), comments-dm (5 min, live only), healthcheck (07:00), tokens (weekly)."""

from __future__ import annotations

import json
import random
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select

from ..alerts import safe_send
from ..app import App
from ..brain.models import HookFormula, Pillar
from ..checks.text import normalize
from ..config import LaunchMode
from ..db import session_scope
from ..db.models import ContentPlan, DmLog, Flag, JobRun, Metric, Post, Token, Weight
from ..kill_switch import is_on
from ..learn.scoring import Observation, deciles, score, thompson_weights, validate_changes
from ..publisher.meta import MetaApiError
from ..publisher.slots import SLOTS, WIB
from ..timeutil import utc
from . import tokens
from .content import TOO_EASY_FLAG

AGES = (6, 24, 72, 168)
IG_FEED_METRICS = ["reach", "saved", "shares", "comments", "likes", "follows", "profile_visits"]
IG_STORY_METRICS = ["reach", "replies", "follows", "profile_visits", "shares"]


# ── metrics ──────────────────────────────────────────────────────────────────────────────────


def run_metrics(app: App) -> dict[str, Any]:
    now = app.now()
    captured, errors = 0, []
    with session_scope(app.db) as s:
        posts = s.scalars(select(Post).where(Post.status == "published", Post.platform_media_id.is_not(None))).all()
        todo = []
        for p in posts:
            if p.published_at is None:
                continue
            age = (now - utc(p.published_at)).total_seconds() / 3600
            have = {m.age_hours for m in s.scalars(select(Metric).where(Metric.post_id == p.id))}
            # An age skipped by a missed run is never back-filled later with older-than-labelled data.
            due = [a for a in AGES if age >= a and a > max(have, default=0)]
            if due:
                todo.append((p.id, p.platform, p.media_type, str(p.platform_media_id), max(due)))
    ig = th = None
    for pid, platform, kind, media_id, age in todo:
        try:
            if platform == "instagram":
                ig = ig or app.instagram(tokens.access_token(app, "instagram"))
                m = ig.insights(media_id, IG_STORY_METRICS if kind == "story" else IG_FEED_METRICS)
                values = {
                    "reach": m.get("reach", 0),
                    "saves": m.get("saved", 0),
                    "shares": m.get("shares", 0),
                    "comments": m.get("comments", 0),
                    "likes": m.get("likes", 0),
                    "follows": m.get("follows", 0),
                    "profile_visits": m.get("profile_visits", 0),
                    "replies": m.get("replies", 0),
                }
            else:
                th = th or app.threads(tokens.access_token(app, "threads"))
                m = th.insights(media_id)
                values = {
                    "reach": m.get("views", 0),
                    "views": m.get("views", 0),
                    "likes": m.get("likes", 0),
                    "replies": m.get("replies", 0),
                    "reposts": m.get("reposts", 0) + m.get("quotes", 0),
                    "shares": m.get("shares", 0),
                }
        except (MetaApiError, tokens.TokenMissing, tokens.TokenExpired) as exc:
            errors.append(f"post {pid}: {exc}")
            continue
        with session_scope(app.db) as s:
            s.add(Metric(post_id=pid, captured_at=now, age_hours=age, **values))
        captured += 1
    return {"captured": captured, "errors": errors[:10]}


# ── learn ────────────────────────────────────────────────────────────────────────────────────

ARMS: dict[str, list[str]] = {
    "pillar": [p.value for p in Pillar],
    "hook_formula": [h.value for h in HookFormula],
    "format": ["carousel", "reel"],
    "slot": [s.name for s in SLOTS],
}


def observations(app: App) -> tuple[list[Observation], list[tuple[Post, float]]]:
    obs: list[Observation] = []
    scored: list[tuple[Post, float]] = []
    with session_scope(app.db) as s:
        rows = s.execute(select(Post, Metric).join(Metric, Metric.post_id == Post.id).where(Metric.age_hours == 72, Post.platform == "instagram")).all()
        for post, m in rows:
            sc = score(m.reach, m.shares, m.saves, m.follows, m.comments, m.profile_visits)
            scored.append((post, sc))
            for dim, value in (
                ("pillar", post.pillar),
                ("hook_formula", post.hook_formula),
                ("format", post.format),
                ("slot", post.slot),
                ("bridge_feature", post.bridge_feature),
            ):
                if value:
                    obs.append(Observation(dim, value, sc))
            s.expunge(post)
    return obs, scored


def run_learn(app: App) -> dict[str, Any]:
    obs, scored = observations(app)
    rng = random.Random(app.now().date().toordinal())
    bridge_arms = [f.key for f in app.sources.features.bridgeable()]
    changes = {dim: thompson_weights(arms, [o for o in obs if o.dimension == dim], rng) for dim, arms in {**ARMS, "bridge_feature": bridge_arms}.items()}
    validate_changes(changes)
    with session_scope(app.db) as s:
        before = {(w.dimension, w.key): w.weight for w in s.scalars(select(Weight))}
        for dim, weights in changes.items():
            for key, w in weights.items():
                row = s.scalar(select(Weight).where(Weight.dimension == dim, Weight.key == key))
                if row is None:
                    s.add(Weight(dimension=dim, key=key, weight=w, updated_at=app.now()))
                else:
                    row.weight, row.updated_at = w, app.now()
        top, bottom = deciles([(str(p.id), sc) for p, sc in scored])
        by_id = {str(p.id): (p, sc) for p, sc in scored}
        hooks = [by_id[i][0].hook_text for i in top if by_id[i][0].hook_text]
        s.merge(Flag(name="hook_examples", value=json.dumps(hooks[:10], ensure_ascii=False), updated_at=app.now(), updated_by="learn"))
        learned = _learned_too_easy(s, [by_id[i] for i in bottom], app.now())
    lines = ["Weight changes:"]
    for dim, weights in changes.items():
        for key, w in sorted(weights.items(), key=lambda kv: -kv[1])[:5]:
            lines.append(f"  {dim}.{key}: {before.get((dim, key), 0):.2f} → {w:.2f}")
    ranked = sorted(scored, key=lambda x: -x[1])
    lines += ["", "Top 3:", *(f"  {p.hook_text} ({p.permalink}) score {sc:.3f}" for p, sc in ranked[:3])]
    lines += ["", "Bottom 3:", *(f"  {p.hook_text} ({p.permalink}) score {sc:.3f}" for p, sc in ranked[-3:])]
    if learned:
        lines += ["", "Added to the too-easy list:", *(f"  {t}" for t in learned)]
    safe_send(app.mailer, "Weekly learning applied", "\n".join(lines))
    return {"posts_scored": len(scored), "dimensions": list(changes), "too_easy_added": learned}


def _learned_too_easy(s: Any, bottom: list[tuple[Post, float]], now: Any) -> list[str]:
    """Bottom-decile facts whose novelty was borderline (< 70%) join the too-easy list (spec §9)."""
    flag = s.get(Flag, TOO_EASY_FLAG)
    entries = json.loads(flag.value) if flag else []
    added = []
    for post, _ in bottom:
        if post.key_fact and post.novelty_pct is not None and post.novelty_pct < 70:
            words = [w for w in normalize(post.key_fact).split() if len(w) > 3][:4]
            if len(words) >= 2:
                entries.append({"id": f"learned-{post.id}", "text": post.key_fact, "all_of": [[w] for w in words]})
                added.append(post.key_fact)
    if added:
        s.merge(Flag(name=TOO_EASY_FLAG, value=json.dumps(entries, ensure_ascii=False), updated_at=now, updated_by="learn"))
    return added


# ── comments → DM (live mode only) ───────────────────────────────────────────────────────────

KEYWORD = re.compile(r"(^| )samawi( |$)")
DM_WINDOW = timedelta(days=7)


def utm_link(base: str, platform: str, campaign: str) -> str:
    sep = "&" if "?" in base else "?"
    return f"{base}{sep}{urlencode({'utm_source': platform, 'utm_medium': 'social', 'utm_campaign': campaign})}"


def run_comments_dm(app: App) -> dict[str, Any]:
    st = app.settings
    if st.launch_mode is not LaunchMode.LIVE or not st.play_store_url:
        return {"skipped": "prelaunch"}
    with session_scope(app.db) as s:
        if is_on(s, st.kill_switch):
            return {"skipped": "kill switch"}
        since = app.now() - DM_WINDOW
        posts = [
            (p.id, str(p.platform_media_id), p.utm_campaign)
            for p in s.scalars(select(Post).where(Post.platform == "instagram", Post.status == "published", Post.media_type.in_(("carousel", "reel"))))
            if p.published_at and utc(p.published_at) >= since
        ]
    ig = app.instagram(tokens.access_token(app, "instagram"))
    sent = 0
    errors: list[str] = []
    for pid, media_id, campaign in posts:
        for c in ig.comments(media_id):
            if not KEYWORD.search(normalize(c.get("text", ""))):
                continue
            ts = c.get("timestamp")
            if ts and app.now() - utc(_parse_ts(ts)) > DM_WINDOW:
                continue
            user = (c.get("from") or {}).get("id") or c.get("username") or c["id"]
            with session_scope(app.db) as s:
                if s.scalar(select(DmLog).where(DmLog.post_id == pid, DmLog.user_id == str(user))) is not None:
                    continue
                if s.scalar(select(DmLog).where(DmLog.comment_id == c["id"])) is not None:
                    continue
            link = utm_link(st.play_store_url, "instagram", campaign or "dm")
            try:
                ig.private_reply(c["id"], f"Assalamu'alaikum! Ini link Samawi di Google Play, tanpa iklan: {link}")
                status, err = "sent", None
                sent += 1
            except MetaApiError as exc:
                status, err = "failed", str(exc)
                errors.append(str(exc))
            with session_scope(app.db) as s:
                s.add(DmLog(comment_id=c["id"], user_id=str(user), post_id=pid, sent_at=app.now(), status=status, error=err))
    return {"sent": sent, "errors": errors[:5]}


def _parse_ts(ts: str) -> Any:
    from datetime import datetime

    return datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")


# ── tokens & healthcheck ─────────────────────────────────────────────────────────────────────


def run_tokens(app: App) -> dict[str, Any]:
    reports = [tokens.refresh(app, p) for p in ("instagram", "threads")]
    issues = tokens.problems(reports)
    if issues:
        safe_send(app.mailer, "Token attention needed", "\n".join(issues))
    return {"reports": [r.__dict__ for r in reports], "issues": issues}


def run_healthcheck(app: App) -> dict[str, Any]:
    now = app.now()
    since = now - timedelta(hours=24)
    with session_scope(app.db) as s:
        posts = [p for p in s.scalars(select(Post)) if p.published_at and utc(p.published_at) >= since]
        runs = [r for r in s.scalars(select(JobRun).where(JobRun.status == "failed")) if utc(r.started_at) >= since]
        today = now.astimezone(WIB).date()
        plans = s.scalars(select(ContentPlan).where(ContentPlan.date == today)).all()
        tok = {t.platform: t.expires_at for t in s.scalars(select(Token))}
        kill = is_on(s, app.settings.kill_switch)
        lines = [
            f"Mode: {'LIVE' if app.settings.publish_live else 'DRY RUN'} · launch {app.settings.launch_mode.value} · kill switch {'ON' if kill else 'off'}",
            "",
            "Published (24 h):",
            *(f"  {p.platform} {p.media_type} {p.status} {p.permalink or ''}" for p in posts),
            "",
            f"Today's plan ({today}):",
            *(f"  {p.slot}: {p.status}" for p in sorted(plans, key=lambda x: utc(x.scheduled_at) if x.scheduled_at else now)),
            "",
            "Failures (24 h):" if runs else "Failures (24 h): none",
            *(f"  {r.job}: {r.error}" for r in runs),
            "",
            "Tokens:",
            *(f"  {k}: expires {utc(v):%Y-%m-%d}" for k, v in tok.items()),
        ]
    quota = []
    try:
        if app.settings.ig_user_id:
            used, total = app.instagram(tokens.access_token(app, "instagram")).publishing_limit()
            quota.append(f"  Instagram {used}/{total}")
        if app.settings.threads_user_id:
            pu, pt, ru, rt = app.threads(tokens.access_token(app, "threads")).publishing_limit()
            quota.append(f"  Threads {pu}/{pt} posts, {ru}/{rt} replies")
    except (MetaApiError, tokens.TokenMissing, tokens.TokenExpired) as exc:
        quota.append(f"  quota check failed: {exc}")
    lines += ["", "Quota (24 h):", *quota]
    failed = any(p.status == "failed" for p in plans) or bool(runs)
    fallback = any(p.status == "fallback" for p in plans)
    verdict = "FAIL" if failed else "FALLBACK" if fallback else "OK"
    safe_send(app.mailer, f"Daily {verdict} {today}", "\n".join(lines))
    return {"verdict": verdict}
