"""Meta token storage and weekly refresh (spec §8.1 token-refresh; alerts ≥ 7 days before expiry)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ..app import App
from ..db import session_scope
from ..db.models import Token
from ..publisher.meta import Graph, MetaApiError
from ..timeutil import utc

ALERT_DAYS = 7
REFRESH_AFTER = timedelta(days=6)
THREADS_MIN_AGE = timedelta(hours=24)
LONG_LIVED = timedelta(days=60)
NEVER = timedelta(days=3650)


class TokenMissing(RuntimeError):
    pass


class TokenExpired(RuntimeError):
    pass


def _env_token(app: App, platform: str) -> str:
    return app.settings.ig_access_token if platform == "instagram" else app.settings.threads_access_token


def ensure(app: App, s: Session, platform: str) -> Token | None:
    row = s.get(Token, platform)
    env = _env_token(app, platform)
    if row is None and env:
        row = Token(platform=platform, access_token=env, issued_at=app.now(), expires_at=app.now() + LONG_LIVED)
        s.add(row)
        s.flush()
    elif row is not None and env and env != row.access_token and _newer_env(row, app):
        # A token re-issued by hand (store-meta-tokens) replaces the stored one.
        row.access_token, row.issued_at, row.expires_at, row.last_error = env, app.now(), app.now() + LONG_LIVED, None
    return row


def _newer_env(row: Token, app: App) -> bool:
    # The env value only wins when the stored token has failed or expired; otherwise the stored
    # (refreshed) token is newer than whatever the secret still holds.
    return bool(row.last_error) or _aware(row.expires_at) <= app.now()


def _aware(t: datetime) -> datetime:
    return utc(t)


def access_token(app: App, platform: str) -> str:
    with session_scope(app.db) as s:
        row = ensure(app, s, platform)
        if row is None:
            raise TokenMissing(f"No {platform} token. Run scripts/store_meta_tokens.py (docs/META-SETUP.md).")
        if _aware(row.expires_at) <= app.now():
            raise TokenExpired(f"{platform} token expired at {row.expires_at:%Y-%m-%d}; re-authorise (docs/META-SETUP.md)")
        return row.access_token


@dataclass
class TokenReport:
    platform: str
    status: str  # refreshed|fresh|missing|failed|too_young
    days_left: int | None
    error: str | None = None


def _exchange(app: App, platform: str, token: str) -> dict[str, Any]:
    if platform == "threads":
        base = app.settings.threads_base_url.rsplit("/v", 1)[0]
        return Graph(app.http, base, token, sleep=app.sleep).get("/refresh_access_token", grant_type="th_refresh_token")
    if not (app.settings.meta_app_id and app.settings.meta_app_secret):
        raise MetaApiError(0, None, "META_APP_ID and META_APP_SECRET are needed to refresh the Instagram token")
    return Graph(app.http, app.settings.graph_base_url, token, sleep=app.sleep).get(
        "/oauth/access_token",
        grant_type="fb_exchange_token",
        client_id=app.settings.meta_app_id,
        client_secret=app.settings.meta_app_secret,
        fb_exchange_token=token,
    )


def refresh(app: App, platform: str, force: bool = False) -> TokenReport:
    with session_scope(app.db) as s:
        row = ensure(app, s, platform)
        if row is None:
            return TokenReport(platform, "missing", None)
        now = app.now()
        days_left = (_aware(row.expires_at) - now).days
        age = now - _aware(row.issued_at)
        if not force and age < REFRESH_AFTER and days_left >= 14:
            return TokenReport(platform, "fresh", days_left)
        if platform == "threads" and age < THREADS_MIN_AGE:
            return TokenReport(platform, "too_young", days_left)
        try:
            res = _exchange(app, platform, row.access_token)
        except MetaApiError as exc:
            row.last_error = str(exc)
            return TokenReport(platform, "failed", days_left, str(exc))
        expires = now + (timedelta(seconds=int(res["expires_in"])) if res.get("expires_in") else NEVER)
        row.access_token, row.issued_at, row.expires_at, row.last_error = res["access_token"], now, expires, None
        return TokenReport(platform, "refreshed", (expires - now).days)


def problems(reports: list[TokenReport]) -> list[str]:
    out = []
    for r in reports:
        if r.status in ("failed", "missing"):
            out.append(f"{r.platform} token {r.status}{': ' + r.error if r.error else ''}")
        if r.days_left is not None and r.days_left <= ALERT_DAYS:
            out.append(f"{r.platform} token expires in {max(0, r.days_left)} days. Re-authorise (docs/META-SETUP.md).")
    return out
