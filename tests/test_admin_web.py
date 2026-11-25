"""The owner-only dashboard (spec §8): email sign-in, read-only pages, kill switch, media."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import itsdangerous.timed
import pytest
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from conftest import NOW
from samawi_bot.admin.web import LINK_TTL, SESSION, SESSION_TTL, create_admin
from samawi_bot.app import App
from samawi_bot.db import session_scope
from samawi_bot.db.models import ContentPlan, DraftRow, EvergreenPost, Flag, JobRun, Metric, Post, Token

OWNER = "owner@example.com"
BASE = "/samawi"


def signer(secret: str = "test-secret") -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret, salt="samawi-admin")


@pytest.fixture
def app(make_app: Callable[..., App]) -> App:
    return make_app()


def client(app: App) -> TestClient:
    return TestClient(create_admin(app), follow_redirects=False)


def signed_in(app: App) -> TestClient:
    c = client(app)
    c.cookies.set(SESSION, signer().dumps(OWNER))
    return c


def later(monkeypatch: pytest.MonkeyPatch, seconds: int) -> None:
    real = time.time
    monkeypatch.setattr(itsdangerous.timed, "time", SimpleNamespace(time=lambda: real() + seconds))


# ── sign-in ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/", "/queue", "/posts", "/evergreen", "/media/posts/a.png"])
def test_unauthenticated_redirects_to_login(app: App, path: str) -> None:
    r = client(app).get(f"{BASE}{path}")
    assert r.status_code == 303
    assert r.headers["location"] == f"{BASE}/login"


def test_kill_requires_auth(app: App) -> None:
    r = client(app).post(f"{BASE}/kill", data={"on": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login"
    with session_scope(app.db) as s:
        assert s.get(Flag, "kill_switch") is None


def test_login_page(app: App) -> None:
    c = client(app)
    r = c.get(f"{BASE}/login")
    assert r.status_code == 200
    assert "Email me a sign-in link" in r.text
    assert 'action="/samawi/login"' in r.text
    assert "<input" not in r.text  # nothing to type: no address field
    r = c.get(f"{BASE}/login?sent=1")
    assert "a sign-in link is on its way" in r.text


def test_post_login_emails_link_to_alert_email_only(app: App) -> None:
    r = client(app).post(f"{BASE}/login", data={"email": "attacker@example.com"})
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login?sent=1"
    sent = app.mailer.sent  # type: ignore[attr-defined]
    assert len(sent) == 1
    subject, body = sent[0]
    assert subject == "Dashboard sign-in link"
    assert "attacker" not in body
    assert body.startswith("http://localhost:3200/samawi/auth?token=")
    token = body.split("token=", 1)[1].split()[0]
    assert signer().loads(token, max_age=LINK_TTL) == f"link:{OWNER}"


def test_post_login_without_owner_sends_nothing(make_app: Callable[..., App]) -> None:
    app = make_app(alert_email="")
    r = client(app).post(f"{BASE}/login")
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login?sent=1"
    assert app.mailer.sent == []  # type: ignore[attr-defined]


def test_auth_with_valid_link_sets_session(app: App) -> None:
    c = client(app)
    r = c.get(f"{BASE}/auth", params={"token": signer().dumps(f"link:{OWNER}")})
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/"
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{SESSION}=")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and f"Max-Age={SESSION_TTL}" in cookie
    assert "Secure" not in cookie  # localhost dashboard
    assert signer().loads(c.cookies[SESSION], max_age=SESSION_TTL) == OWNER
    assert c.get(f"{BASE}/").status_code == 200


def test_auth_cookie_is_secure_off_localhost(make_app: Callable[..., App]) -> None:
    app = make_app(dashboard_url="https://admin.example.com/samawi")
    r = client(app).get(f"{BASE}/auth", params={"token": signer().dumps(f"link:{OWNER}")})
    assert "Secure" in r.headers["set-cookie"]


def test_auth_with_expired_link(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    token = signer().dumps(f"link:{OWNER}")
    later(monkeypatch, LINK_TTL + 5)
    r = client(app).get(f"{BASE}/auth", params={"token": token})
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login"
    assert "set-cookie" not in r.headers


@pytest.mark.parametrize(
    "token",
    [
        signer().dumps(f"link:{OWNER}")[:-2] + "xx",  # tampered signature
        signer("other-secret").dumps(f"link:{OWNER}"),  # signed with another key
        URLSafeTimedSerializer("test-secret", salt="other").dumps(f"link:{OWNER}"),
        signer().dumps("link:attacker@example.com"),  # wrong subject
        signer().dumps(OWNER),  # a session token is not a sign-in link
        "garbage",
    ],
)
def test_auth_rejects_bad_links(app: App, token: str) -> None:
    r = client(app).get(f"{BASE}/auth", params={"token": token})
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login"
    assert "set-cookie" not in r.headers


def test_auth_rejects_empty_owner_link(make_app: Callable[..., App]) -> None:
    app = make_app(alert_email="")
    r = client(app).get(f"{BASE}/auth", params={"token": signer().dumps("link:")})
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login"


@pytest.mark.parametrize("cookie", [signer().dumps("attacker@example.com"), signer("x").dumps(OWNER), "nope"])
def test_bad_session_cookie_redirects(app: App, cookie: str) -> None:
    c = client(app)
    c.cookies.set(SESSION, cookie)
    r = c.get(f"{BASE}/")
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/login"


def test_expired_session_redirects(app: App, monkeypatch: pytest.MonkeyPatch) -> None:
    c = signed_in(app)
    later(monkeypatch, SESSION_TTL + 5)
    assert c.get(f"{BASE}/").status_code == 303


# ── pages ────────────────────────────────────────────────────────────────────────────────────


def seed(app: App) -> dict[str, int]:
    today = NOW.date() + timedelta(days=1)  # 26 Nov in WIB
    with session_scope(app.db) as s:
        s.add(JobRun(job="produce", started_at=NOW - timedelta(hours=1), finished_at=NOW, status="ok", summary={"made": 3}))
        s.add(JobRun(job="publish", started_at=NOW - timedelta(hours=2), status="failed", summary={}, error="MetaApiError: boom"))
        s.add(Token(platform="threads", access_token="t", issued_at=NOW, expires_at=NOW + timedelta(days=50), last_error="token revoked"))
        feed = ContentPlan(
            date=today,
            slot="siang_feed",
            pillar="fakta_quran",
            format="carousel",
            hook_formula="ternyata",
            series_part=2,
            series_total=5,
            bridge_options=["tafsir", "khatamPlanner"],
            scheduled_at=NOW + timedelta(hours=6),
            status="ready",
        )
        story = ContentPlan(date=today, slot="subuh_story", scheduled_at=NOW, status="failed", bridge_options=[])
        threads = ContentPlan(
            date=today + timedelta(days=2),
            slot="siang_threads",
            scheduled_at=None,
            status="planned",
            bridge_options=[],
            pillar="doa_dzikir",
            bridge_feature="dailyDhikr",
        )
        far = ContentPlan(date=today + timedelta(days=9), slot="siang_feed", status="planned", bridge_options=[])
        s.add_all([feed, story, threads, far])
        s.flush()
        s.add(DraftRow(plan_id=feed.id, json={"media": ["posts/x/1.png", "posts/x/reel.mp4"], "caption": "Caption feed"}, status="payload", created_at=NOW))
        s.add(DraftRow(plan_id=feed.id, json={}, status="passed", attempt=1, created_at=NOW))
        s.add(DraftRow(plan_id=story.id, json={}, status="failed", attempt=2, check_report={"errors": ["hadith 4191 rejected"]}, created_at=NOW))
        s.add(DraftRow(plan_id=threads.id, json={"posts": ["Post satu", "Post dua"]}, status="payload", created_at=NOW))
        p1 = Post(
            idempotency_key="a", platform="instagram", media_type="carousel", status="published", hook_text="Hook IG", permalink="https://instagram.com/p/1"
        )
        p2 = Post(idempotency_key="b", platform="threads", media_type="chain", status="failed", error="Meta down")
        s.add_all([p1, p2])
        s.flush()
        s.add(Metric(post_id=p1.id, captured_at=NOW, age_hours=6, reach=10))
        s.add(Metric(post_id=p1.id, captured_at=NOW, age_hours=24, reach=4321, saves=77, shares=55, follows=9))
        s.add(
            EvergreenPost(
                json={"hooks": ["Hook nol", "Hook satu"], "key_fact": "Fakta evergreen"}, pillar="sejarah_kisah", topic_hash="abc", hook_index=1, created_at=NOW
            )
        )
        s.add(EvergreenPost(json={"hooks": ["H"], "key_fact": "x"}, pillar="doa_dzikir", topic_hash="def", created_at=NOW, retired=True))
        return {"feed": feed.id, "story": story.id}


def test_status_page(app: App) -> None:
    seed(app)
    r = signed_in(app).get(f"{BASE}/")
    assert r.status_code == 200
    t = r.text
    assert "Kill switch: <span" in t and "off" in t and "Pause all publishing" in t
    assert 'name="on" value="1"' in t
    assert "LIVE" in t and "prelaunch" in t
    assert "siang_feed" in t and "subuh_story" in t
    assert "siang_threads" not in t  # not today
    assert "token revoked" in t
    assert "Evergreen reserve: 1" in t
    assert "MetaApiError: boom" in t and '{"made": 3}' in t
    assert "KILL_SWITCH is set" not in t


def test_status_page_empty_and_env_kill(make_app: Callable[..., App]) -> None:
    app = make_app(kill_switch=True, publish_live=False)
    t = signed_in(app).get(f"{BASE}/").text
    assert "ON — nothing publishes" in t and "Resume publishing" in t
    assert "KILL_SWITCH is set in the task environment" in t
    assert "Nothing planned today." in t and "No tokens stored yet." in t
    assert "DRY RUN" in t
    assert "Evergreen reserve: 0" in t


def test_queue_page(app: App) -> None:
    seed(app)
    t = signed_in(app).get(f"{BASE}/queue").text
    assert "Queue — next 7 days" in t
    assert "fakta_quran · carousel · hook ternyata · 2/5 · bridge tafsir, khatamPlanner" in t
    assert "bridge dailyDhikr" in t
    assert '<img src="/samawi/media/posts/x/1.png"' in t
    assert '<video src="/samawi/media/posts/x/reel.mp4"' in t
    assert "Caption feed" in t
    assert "— Post satu" in t and "— Post dua" in t
    assert 'Draft attempt 1: <span class="s-ok">passed</span>' in t
    assert 'Draft attempt 2: <span class="s-failed">failed</span>' in t
    assert "hadith 4191 rejected" in t
    assert t.count('class="card"') == 3  # the plan 9 days out is not shown


def test_queue_empty(app: App) -> None:
    assert "Nothing planned." in signed_in(app).get(f"{BASE}/queue").text


def test_posts_page(app: App) -> None:
    seed(app)
    t = signed_in(app).get(f"{BASE}/posts").text
    assert "Hook IG" in t and "4321" in t and "77" in t and "Meta down" in t
    assert 'href="https://instagram.com/p/1"' in t
    assert ">10<" not in t  # the latest (24 h) metric is shown, not the 6 h one
    assert "No posts yet." in signed_in(make_empty(app)).get(f"{BASE}/posts").text


def make_empty(app: App) -> App:
    with session_scope(app.db) as s:
        s.query(Metric).delete()
        s.query(Post).delete()
    return app


def test_evergreen_page(app: App) -> None:
    seed(app)
    t = signed_in(app).get(f"{BASE}/evergreen").text
    assert "Evergreen bank (2)" in t
    assert "Hook satu" in t and "Hook nol" not in t
    assert "Fakta evergreen" in t


def test_evergreen_empty(app: App) -> None:
    assert "The bank is empty." in signed_in(app).get(f"{BASE}/evergreen").text


# ── kill switch ──────────────────────────────────────────────────────────────────────────────


def test_kill_switch_toggle(app: App) -> None:
    c = signed_in(app)
    r = c.post(f"{BASE}/kill", data={"on": "1"})
    assert r.status_code == 303 and r.headers["location"] == f"{BASE}/"
    with session_scope(app.db) as s:
        f = s.get(Flag, "kill_switch")
        assert f is not None and f.value == "on" and f.updated_by == OWNER
    assert app.mailer.sent[-1] == ("Kill switch ON", f"Set from the dashboard by {OWNER}.")  # type: ignore[attr-defined]
    assert "ON — nothing publishes" in c.get(f"{BASE}/").text

    c.post(f"{BASE}/kill", data={"on": "0"})
    with session_scope(app.db) as s:
        f = s.get(Flag, "kill_switch")
        assert f is not None and f.value == "off"
    assert app.mailer.sent[-1][0] == "Kill switch off"  # type: ignore[attr-defined]


def test_kill_switch_needs_value(app: App) -> None:
    assert signed_in(app).post(f"{BASE}/kill", data={}).status_code == 422


# ── media ────────────────────────────────────────────────────────────────────────────────────


def test_media(app: App) -> None:
    app.storage.put("posts/x/1.png", b"\x89PNG fake")
    app.storage.put("posts/x/reel.mp4", b"mp4 bytes")
    app.storage.put("posts/..secret.png", b"secret")
    app.storage.put("secrets/key.png", b"secret")
    c = signed_in(app)
    r = c.get(f"{BASE}/media/posts/x/1.png")
    assert r.status_code == 200 and r.content == b"\x89PNG fake" and r.headers["content-type"] == "image/png"
    r = c.get(f"{BASE}/media/posts/x/reel.mp4")
    assert r.status_code == 200 and r.content == b"mp4 bytes" and r.headers["content-type"] == "video/mp4"
    assert c.get(f"{BASE}/media/posts/..secret.png").status_code == 404
    assert c.get(f"{BASE}/media/posts/..%2F..%2Fsecrets/key.png").status_code == 404
    assert c.get(f"{BASE}/media/secrets/key.png").status_code == 404
    assert c.get(f"{BASE}/media/posts/missing.png").status_code == 404


def test_healthz_needs_no_auth(app: App) -> None:
    r = client(app).get(f"{BASE}/healthz")
    assert r.status_code == 200 and r.json() == {"ok": "yes"}


def test_no_docs_routes(app: App) -> None:
    c = client(app)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert c.get(path).status_code == 404


def test_custom_base_path(make_app: Callable[..., App]) -> None:
    app: Any = make_app(admin_base_path="/admin/")
    c = TestClient(create_admin(app), follow_redirects=False)
    assert c.get("/admin/healthz").status_code == 200
    assert c.get("/admin/").headers["location"] == "/admin/login"
