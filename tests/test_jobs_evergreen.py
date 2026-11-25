"""The evergreen bank (spec §8.3): verified top-ups, LRU selection, 60-day topic cooldown."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from conftest import NOW, TODAY, Clock, FakeLLM, judge_report, load_draft
from samawi_bot.app import App
from samawi_bot.brain.models import Draft
from samawi_bot.checks.rules import topic_hash
from samawi_bot.db import session_scope
from samawi_bot.db.models import EvergreenPost, TopicHistory
from samawi_bot.jobs.evergreen import COOLDOWN, EVERGREEN_PILLARS, EvergreenEmpty, take, top_up

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def evergreen_draft(**overrides: Any) -> Draft:
    """The fixture draft made timeless: no series, no 'hari ini'."""
    data = load_draft().model_dump(mode="json")
    data["series"] = None
    data["slides"][-1]["heading"] = "Catat utangmu sekarang"
    data.update(overrides)
    return Draft.model_validate(data)


def _rows(app: App) -> list[EvergreenPost]:
    with session_scope(app.db) as s:
        rows = list(s.scalars(select(EvergreenPost).order_by(EvergreenPost.id)))
        for r in rows:
            s.expunge(r)
        return rows


# ── top_up ────────────────────────────────────────────────────────────────────────────────────


def test_top_up_adds_verified_posts(make_app: Callable[..., App]) -> None:
    a = evergreen_draft()
    b = evergreen_draft(topic="Pencatatan utang dalam ayat terpanjang")
    llm = FakeLLM(
        {
            "Draft": [a, b],
            "JudgeReport": [
                judge_report(a),
                judge_report(
                    b, hooks=[{"index": i, "curiosity_gap": 9, "specificity": 9, "payoff_honesty": 9, "thumb_stop": 10 if i == 3 else 7} for i in range(5)]
                ),
            ],
        }
    )
    app = make_app(llm=llm)
    assert top_up(app, 2) == {"added": 2, "failed": 0}
    rows = _rows(app)
    assert [r.pillar for r in rows] == EVERGREEN_PILLARS[:2]
    assert [r.topic_hash for r in rows] == [topic_hash(a.topic), topic_hash(b.topic)]
    assert [r.hook_index for r in rows] == [0, 3]
    assert rows[0].json == a.model_dump(mode="json")
    assert all(r.created_at is not None and r.last_used_at is None and not r.retired for r in rows)
    writer_prompts = [u for name, _, u in llm.calls if name == "Draft"]
    assert len(writer_prompts) == 2 and all("EVERGREEN" in p and "slot evergreen" in p for p in writer_prompts)
    assert app.mailer.sent == []  # type: ignore[attr-defined]


def test_top_up_with_explicit_pillars(make_app: Callable[..., App]) -> None:
    d = evergreen_draft()
    llm = FakeLLM({"Draft": [d], "JudgeReport": [judge_report(d)]})
    app = make_app(llm=llm)
    assert top_up(app, 1, pillars=["doa_dzikir"]) == {"added": 1, "failed": 0}
    assert _rows(app)[0].pillar == "doa_dzikir"
    assert "Pilar: doa_dzikir" in llm.calls[0][2]


def test_top_up_failure_is_counted_and_emailed(make_app: Callable[..., App]) -> None:
    app = make_app(llm=FakeLLM())  # the writer fails both attempts
    assert top_up(app, 1) == {"added": 0, "failed": 1}
    assert _rows(app) == []
    ((subject, body),) = app.mailer.sent  # type: ignore[attr-defined]
    assert subject == "Evergreen top-up: 0 added, 1 failed"
    assert body.startswith(f"{EVERGREEN_PILLARS[0]}: ") and "FakeLLM has no Draft" in body


def test_top_up_rejects_a_dated_draft(make_app: Callable[..., App]) -> None:
    dated = load_draft()  # part of a series, and says "hari ini"
    app = make_app(llm=FakeLLM({"Draft": [dated, dated]}))
    assert top_up(app, 1) == {"added": 0, "failed": 1}
    body = app.mailer.sent[0][1]  # type: ignore[attr-defined]
    assert "not part of a series" in body and "specific day" in body


def test_top_up_never_banks_the_same_topic_twice(make_app: Callable[..., App]) -> None:
    d = evergreen_draft()
    app = make_app(llm=FakeLLM({"Draft": [d, d, d], "JudgeReport": [judge_report(d)]}))
    assert top_up(app, 2) == {"added": 1, "failed": 1}
    assert len(_rows(app)) == 1
    assert "topic reused" in app.mailer.sent[0][1]  # type: ignore[attr-defined]


# ── take ──────────────────────────────────────────────────────────────────────────────────────


def _add(app: App, topic: str, created: datetime, last_used: datetime | None = None, retired: bool = False, json: dict[str, Any] | None = None) -> int:
    with session_scope(app.db) as s:
        row = EvergreenPost(
            json=json if json is not None else evergreen_draft(topic=topic).model_dump(mode="json"),
            rendered_media_keys=[],
            pillar="fakta_quran",
            topic_hash=topic_hash(topic),
            hook_index=1,
            created_at=created,
            last_used_at=last_used,
            retired=retired,
        )
        s.add(row)
        s.flush()
        return row.id


def test_take_is_least_recently_used(make_app: Callable[..., App], clock: Clock) -> None:
    app = make_app()
    used_old = _add(app, "Topik satu", T0, last_used=T0 + timedelta(days=1))
    used_new = _add(app, "Topik dua", T0, last_used=T0 + timedelta(days=5))
    never_late = _add(app, "Topik tiga", T0 + timedelta(days=2))
    never_early = _add(app, "Topik empat", T0 + timedelta(days=1))
    _add(app, "Topik lima", T0, retired=True)
    order = []
    for i in range(4):
        clock.now = NOW + timedelta(days=i)
        item = take(app, TODAY)
        order.append(item.id)
        assert item.last_used_at == clock.now
    assert order == [never_early, never_late, used_old, used_new]
    clock.now = NOW + timedelta(days=10)
    assert take(app, TODAY).id == never_early  # now the least recently used again
    stored = {r.id: r.last_used_at for r in _rows(app)}
    assert stored[never_early] is not None and stored[never_early].replace(tzinfo=UTC) == clock.now


def test_take_skips_topics_in_their_60_day_cooldown(make_app: Callable[..., App]) -> None:
    app = make_app()
    cooling = _add(app, "Topik dingin", T0)
    free = _add(app, "Topik bebas", T0 + timedelta(days=1))
    with session_scope(app.db) as s:
        s.add(TopicHistory(topic_hash=topic_hash("Topik dingin"), topic="Topik dingin", used_at=TODAY - timedelta(days=59)))
        s.add(TopicHistory(topic_hash=topic_hash("Topik bebas"), topic="Topik bebas", used_at=TODAY - COOLDOWN))  # exactly 60 days: free again
    assert take(app, TODAY).id == free
    assert take(app, TODAY).id == free  # the cooling one is still not eligible
    assert take(app, TODAY + timedelta(days=1)).id == cooling  # day 60 for it


def test_take_raises_when_bank_is_empty_or_cooling(make_app: Callable[..., App]) -> None:
    app = make_app()
    with pytest.raises(EvergreenEmpty):
        take(app, TODAY)
    _add(app, "Satu-satunya", T0)
    _add(app, "Pensiun", T0, retired=True)
    with session_scope(app.db) as s:
        s.add(TopicHistory(topic_hash=topic_hash("Satu-satunya"), topic="x", used_at=TODAY - timedelta(days=1)))
    with pytest.raises(EvergreenEmpty, match="60-day cooldown"):
        take(app, TODAY)


def test_take_refuses_an_unparseable_bank_row(make_app: Callable[..., App]) -> None:
    app = make_app()
    rid = _add(app, "Rusak", T0, json={"topic": "Rusak"})
    with pytest.raises(ValidationError):
        take(app, TODAY)
    (row,) = _rows(app)
    assert row.id == rid and row.last_used_at is None  # not marked as used
