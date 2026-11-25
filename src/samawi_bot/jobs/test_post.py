"""`test-post` (spec §11 Phase 0): prove publishing works end to end from deployed code.

Publishes one Instagram story and one Threads post, both built only from verified sources and
dated today: the ayat of the day (Tanzil Arabic + Kemenag translation, verbatim) and today's
Jakarta prayer times. A story expires after 24 hours, so a test leaves nothing permanent on the
feed. It is a deliberate manual action: it runs whatever PUBLISH_LIVE says, but still stops for
the kill switch, and posts at most once per day per platform.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from ..app import App
from ..db import session_scope
from ..db.models import Post
from ..kill_switch import ensure_off
from ..publisher.slots import WIB
from ..render.compose import ayat_of_day, story_subuh
from ..sources.hijri import islamic_date
from ..sources.quran import parse_reference
from ..verify.facts import prayer_times_today
from .content import render_story
from .tokens import access_token


def ayat_text_post(app: App, ref: str) -> str:
    rng = parse_reference(ref, app.sources.quran)
    text = " ".join(app.sources.quran.translation(r) for r in rng.refs)
    post = f"Ayat hari ini:\n\n“{text}” {app.sources.quran.citation(rng)}"
    return post if len(post) <= 500 else f"Ayat hari ini: {app.sources.quran.citation(rng)}. Buka mushafmu dan baca pelan-pelan."


def _already(app: App, key: str) -> Post | None:
    with session_scope(app.db) as s:
        row = s.scalar(select(Post).where(Post.idempotency_key == key, Post.status == "published"))
        if row is not None:
            s.expunge(row)
        return row


def _record(app: App, key: str, platform: str, kind: str, media_id: str, permalink: str | None) -> None:
    with session_scope(app.db) as s:
        s.add(
            Post(
                idempotency_key=key,
                platform=platform,
                media_type=kind,
                platform_media_id=media_id,
                permalink=permalink,
                published_at=app.now(),
                status="published",
                slot="test",
            )
        )


def run_test_post(app: App) -> dict[str, Any]:
    with session_scope(app.db) as s:
        ensure_off(s, app.settings.kill_switch)
    day = app.now().astimezone(WIB).date()
    ref = ayat_of_day(day.toordinal())
    out: dict[str, Any] = {"day": str(day), "ayat": ref}

    ig_key = f"test:{day}:instagram"
    if (done := _already(app, ig_key)) is not None:
        out["instagram"] = {"status": "already posted today", "id": done.platform_media_id}
    else:
        h = islamic_date(day)
        spec = story_subuh(app.sources, ref, f"{h.day} {h.month_name} {h.year} H", prayer_times_today(app.sources, "Jakarta", day))
        key = render_story(app, spec, "isya", f"posts/{day}/test_story.png")
        pub = app.instagram(access_token(app, "instagram")).story(app.storage.url(key))
        _record(app, ig_key, "instagram", "story", pub.media_id, pub.permalink)
        out["instagram"] = {"status": "published", "id": pub.media_id, "permalink": pub.permalink}

    th_key = f"test:{day}:threads"
    if (done := _already(app, th_key)) is not None:
        out["threads"] = {"status": "already posted today", "id": done.platform_media_id}
    else:
        th = app.threads(access_token(app, "threads"))
        post_id = th.text(ayat_text_post(app, ref))
        link = th.permalink(post_id)
        _record(app, th_key, "threads", "text", post_id, link)
        out["threads"] = {"status": "published", "id": post_id, "permalink": link}
    return out
