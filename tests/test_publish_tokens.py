"""Meta token storage and refresh (spec §8.1 token-refresh; §10.3 expired / revoked / 6-7-8 days)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from conftest import NOW, Clock
from publish_fakes import FakeMeta, check_cassette, error
from samawi_bot.app import App
from samawi_bot.db import session_scope
from samawi_bot.db.models import Token
from samawi_bot.jobs.tokens import (
    LONG_LIVED,
    NEVER,
    TokenExpired,
    TokenMissing,
    TokenReport,
    access_token,
    ensure,
    problems,
    refresh,
)
from samawi_bot.timeutil import utc


def _row(app: App, platform: str) -> Token | None:
    with session_scope(app.db) as s:
        row = s.get(Token, platform)
        if row is not None:
            s.expunge(row)
        return row


def _store(app: App, platform: str, token: str, issued_ago: timedelta, expires_in: timedelta, last_error: str | None = None) -> None:
    with session_scope(app.db) as s:
        s.merge(Token(platform=platform, access_token=token, issued_at=NOW - issued_ago, expires_at=NOW + expires_in, last_error=last_error))


@pytest.fixture
def meta() -> FakeMeta:
    return FakeMeta()


# ---- seeding and access ------------------------------------------------------------------------


@pytest.mark.parametrize("platform,env", [("instagram", "IG-TOKEN"), ("threads", "TH-TOKEN")])
def test_env_seeds_token(make_app: Callable[..., App], platform: str, env: str) -> None:
    app = make_app()
    assert access_token(app, platform) == env
    row = _row(app, platform)
    assert row is not None
    assert utc(row.issued_at) == NOW and utc(row.expires_at) == NOW + LONG_LIVED and row.last_error is None


def test_missing_token_raises(make_app: Callable[..., App]) -> None:
    app = make_app(ig_access_token="")
    with pytest.raises(TokenMissing, match="No instagram token"):
        access_token(app, "instagram")
    assert _row(app, "instagram") is None


def test_expired_token_raises(make_app: Callable[..., App], clock: Clock) -> None:
    app = make_app()
    access_token(app, "threads")
    clock.now = NOW + LONG_LIVED
    with pytest.raises(TokenExpired, match="threads token expired"):
        access_token(app, "threads")


def test_stored_token_used_when_no_env(make_app: Callable[..., App]) -> None:
    app = make_app(threads_access_token="")
    _store(app, "threads", "STORED", timedelta(days=1), timedelta(days=50))
    assert access_token(app, "threads") == "STORED"


def test_env_does_not_replace_a_healthy_stored_token(make_app: Callable[..., App]) -> None:
    app = make_app(ig_access_token="ENV-OLD")
    _store(app, "instagram", "REFRESHED", timedelta(days=2), timedelta(days=58))
    assert access_token(app, "instagram") == "REFRESHED"


def test_env_replaces_token_after_failure(make_app: Callable[..., App]) -> None:
    app = make_app(ig_access_token="ENV-NEW")
    _store(app, "instagram", "BROKEN", timedelta(days=20), timedelta(days=40), last_error="190 revoked")
    assert access_token(app, "instagram") == "ENV-NEW"
    row = _row(app, "instagram")
    assert row is not None and row.last_error is None and utc(row.issued_at) == NOW and utc(row.expires_at) == NOW + LONG_LIVED


def test_env_replaces_expired_token(make_app: Callable[..., App]) -> None:
    app = make_app(threads_access_token="ENV-NEW")
    _store(app, "threads", "OLD", timedelta(days=61), timedelta(days=-1))
    assert access_token(app, "threads") == "ENV-NEW"


def test_env_same_as_stored_is_a_no_op(make_app: Callable[..., App]) -> None:
    app = make_app()
    _store(app, "instagram", "IG-TOKEN", timedelta(days=3), timedelta(days=-1), last_error="x")
    with pytest.raises(TokenExpired):
        access_token(app, "instagram")


def test_ensure_without_row_or_env(make_app: Callable[..., App]) -> None:
    app = make_app(threads_access_token="")
    with session_scope(app.db) as s:
        assert ensure(app, s, "threads") is None


# ---- refresh ----------------------------------------------------------------------------------


def test_refresh_fresh_makes_no_call(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta)
    r = refresh(app, "instagram")
    assert r == TokenReport("instagram", "fresh", 60)
    assert meta.requests == []


def test_refresh_missing(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta, ig_access_token="")
    assert refresh(app, "instagram") == TokenReport("instagram", "missing", None)


def test_refresh_forced_instagram_contract(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta)
    r = refresh(app, "instagram", force=True)
    assert r.status == "refreshed" and r.days_left == 60
    check_cassette("ig_token_exchange", meta.requests)
    row = _row(app, "instagram")
    assert row is not None and row.access_token == "IG-NEW" and utc(row.expires_at) == NOW + timedelta(seconds=5184000)
    assert access_token(app, "instagram") == "IG-NEW"  # the env value no longer wins


def test_refresh_due_by_age(make_app: Callable[..., App], meta: FakeMeta, clock: Clock) -> None:
    app = make_app(handler=meta)
    access_token(app, "threads")
    clock.now = NOW + timedelta(days=6)
    r = refresh(app, "threads")
    assert r.status == "refreshed"
    check_cassette("threads_token_refresh", meta.requests)
    row = _row(app, "threads")
    assert row is not None and row.access_token == "TH-NEW" and utc(row.issued_at) == clock.now


def test_refresh_not_due_just_under_six_days(make_app: Callable[..., App], meta: FakeMeta, clock: Clock) -> None:
    app = make_app(handler=meta)
    access_token(app, "threads")
    clock.now = NOW + timedelta(days=6) - timedelta(seconds=1)
    assert refresh(app, "threads").status == "fresh"
    assert meta.requests == []


def test_refresh_due_by_expiry_even_if_young(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta)
    _store(app, "instagram", "IG-TOKEN", timedelta(days=2), timedelta(days=13))
    assert refresh(app, "instagram").status == "refreshed"


def test_threads_too_young_to_refresh(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta)
    _store(app, "threads", "TH-TOKEN", timedelta(hours=23), timedelta(days=10))
    r = refresh(app, "threads", force=True)
    assert r == TokenReport("threads", "too_young", 10)
    assert meta.requests == []


def test_threads_exactly_24h_can_refresh(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta)
    _store(app, "threads", "TH-TOKEN", timedelta(hours=24), timedelta(days=10))
    assert refresh(app, "threads", force=True).status == "refreshed"


def test_instagram_young_token_can_refresh(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta)
    _store(app, "instagram", "IG-TOKEN", timedelta(hours=1), timedelta(days=10))
    assert refresh(app, "instagram").status == "refreshed"


def test_refresh_without_expires_in_never_expires(make_app: Callable[..., App], meta: FakeMeta) -> None:
    meta.refresh_response = {"access_token": "IG-NEW", "token_type": "bearer"}
    app = make_app(handler=meta)
    r = refresh(app, "instagram", force=True)
    assert r.days_left == NEVER.days


def test_refresh_failure_records_last_error(make_app: Callable[..., App], meta: FakeMeta) -> None:
    meta.fail("GET", "/refresh_access_token", error(400, 190, "Error validating access token: The session has been invalidated", subcode=460))
    app = make_app(handler=meta)
    _store(app, "threads", "TH-TOKEN", timedelta(days=7), timedelta(days=53))
    r = refresh(app, "threads")
    assert r.status == "failed" and r.days_left == 53
    assert r.error is not None and "invalidated" in r.error
    row = _row(app, "threads")
    assert row is not None and row.access_token == "TH-TOKEN" and row.last_error == r.error
    assert len(meta.requests) == 1  # a token error is not retried


def test_refresh_transient_failure_retries_then_fails(make_app: Callable[..., App], meta: FakeMeta) -> None:
    meta.fail("GET", "/oauth/access_token", error(500, 2, "Service temporarily unavailable"), times=3)
    slept: list[float] = []
    app = make_app(handler=meta)
    app.sleep = slept.append
    r = refresh(app, "instagram", force=True)
    assert r.status == "failed" and slept == [2.0, 4.0]


def test_refresh_failure_then_env_reissue_wins(make_app: Callable[..., App], meta: FakeMeta) -> None:
    meta.fail("GET", "/oauth/access_token", error(400, 190, "revoked"))
    app = make_app(handler=meta)
    access_token(app, "instagram")
    assert refresh(app, "instagram", force=True).status == "failed"
    app.settings.ig_access_token = "IG-NEW"  # owner re-ran store-meta-tokens
    assert access_token(app, "instagram") == "IG-NEW"


@pytest.mark.parametrize("missing", ["meta_app_id", "meta_app_secret"])
def test_refresh_instagram_needs_app_credentials(make_app: Callable[..., App], meta: FakeMeta, missing: str) -> None:
    app = make_app(handler=meta, **{missing: ""})
    r = refresh(app, "instagram", force=True)
    assert r.status == "failed" and r.error is not None and "META_APP_ID and META_APP_SECRET" in r.error
    assert meta.requests == []
    row = _row(app, "instagram")
    assert row is not None and row.last_error == r.error


def test_threads_refresh_needs_no_app_credentials(make_app: Callable[..., App], meta: FakeMeta) -> None:
    app = make_app(handler=meta, meta_app_id="", meta_app_secret="")
    _store(app, "threads", "TH-TOKEN", timedelta(days=7), timedelta(days=53))
    assert refresh(app, "threads").status == "refreshed"


# ---- problems ---------------------------------------------------------------------------------


@pytest.mark.parametrize("days,alert", [(6, True), (7, True), (8, False), (0, True), (60, False)])
def test_problems_expiry_window(days: int, alert: bool) -> None:
    out = problems([TokenReport("threads", "fresh", days)])
    if alert:
        assert out == [f"threads token expires in {days} days. Re-authorise (docs/META-SETUP.md)."]
    else:
        assert out == []


def test_problems_negative_days_shown_as_zero() -> None:
    assert problems([TokenReport("instagram", "refreshed", -3)]) == ["instagram token expires in 0 days. Re-authorise (docs/META-SETUP.md)."]


def test_problems_failed_and_missing() -> None:
    out = problems(
        [TokenReport("instagram", "failed", 30, "Meta API GET /oauth: revoked"), TokenReport("threads", "missing", None), TokenReport("x", "too_young", 20)]
    )
    assert out == ["instagram token failed: Meta API GET /oauth: revoked", "threads token missing"]


def test_problems_failed_and_expiring_both_reported() -> None:
    out = problems([TokenReport("threads", "failed", 5, "boom")])
    assert out == ["threads token failed: boom", "threads token expires in 5 days. Re-authorise (docs/META-SETUP.md)."]


def test_problems_from_real_refresh_at_6_7_8_days(make_app: Callable[..., App], meta: FakeMeta) -> None:
    meta.fail("GET", "/refresh_access_token", error(400, 190, "revoked"), times=3)
    app = make_app(handler=meta)
    out = []
    for d in (6, 7, 8):
        _store(app, "threads", "TH-TOKEN", timedelta(days=50), timedelta(days=d, hours=1))
        out.append(problems([refresh(app, "threads")]))
    assert all(o[0] == "threads token failed: Meta API GET /refresh_access_token: revoked" for o in out)
    assert [len(o) for o in out] == [2, 2, 1]
