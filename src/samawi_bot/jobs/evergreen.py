"""The evergreen bank (spec §8.3): ≥ 30 verified posts with nothing time-sensitive, ≥ 14 in reserve.

Bank posts go through exactly the same writer → checks → judge gate as daily posts, with the extra
evergreen rules (no dated facts, no series, no "hari ini").
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from sqlalchemy import select

from ..alerts import safe_send
from ..app import App
from ..brain.models import Draft, Pillar
from ..brain.planner import BRIDGES, choose_bridges
from ..brain.writer import Brief, write_verified
from ..checks.rules import topic_hash
from ..db import session_scope
from ..db.models import EvergreenPost, TopicHistory
from ..publisher.slots import WIB
from .content import check_context

EVERGREEN_PILLARS = [p.value for p in Pillar if p is not Pillar.KALENDER]
COOLDOWN = timedelta(days=60)


class EvergreenEmpty(RuntimeError):
    pass


def top_up(app: App, count: int, pillars: list[str] | None = None) -> dict[str, Any]:
    pillars = pillars or EVERGREEN_PILLARS
    today = app.now().astimezone(WIB).date()
    added, failed = 0, 0
    reports: list[str] = []
    llm = app.llm_factory()
    for i in range(count):
        pillar = pillars[i % len(pillars)]
        with session_scope(app.db) as s:
            ctx = check_context(app, s, today, evergreen=True)
            existing = {r.topic_hash: today for r in s.scalars(select(EvergreenPost))}
        ctx.topic_history.update(existing)  # never two bank posts on the same topic
        brief = Brief(
            day=today,
            slot="evergreen",
            pillar=pillar,
            format="carousel",
            hook_formula="ternyata",
            bridge_options=choose_bridges(app.sources.features, pillar, [], 1, 1) or BRIDGES[pillar][:2],
            sky_moment="dzuhur",
            seasonal=(
                "EVERGREEN: post cadangan yang bisa terbit kapan saja. Jangan sebut tanggal, hari ini, pekan ini, "
                "waktu salat tanggal tertentu, atau seri. `next_teaser` umum: 'Besok: satu fakta lagi yang jarang "
                "dibahas. Follow @samawi.indonesia biar nggak ketinggalan.'"
            ),
        )
        attempts = write_verified(llm, app.sources, brief, ctx)
        ok = next((a for a in attempts if a.passed), None)
        if ok is None or ok.draft is None or ok.gate is None or ok.gate.hook_index is None:
            failed += 1
            reports.append(f"{pillar}: " + attempts[-1].notes()[:600])
            continue
        with session_scope(app.db) as s:
            s.add(
                EvergreenPost(
                    json=ok.draft.model_dump(mode="json"),
                    rendered_media_keys=[],
                    pillar=pillar,
                    topic_hash=topic_hash(ok.draft.topic),
                    hook_index=ok.gate.hook_index,
                    created_at=app.now(),
                )
            )
        added += 1
    if failed:
        safe_send(app.mailer, f"Evergreen top-up: {added} added, {failed} failed", "\n\n".join(reports))
    return {"added": added, "failed": failed}


def take(app: App, day: date) -> EvergreenPost:
    """The least recently used bank post whose topic is not within its 60-day cooldown."""
    with session_scope(app.db) as s:
        recent = {r.topic_hash for r in s.scalars(select(TopicHistory).where(TopicHistory.used_at > day - COOLDOWN))}
        candidates = s.scalars(select(EvergreenPost).where(EvergreenPost.retired.is_(False))).all()
        usable = [c for c in candidates if c.topic_hash not in recent]
        if not usable:
            raise EvergreenEmpty("No evergreen post is available outside its 60-day cooldown")
        usable.sort(key=lambda c: (c.last_used_at is not None, c.last_used_at or c.created_at, c.id))
        item = usable[0]
        item.last_used_at = app.now()
        s.flush()
        s.expunge(item)
        Draft.model_validate(item.json)  # a bank row that no longer parses must not be used
        return item
