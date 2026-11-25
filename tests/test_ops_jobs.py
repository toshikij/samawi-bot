"""metrics, learn, comments-dm, tokens and healthcheck jobs (spec §8.1, §9), over a fake Graph API."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from conftest import NOW
from samawi_bot.app import App
from samawi_bot.db import session_scope
from samawi_bot.db.models import ContentPlan, DmLog, Flag, JobRun, Metric, Post, Token, Weight
from samawi_bot.jobs import ops
from samawi_bot.jobs.content import TOO_EASY_FLAG
from samawi_bot.kill_switch import set_switch
from samawi_bot.learn.scoring import ALLOWED_DIMENSIONS, LearnerViolation

IG_USER = "17841400000000000"
TH_USER = "2600000000000000"
PLAY = "https://play.google.com/store/apps/details?id=id.samawi"

Route = Callable[[httpx.Request], httpx.Response]


class FakeMeta:
    """Routes by (method, path); unknown routes 404. Records every request."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Route | dict[str, Any]] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, reply: Route | dict[str, Any]) -> None:
        self.routes[(method, path)] = reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.routes.get((request.method, request.url.path))
        if reply is None:
            return httpx.Response(404, json={"error": {"message": f"no route {request.method} {request.url.path}", "code": 803}})
        if callable(reply):
            return reply(request)
        return httpx.Response(200, json=reply)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]


def params(r: httpx.Request) -> dict[str, str]:
    q = {k: v[0] for k, v in parse_qs(urlparse(str(r.url)).query).items()}
    if r.method == "POST":
        q.update({k: v[0] for k, v in parse_qs(r.content.decode()).items()})
    return q


def error(status: int = 400, code: int = 100, msg: str = "bad thing") -> Route:
    return lambda r: httpx.Response(status, json={"error": {"message": msg, "code": code}})


def add_post(app: App, **kw: Any) -> int:
    base: dict[str, Any] = {
        "idempotency_key": f"k-{datetime.now().timestamp()}-{id(kw)}-{len(kw)}-{kw.get('platform_media_id')}",
        "platform": "instagram",
        "media_type": "carousel",
        "status": "published",
        "platform_media_id": "m1",
        "published_at": NOW - timedelta(hours=30),
    }
    base.update(kw)
    with session_scope(app.db) as s:
        p = Post(**base)
        s.add(p)
        s.flush()
        return p.id


def metrics_of(app: App) -> list[tuple[int, int, dict[str, int]]]:
    with session_scope(app.db) as s:
        rows = s.query(Metric).order_by(Metric.post_id, Metric.age_hours).all()
        cols = ("reach", "saves", "shares", "comments", "likes", "follows", "profile_visits", "replies", "reposts", "views")
        return [(m.post_id, m.age_hours, {c: getattr(m, c) for c in cols}) for m in rows]


def ig_insights(values: dict[str, int]) -> dict[str, Any]:
    data = []
    for i, (k, v) in enumerate(values.items()):
        data.append({"name": k, "total_value": {"value": v}} if i % 2 else {"name": k, "values": [{"value": v}]})
    return {"data": data}


# ── metrics ──────────────────────────────────────────────────────────────────────────────────


def test_metrics_feed_story_and_threads(make_app: Callable[..., App], clock: Any) -> None:
    meta = FakeMeta()
    meta.on(
        "GET", "/v23.0/feed1/insights", ig_insights({"reach": 1000, "saved": 50, "shares": 20, "comments": 5, "likes": 70, "follows": 3, "profile_visits": 9})
    )
    meta.on("GET", "/v23.0/story1/insights", ig_insights({"reach": 400, "replies": 2, "follows": 1, "profile_visits": 4, "shares": 6}))
    meta.on("GET", "/v1.0/th1/insights", ig_insights({"views": 900, "likes": 30, "replies": 4, "reposts": 2, "quotes": 1, "shares": 7}))
    app = make_app(meta)
    feed = add_post(app, platform_media_id="feed1", published_at=NOW - timedelta(hours=7))
    story = add_post(app, platform_media_id="story1", media_type="story", published_at=NOW - timedelta(hours=25))
    th = add_post(app, platform="threads", media_type="chain", platform_media_id="th1", published_at=NOW - timedelta(hours=80))
    add_post(app, platform_media_id="young", published_at=NOW - timedelta(hours=2))  # nothing due yet
    add_post(app, platform_media_id="nodate", published_at=None)
    add_post(app, platform_media_id=None)
    add_post(app, platform_media_id="pending", status="pending")

    out = ops.run_metrics(app)
    assert out == {"captured": 3, "errors": []}
    got = {(pid, age): v for pid, age, v in metrics_of(app)}
    assert set(got) == {(feed, 6), (story, 24), (th, 72)}
    assert got[(feed, 6)] == {
        "reach": 1000,
        "saves": 50,
        "shares": 20,
        "comments": 5,
        "likes": 70,
        "follows": 3,
        "profile_visits": 9,
        "replies": 0,
        "reposts": 0,
        "views": 0,
    }
    assert got[(story, 24)]["reach"] == 400 and got[(story, 24)]["replies"] == 2 and got[(story, 24)]["saves"] == 0
    assert got[(th, 72)] == {
        "reach": 900,
        "saves": 0,
        "shares": 7,
        "comments": 0,
        "likes": 30,
        "follows": 0,
        "profile_visits": 0,
        "replies": 4,
        "reposts": 3,
        "views": 900,
    }

    (feed_req,) = meta.calls("GET", "/v23.0/feed1/insights")
    assert params(feed_req)["metric"] == ",".join(ops.IG_FEED_METRICS)
    assert params(feed_req)["access_token"] == "IG-TOKEN"
    (story_req,) = meta.calls("GET", "/v23.0/story1/insights")
    assert params(story_req)["metric"] == ",".join(ops.IG_STORY_METRICS)
    assert "saved" not in params(story_req)["metric"]
    (th_req,) = meta.calls("GET", "/v1.0/th1/insights")
    assert params(th_req)["metric"] == "views,likes,replies,reposts,quotes,shares"
    assert params(th_req)["access_token"] == "TH-TOKEN"

    # a second run at the same time captures nothing again
    assert ops.run_metrics(app) == {"captured": 0, "errors": []}
    assert len(meta.calls("GET", "/v23.0/feed1/insights")) == 1


def test_each_age_is_captured_once_and_never_backfilled(make_app: Callable[..., App], clock: Any) -> None:
    meta = FakeMeta()
    meta.on("GET", "/v23.0/m1/insights", ig_insights({"reach": 10}))
    app = make_app(meta)
    published = NOW - timedelta(hours=6)
    pid = add_post(app, platform_media_id="m1", published_at=published)
    seen: list[int] = []
    for hours in (6, 12, 24, 30, 100, 160, 168, 200, 400):
        clock.now = published + timedelta(hours=hours)
        ops.run_metrics(app)
        seen = [age for p, age, _ in metrics_of(app) if p == pid]
    assert seen == [6, 24, 72, 168]
    assert len(meta.calls("GET", "/v23.0/m1/insights")) == 4


def test_missed_runs_capture_the_latest_age_only(make_app: Callable[..., App], clock: Any) -> None:
    meta = FakeMeta()
    meta.on("GET", "/v23.0/m1/insights", ig_insights({"reach": 10}))
    app = make_app(meta)
    pid = add_post(app, platform_media_id="m1", published_at=NOW - timedelta(hours=30))
    ops.run_metrics(app)
    clock.now = NOW + timedelta(hours=6)
    ops.run_metrics(app)
    assert [age for p, age, _ in metrics_of(app) if p == pid] == [24]


def test_meta_error_is_recorded_not_raised(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("GET", "/v23.0/bad/insights", error(400, 100, "media deleted"))
    meta.on("GET", "/v23.0/good/insights", ig_insights({"reach": 5}))
    app = make_app(meta)
    bad = add_post(app, platform_media_id="bad")
    good = add_post(app, platform_media_id="good")
    out = ops.run_metrics(app)
    assert out["captured"] == 1
    assert len(out["errors"]) == 1
    assert out["errors"][0].startswith(f"post {bad}: ")
    assert "media deleted" in out["errors"][0]
    assert [p for p, _, _ in metrics_of(app)] == [good]


def test_missing_token_is_recorded(make_app: Callable[..., App]) -> None:
    app = make_app(FakeMeta(), threads_access_token="")
    pid = add_post(app, platform="threads", platform_media_id="t1", media_type="text")
    out = ops.run_metrics(app)
    assert out["captured"] == 0
    assert out["errors"] and out["errors"][0].startswith(f"post {pid}: No threads token")


def test_errors_are_capped_at_ten(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    app = make_app(meta)  # every insights call 404s
    for i in range(12):
        add_post(app, platform_media_id=f"x{i}")
    out = ops.run_metrics(app)
    assert out["captured"] == 0 and len(out["errors"]) == 10


# ── learn ────────────────────────────────────────────────────────────────────────────────────


def add_scored(app: App, n: int, *, reach: int = 1000, follows: int = 1, **post: Any) -> int:
    pid = add_post(app, platform_media_id=f"p{n}-{post.get('hook_text', '')}", **post)
    with session_scope(app.db) as s:
        s.add(Metric(post_id=pid, captured_at=NOW, age_hours=72, reach=reach, follows=follows, shares=n, saves=n))
    return pid


def weights_of(app: App) -> dict[tuple[str, str], float]:
    with session_scope(app.db) as s:
        return {(w.dimension, w.key): w.weight for w in s.query(Weight)}


def flag(app: App, name: str) -> Any:
    with session_scope(app.db) as s:
        f = s.get(Flag, name)
        return json.loads(f.value) if f else None


def test_learn_cold_start(make_app: Callable[..., App]) -> None:
    app = make_app()
    out = ops.run_learn(app)
    assert out["posts_scored"] == 0
    assert out["too_easy_added"] == []
    assert set(out["dimensions"]) == {"pillar", "hook_formula", "format", "slot", "bridge_feature"}
    w = weights_of(app)
    assert {d for d, _ in w} <= ALLOWED_DIMENSIONS
    assert all(v > 0 for v in w.values())
    pillar = {k: v for (d, k), v in w.items() if d == "pillar"}
    assert len(pillar) == 8 and sum(pillar.values()) == pytest.approx(1.0)
    bridge_keys = {k for (d, k) in w if d == "bridge_feature"}
    assert bridge_keys == {f.key for f in app.sources.features.bridgeable()}
    assert "customAdzanVoices" not in bridge_keys
    assert flag(app, "hook_examples") == []
    assert flag(app, TOO_EASY_FLAG) is None
    subject, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert subject == "Weekly learning applied"
    assert "Weight changes:" in body and "Top 3:" in body and "Bottom 3:" in body
    assert "too-easy" not in body


def test_learn_scores_posts_and_applies(make_app: Callable[..., App]) -> None:
    app = make_app()
    with session_scope(app.db) as s:
        s.add(Weight(dimension="pillar", key="fakta_quran", weight=0.2, updated_at=NOW - timedelta(days=7)))
    ids = []
    for i in range(20):
        ids.append(
            add_scored(
                app,
                i,
                pillar="fakta_quran" if i >= 10 else "doa_dzikir",
                hook_formula="ternyata",
                format="carousel",
                slot="siang_feed",
                bridge_feature="tafsir",
                hook_text=f"Hook nomor {i}",
                permalink=f"https://instagram.com/p/{i}",
                key_fact=f"Fakta kunci tentang sesuatu bernomor {i}" if i < 2 else None,
                novelty_pct=65 if i == 0 else 80,
            )
        )
    add_scored(app, 99, platform="threads", hook_text="threads post")  # not instagram: ignored
    no72 = add_post(app, platform_media_id="no72", hook_text="only 24h")
    with session_scope(app.db) as s:
        s.add(Metric(post_id=no72, captured_at=NOW, age_hours=24, reach=10, shares=10))

    out = ops.run_learn(app)
    assert out["posts_scored"] == 20
    w = weights_of(app)
    assert w[("pillar", "fakta_quran")] > w[("pillar", "doa_dzikir")]
    assert w[("pillar", "fakta_quran")] != 0.2  # the existing row was updated
    assert all(v > 0 for v in w.values())
    assert {d for d, _ in w} <= ALLOWED_DIMENSIONS
    # top decile (2 of 20) hooks become writer examples
    assert flag(app, "hook_examples") == ["Hook nomor 19", "Hook nomor 18"]
    # bottom decile: post 0 (novelty 65 → borderline) joins too-easy; post 1 (novelty 80) does not
    assert out["too_easy_added"] == ["Fakta kunci tentang sesuatu bernomor 0"]
    entries = flag(app, TOO_EASY_FLAG)
    assert entries == [
        {"id": f"learned-{ids[0]}", "text": "Fakta kunci tentang sesuatu bernomor 0", "all_of": [["fakta"], ["kunci"], ["tentang"], ["sesuatu"]]}
    ]
    _, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert "pillar.fakta_quran: 0.20 → " in body
    assert "Hook nomor 19 (https://instagram.com/p/19)" in body.split("Bottom 3:")[0]
    assert "Hook nomor 0 (https://instagram.com/p/0)" in body.split("Bottom 3:")[1]
    assert "Added to the too-easy list:\n  Fakta kunci tentang sesuatu bernomor 0" in body


def test_learn_appends_to_existing_too_easy_and_skips_short_facts(make_app: Callable[..., App]) -> None:
    app = make_app()
    existing = [{"id": "learned-old", "text": "lama", "all_of": [["lama"]]}]
    with session_scope(app.db) as s:
        s.add(Flag(name=TOO_EASY_FLAG, value=json.dumps(existing), updated_at=NOW, updated_by="learn"))
    add_scored(app, 0, key_fact="Ini itu salat", novelty_pct=10, hook_text="a")  # only one word > 3 letters
    add_scored(app, 1, key_fact="Puasa Ayyamul Bidh tiap bulan Hijriah", novelty_pct=50, hook_text="b")
    add_scored(app, 2, key_fact=None, novelty_pct=10, hook_text="c")
    add_scored(app, 3, key_fact="Fakta tanpa skor novelty", novelty_pct=None, hook_text="d")
    add_scored(app, 4, hook_text=None)
    out = ops.run_learn(app)
    assert out["posts_scored"] == 5
    assert out["too_easy_added"] == []  # bottom decile is post 0 alone, and it is too short
    assert flag(app, TOO_EASY_FLAG) == existing


def test_learn_adds_to_existing_flag(make_app: Callable[..., App]) -> None:
    app = make_app()
    existing = [{"id": "learned-old", "text": "lama", "all_of": [["lama"]]}]
    with session_scope(app.db) as s:
        s.add(Flag(name=TOO_EASY_FLAG, value=json.dumps(existing), updated_at=NOW, updated_by="learn"))
    add_scored(app, 0, key_fact="Puasa Ayyamul Bidh tiap bulan Hijriah", novelty_pct=50, hook_text="a")
    add_scored(app, 5, hook_text="b")
    out = ops.run_learn(app)
    assert out["too_easy_added"] == ["Puasa Ayyamul Bidh tiap bulan Hijriah"]
    entries = flag(app, TOO_EASY_FLAG)
    assert entries[0] == existing[0]
    assert entries[1]["all_of"] == [["puasa"], ["ayyamul"], ["bidh"], ["tiap"]]
    # zero-reach posts score 0 without error and still count
    add_scored(app, 6, reach=0, hook_text="zero")
    assert ops.run_learn(app)["posts_scored"] == 3


def test_learn_rejects_a_change_outside_allowed_dimensions(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    """A learner change attempting to unblock a topic → rejected, nothing written."""
    app = make_app()
    monkeypatch.setitem(ops.ARMS, "blocked_topics", ["politik"])
    with pytest.raises(LearnerViolation):
        ops.run_learn(app)
    assert weights_of(app) == {}
    assert app.mailer.sent == []  # type: ignore[attr-defined]


# ── comments → DM ────────────────────────────────────────────────────────────────────────────


def live(make_app: Callable[..., App], meta: FakeMeta, **kw: Any) -> App:
    return make_app(meta, launch_mode="live", play_store_url=PLAY, **kw)


def comment(cid: str, text: str, user: str | None = "u1", ts: str | None = "2026-11-25T18:00:00+0000", **extra: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"id": cid, "text": text, **extra}
    if user is not None:
        c["from"] = {"id": user, "username": f"name-{user}"}
    if ts is not None:
        c["timestamp"] = ts
    return c


def dm_rows(app: App) -> list[tuple[str, str, int, str, str | None]]:
    with session_scope(app.db) as s:
        return [(d.comment_id, d.user_id, d.post_id, d.status, d.error) for d in s.query(DmLog).order_by(DmLog.id)]


def sent_messages(meta: FakeMeta) -> list[tuple[str, str]]:
    out = []
    for r in meta.calls("POST", f"/v23.0/{IG_USER}/messages"):
        p = params(r)
        out.append((json.loads(p["recipient"])["comment_id"], json.loads(p["message"])["text"]))
    return out


def test_prelaunch_never_sends(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("GET", "/v23.0/m1/comments", {"data": [comment("c1", "samawi")]})
    for app in (make_app(meta), make_app(meta, play_store_url=PLAY), make_app(meta, launch_mode="live", play_store_url="")):
        add_post(app)
        assert ops.run_comments_dm(app) == {"skipped": "prelaunch"}
    assert meta.requests == []


@pytest.mark.parametrize("where", ["env", "dashboard"])
def test_kill_switch_skips(make_app: Callable[..., App], where: str) -> None:
    meta = FakeMeta()
    meta.on("GET", "/v23.0/m1/comments", {"data": [comment("c1", "samawi")]})
    app = live(make_app, meta, kill_switch=where == "env")
    if where == "dashboard":
        with session_scope(app.db) as s:
            set_switch(s, True, "owner", NOW)
    add_post(app)
    assert ops.run_comments_dm(app) == {"skipped": "kill switch"}
    assert meta.requests == []


def test_keyword_variants_dedupe_and_window(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("POST", f"/v23.0/{IG_USER}/messages", {"recipient_id": "x", "message_id": "mid"})
    meta.on(
        "GET",
        "/v23.0/m1/comments",
        {
            "data": [
                comment("c1", "samawi!", user="u1"),
                comment("c2", "SAMAWI 🙏", user="u2"),
                comment("c3", "Samawi.", user="u3"),
                comment("c4", "mau dong, Samawi", user="u1"),  # same user, same post → no second DM
                comment("c5", "assamawi", user="u5"),
                comment("c6", "samawiku keren", user="u6"),
                comment("c7", "nice post", user="u7"),
                comment("c8", "samawi", user="u8", ts="2026-11-18T18:59:59+0000"),  # > 7 days old
                comment("c9", "samawi", user="u9", ts="2026-11-18T19:00:00Z"),  # exactly 7 days: allowed
                comment("c10", "samawi", user=None, username="anon"),
                comment("c11", "samawi", user=None, ts=None),
                {"id": "c12"},  # no text at all
            ]
        },
    )
    app = live(make_app, meta)
    pid = add_post(app, utm_campaign="2026-11-24-siang_feed", published_at=NOW - timedelta(days=1))
    out = ops.run_comments_dm(app)
    assert out == {"sent": 6, "errors": []}
    msgs = sent_messages(meta)
    assert [c for c, _ in msgs] == ["c1", "c2", "c3", "c9", "c10", "c11"]
    link = f"{PLAY}&utm_source=instagram&utm_medium=social&utm_campaign=2026-11-24-siang_feed"
    assert msgs[0][1] == f"Assalamu'alaikum! Ini link Samawi di Google Play, tanpa iklan: {link}"
    rows = dm_rows(app)
    assert [(c, u) for c, u, *_ in rows] == [("c1", "u1"), ("c2", "u2"), ("c3", "u3"), ("c9", "u9"), ("c10", "anon"), ("c11", "c11")]
    assert all(r[2] == pid and r[3] == "sent" and r[4] is None for r in rows)

    # running again sends nothing new
    assert ops.run_comments_dm(app) == {"sent": 0, "errors": []}
    assert len(sent_messages(meta)) == 6


def test_only_recent_feed_posts_are_scanned(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("POST", f"/v23.0/{IG_USER}/messages", {})
    for m in ("old", "story", "threads", "reel", "nodate", "pending"):
        meta.on("GET", f"/v23.0/{m}/comments", {"data": [comment(f"c-{m}", "samawi")]})
    app = live(make_app, meta)
    add_post(app, platform_media_id="old", published_at=NOW - timedelta(days=8))
    add_post(app, platform_media_id="story", media_type="story")
    add_post(app, platform_media_id="threads", platform="threads", media_type="chain")
    add_post(app, platform_media_id="reel", media_type="reel")
    add_post(app, platform_media_id="nodate", published_at=None)
    add_post(app, platform_media_id="pending", status="pending")
    out = ops.run_comments_dm(app)
    assert out["sent"] == 1
    assert [r.url.path for r in meta.requests if r.method == "GET"] == ["/v23.0/reel/comments"]
    # no campaign stored → the "dm" campaign
    assert sent_messages(meta)[0][1].endswith("utm_campaign=dm")


def test_comment_already_logged_for_another_post_is_skipped(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("POST", f"/v23.0/{IG_USER}/messages", {})
    meta.on("GET", "/v23.0/m1/comments", {"data": [comment("c1", "samawi", user="new-user")]})
    app = live(make_app, meta)
    other = add_post(app, platform_media_id="other", published_at=NOW - timedelta(days=30))
    add_post(app, platform_media_id="m1")
    with session_scope(app.db) as s:
        s.add(DmLog(comment_id="c1", user_id="someone", post_id=other, sent_at=NOW, status="sent"))
    assert ops.run_comments_dm(app) == {"sent": 0, "errors": []}


def test_dm_api_error_is_logged_as_failed(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("POST", f"/v23.0/{IG_USER}/messages", error(400, 10, "messaging not allowed"))
    meta.on("GET", "/v23.0/m1/comments", {"data": [comment("c1", "SAMAWI"), comment("c2", "samawi", user="u2")]})
    app = live(make_app, meta)
    pid = add_post(app)
    out = ops.run_comments_dm(app)
    assert out["sent"] == 0
    assert len(out["errors"]) == 2 and "messaging not allowed" in out["errors"][0]
    rows = dm_rows(app)
    assert [(r[0], r[2], r[3]) for r in rows] == [("c1", pid, "failed"), ("c2", pid, "failed")]
    assert "messaging not allowed" in (rows[0][4] or "")
    # a failed DM is not retried for that user on that post
    assert ops.run_comments_dm(app)["sent"] == 0
    assert len(meta.calls("POST", f"/v23.0/{IG_USER}/messages")) == 2


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        (
            "https://play.google.com/store/apps/details?id=id.samawi",
            "https://play.google.com/store/apps/details?id=id.samawi&utm_source=instagram&utm_medium=social&utm_campaign=a+b",
        ),
        ("https://samawi.id/get", "https://samawi.id/get?utm_source=instagram&utm_medium=social&utm_campaign=a+b"),
    ],
)
def test_utm_link(base: str, expected: str) -> None:
    assert ops.utm_link(base, "instagram", "a b") == expected


def test_parse_ts() -> None:
    from datetime import UTC

    assert ops._parse_ts("2026-11-25T18:00:00+0000") == datetime(2026, 11, 25, 18, tzinfo=UTC)
    assert ops._parse_ts("2026-11-25T18:00:00Z") == datetime(2026, 11, 25, 18, tzinfo=UTC)


# ── tokens ───────────────────────────────────────────────────────────────────────────────────


def test_tokens_fresh_no_alert(make_app: Callable[..., App]) -> None:
    app = make_app(FakeMeta())
    out = ops.run_tokens(app)
    assert [r["status"] for r in out["reports"]] == ["fresh", "fresh"]
    assert out["issues"] == []
    assert app.mailer.sent == []  # type: ignore[attr-defined]


def test_tokens_alert_on_problems(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("GET", "/v23.0/oauth/access_token", error(400, 190, "token revoked"))
    app = make_app(meta, threads_access_token="")
    with session_scope(app.db) as s:
        s.add(Token(platform="instagram", access_token="IG-TOKEN", issued_at=NOW - timedelta(days=55), expires_at=NOW + timedelta(days=5)))
    out = ops.run_tokens(app)
    assert {r["platform"]: r["status"] for r in out["reports"]} == {"instagram": "failed", "threads": "missing"}
    assert any("instagram token failed" in i and "token revoked" in i for i in out["issues"])
    assert "instagram token expires in 5 days. Re-authorise (docs/META-SETUP.md)." in out["issues"]
    assert "threads token missing" in out["issues"]
    subject, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert subject == "Token attention needed"
    assert "threads token missing" in body


# ── healthcheck ──────────────────────────────────────────────────────────────────────────────


def quota_meta() -> FakeMeta:
    meta = FakeMeta()
    meta.on("GET", f"/v23.0/{IG_USER}/content_publishing_limit", {"data": [{"quota_usage": 4, "config": {"quota_total": 50}}]})
    meta.on(
        "GET",
        f"/v1.0/{TH_USER}/threads_publishing_limit",
        {"data": [{"quota_usage": 12, "config": {"quota_total": 250}, "reply_quota_usage": 30, "reply_config": {"quota_total": 1000}}]},
    )
    return meta


def add_plan(app: App, slot: str, status: str, day: Any = None, at: datetime | None = None) -> None:
    with session_scope(app.db) as s:
        s.add(ContentPlan(date=day or NOW.date() + timedelta(days=1), slot=slot, status=status, scheduled_at=at, bridge_options=[]))


def test_healthcheck_ok(make_app: Callable[..., App]) -> None:
    app = make_app(quota_meta())
    add_post(app, platform_media_id="m1", permalink="https://instagram.com/p/x", published_at=NOW - timedelta(hours=3))
    add_post(app, platform_media_id="old", permalink="https://instagram.com/p/old", published_at=NOW - timedelta(hours=30))
    add_post(app, platform_media_id="unpub", status="pending", published_at=None)
    add_plan(app, "siang_feed", "published", at=NOW + timedelta(hours=6))
    add_plan(app, "subuh_story", "published", at=NOW - timedelta(hours=1))
    add_plan(app, "malam_story", "planned")
    add_plan(app, "siang_feed", "failed", day=NOW.date() - timedelta(days=3))  # not today
    with session_scope(app.db) as s:
        s.add(JobRun(job="produce", started_at=NOW - timedelta(hours=30), status="failed", error="old failure"))
        s.add(JobRun(job="publish", started_at=NOW - timedelta(hours=2), status="ok"))
        s.add(Token(platform="instagram", access_token="IG-TOKEN", issued_at=NOW, expires_at=datetime(2027, 1, 24, 19)))
    out = ops.run_healthcheck(app)
    assert out == {"verdict": "OK"}
    subject, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert subject == "Daily OK 2026-11-26"
    assert "Mode: LIVE · launch prelaunch · kill switch off" in body
    assert "instagram carousel published https://instagram.com/p/x" in body
    assert "p/old" not in body
    assert "Today's plan (2026-11-26):" in body
    lines = body.splitlines()
    assert lines.index("  subuh_story: published") < lines.index("  siang_feed: published")
    assert "Failures (24 h): none" in body
    assert "instagram: expires 2027-01-24" in body
    assert "Quota (24 h):\n  Instagram 4/50\n  Threads 12/250 posts, 30/1000 replies" in body


def test_healthcheck_fallback(make_app: Callable[..., App]) -> None:
    app = make_app(quota_meta(), publish_live=False, kill_switch=True)
    add_plan(app, "siang_feed", "fallback")
    assert ops.run_healthcheck(app) == {"verdict": "FALLBACK"}
    subject, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert subject.startswith("Daily FALLBACK")
    assert "Mode: DRY RUN" in body and "kill switch ON" in body


@pytest.mark.parametrize("cause", ["plan", "run"])
def test_healthcheck_fail(make_app: Callable[..., App], cause: str) -> None:
    app = make_app(quota_meta())
    add_plan(app, "siang_feed", "fallback")
    if cause == "plan":
        add_plan(app, "malam_story", "failed")
    else:
        with session_scope(app.db) as s:
            s.add(JobRun(job="produce", started_at=NOW - timedelta(hours=1), status="failed", error="RuntimeError: boom"))
    assert ops.run_healthcheck(app) == {"verdict": "FAIL"}
    subject, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert subject.startswith("Daily FAIL")
    if cause == "run":
        assert "Failures (24 h):\n  produce: RuntimeError: boom" in body


def test_healthcheck_quota_failure_is_reported(make_app: Callable[..., App]) -> None:
    meta = FakeMeta()
    meta.on("GET", f"/v23.0/{IG_USER}/content_publishing_limit", error(500, 2, "service down"))
    app = make_app(meta)
    assert ops.run_healthcheck(app) == {"verdict": "OK"}
    _, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert "Quota (24 h):\n  quota check failed: Meta API GET" in body
    assert "service down" in body


def test_healthcheck_without_accounts_or_tokens(make_app: Callable[..., App]) -> None:
    app = make_app(FakeMeta(), ig_user_id="", threads_user_id="")
    assert ops.run_healthcheck(app) == {"verdict": "OK"}
    _, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert body.endswith("Quota (24 h):")


def test_healthcheck_missing_token_quota(make_app: Callable[..., App]) -> None:
    app = make_app(FakeMeta(), ig_access_token="")
    ops.run_healthcheck(app)
    _, body = app.mailer.sent[-1]  # type: ignore[attr-defined]
    assert "quota check failed: No instagram token" in body
