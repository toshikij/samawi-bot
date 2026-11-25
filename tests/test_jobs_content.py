"""Shared produce/evergreen pieces: check context from history, captions, threads, rendering glue."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Any, ClassVar

import pytest
from PIL import Image

from conftest import TODAY, load_draft
from samawi_bot.app import App
from samawi_bot.brain.models import Draft
from samawi_bot.checks.rules import topic_hash
from samawi_bot.checks.text import count_chars
from samawi_bot.config import LaunchMode
from samawi_bot.db import session_scope
from samawi_bot.db.models import ContentPlan, DraftRow, Flag, SourceAudit, TopicHistory
from samawi_bot.jobs import content
from samawi_bot.jobs.content import (
    HADITH_CREDIT,
    LICENCES,
    TOO_EASY_FLAG,
    RenderedPost,
    audit_sources,
    check_context,
    final_caption,
    final_thread,
    record_topic,
    render_feed,
    render_story,
)
from samawi_bot.render.compose import SlideSpec, action_line
from samawi_bot.render.renderer import FEED, STORY, Rendered


def tiny_png() -> bytes:
    out = BytesIO()
    Image.new("RGB", (4, 5), (10, 20, 30)).save(out, "PNG")
    return out.getvalue()


# ── check_context ─────────────────────────────────────────────────────────────────────────────


def _feed(day: Any, bridge: str | None, slot: str = "siang_feed") -> ContentPlan:
    return ContentPlan(date=day, slot=slot, status="published", bridge_options=[], bridge_feature=bridge)


def test_check_context_reads_history_bridges_and_learned_too_easy(make_app: Callable[..., App]) -> None:
    app = make_app()
    learned = [{"id": "learned-1", "text": "Subuh dua rakaat", "all_of": [["subuh"], ["dua", "2"]]}]
    with session_scope(app.db) as s:
        s.add(TopicHistory(topic_hash="recent", topic="t", used_at=TODAY - timedelta(days=61)))
        s.add(TopicHistory(topic_hash="old", topic="t", used_at=TODAY - timedelta(days=62)))
        s.add_all(
            [
                _feed(TODAY - timedelta(days=1), "tafsir"),
                _feed(TODAY - timedelta(days=2), None),  # no bridge recorded: skipped
                _feed(TODAY - timedelta(days=3), "adzan"),
                _feed(TODAY - timedelta(days=4), "qibla"),  # older than the last two
                _feed(TODAY, "khatamPlanner"),  # today itself is not "recent"
                _feed(TODAY - timedelta(days=1), "travelMode", slot="malam_story"),  # not a feed slot
            ]
        )
        s.add(Flag(name=TOO_EASY_FLAG, value=json.dumps(learned), updated_at=datetime(2026, 11, 1, tzinfo=UTC), updated_by="learn"))
    with session_scope(app.db) as s:
        ctx = check_context(app, s, TODAY, evergreen=True)
    assert ctx.today == TODAY and ctx.launch_mode is LaunchMode.PRELAUNCH and ctx.evergreen
    assert set(ctx.topic_history) == {"recent"}
    assert ctx.recent_bridges == ["tafsir", "adzan"]
    (te,) = ctx.extra_too_easy
    assert (te.id, te.text, te.all_of) == ("learned-1", "Subuh dua rakaat", (("subuh",), ("dua", "2")))
    assert te.matches("Salat Subuh itu dua rakaat")


def test_check_context_empty(make_app: Callable[..., App]) -> None:
    app = make_app(launch_mode="live")
    with session_scope(app.db) as s:
        ctx = check_context(app, s, TODAY)
    assert ctx.topic_history == {} and ctx.recent_bridges == [] and ctx.extra_too_easy == []
    assert ctx.launch_mode is LaunchMode.LIVE and not ctx.evergreen


def test_record_topic(make_app: Callable[..., App]) -> None:
    app = make_app()
    with session_scope(app.db) as s:
        record_topic(s, "Ayat terpanjang", TODAY)
    with session_scope(app.db) as s:
        (row,) = s.query(TopicHistory).all()
        assert (row.topic, row.topic_hash, row.used_at) == ("Ayat terpanjang", topic_hash("Ayat terpanjang"), TODAY)


def test_audit_sources_hashes_every_source(make_app: Callable[..., App]) -> None:
    app = make_app()
    draft = load_draft()
    with session_scope(app.db) as s:
        plan = ContentPlan(date=TODAY, slot="siang_feed", bridge_options=[])
        s.add(plan)
        s.flush()
        row = DraftRow(plan_id=plan.id, json={}, check_report={}, attempt=1, status="passed", created_at=datetime(2026, 11, 25, tzinfo=UTC))
        s.add(row)
        s.flush()
        audit_sources(app, s, row, draft, TODAY)
    with session_scope(app.db) as s:
        rows = s.query(SourceAudit).order_by(SourceAudit.id).all()
    assert [(r.kind, r.ref) for r in rows] == [("fact", "quran:longest_ayah"), ("ayat", "2:282"), ("ayat", "2:282"), ("hadith", "2962")]
    assert all(r.licence == LICENCES[r.kind] and len(r.text_hash) == 64 for r in rows)


# ── final_caption ─────────────────────────────────────────────────────────────────────────────


def test_final_caption_appends_everything_once() -> None:
    d = load_draft()
    out = final_caption(d, LaunchMode.PRELAUNCH)
    assert out.split("\n\n") == [
        *d.caption.split("\n\n"),
        d.next_teaser,
        d.bridge_line,
        action_line(LaunchMode.PRELAUNCH),
        d.share_prompt,
        HADITH_CREDIT,  # the draft quotes HadeethEnc hadith 2962
        " ".join(d.hashtags),
    ]
    assert count_chars(out) <= 2200


def test_final_caption_credits_hadeethenc_only_when_a_hadith_is_quoted() -> None:
    d = load_draft()
    without = load_draft(slides=[s.model_dump() for s in d.slides if not (s.source and s.source.type == "hadith")])
    assert HADITH_CREDIT in final_caption(d, LaunchMode.PRELAUNCH)
    assert HADITH_CREDIT not in final_caption(without, LaunchMode.PRELAUNCH)


def test_final_caption_live_action_line() -> None:
    assert action_line(LaunchMode.LIVE) in final_caption(load_draft(), LaunchMode.LIVE)


def test_final_caption_does_not_duplicate_what_the_caption_already_says() -> None:
    d = load_draft()
    d = load_draft(caption=f"Isi.\n\n{d.next_teaser}\n\n{d.bridge_line}\n\n{d.share_prompt}")
    out = final_caption(d, LaunchMode.PRELAUNCH)
    for part in (d.next_teaser, d.bridge_line, d.share_prompt):
        assert out.count(part) == 1
    assert out.endswith(action_line(LaunchMode.PRELAUNCH) + "\n\n" + HADITH_CREDIT + "\n\n" + " ".join(d.hashtags))


def test_final_caption_without_share_prompt() -> None:
    out = final_caption(load_draft(share_prompt=""), LaunchMode.PRELAUNCH)
    assert "Kirim ke teman" not in out
    assert "\n\n\n" not in out


def _overhead() -> int:
    d = load_draft(caption="x")
    return count_chars(final_caption(d, LaunchMode.PRELAUNCH)) - 1


def test_final_caption_at_exactly_2200_keeps_everything() -> None:
    d = load_draft(caption="a" * (2200 - _overhead()))
    out = final_caption(d, LaunchMode.PRELAUNCH)
    assert count_chars(out) <= 2200
    assert d.bridge_line in out


def test_final_caption_over_2200_keeps_credit_and_hashtags() -> None:
    d = load_draft(caption="a" * (2201 - _overhead()))
    out = final_caption(d, LaunchMode.PRELAUNCH)
    assert out.endswith("\n\n" + HADITH_CREDIT + "\n\n" + " ".join(d.hashtags))
    assert out.startswith("a" * 1000)
    assert count_chars(out) <= 2200


# ── final_thread ──────────────────────────────────────────────────────────────────────────────


def test_final_thread_appends_action_line_to_last_post() -> None:
    d = load_draft()
    out = final_thread(d, LaunchMode.LIVE)
    assert out[:-1] == d.thread[:-1]
    assert out[-1] == f"{d.thread[-1]}\n\n{action_line(LaunchMode.LIVE)}"
    assert d.thread[-1] != out[-1]  # the draft itself is untouched


def test_final_thread_skips_action_line_when_it_would_pass_500() -> None:
    action = action_line(LaunchMode.PRELAUNCH)
    fits = "a" * (500 - len(action) - 2)
    assert final_thread(load_draft(thread=["hook", fits]), LaunchMode.PRELAUNCH)[-1] == f"{fits}\n\n{action}"
    too_long = fits + "a"
    assert final_thread(load_draft(thread=["hook", too_long]), LaunchMode.PRELAUNCH) == ["hook", too_long]


# ── render_feed / render_story with a fake browser ────────────────────────────────────────────


class FakeRenderer:
    calls: ClassVar[list[tuple[str, Any, str, tuple[int, int]]]] = []
    extra_pages: ClassVar[int] = 0

    def __init__(self, sources: Any, browser: Any):
        self.sources, self.browser = sources, browser

    def render_slides(self, specs: list[SlideSpec], sky: str, size: tuple[int, int]) -> list[Rendered]:
        FakeRenderer.calls.append(("slides", len(specs), sky, size))
        return [Rendered(tiny_png(), s) for s in specs] + [Rendered(tiny_png(), specs[-1]) for _ in range(FakeRenderer.extra_pages)]

    def render_one(self, spec: SlideSpec, sky: str, size: tuple[int, int]) -> bytes:
        FakeRenderer.calls.append(("one", spec.kind, sky, size))
        return tiny_png()


@pytest.fixture
def fake_browser(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[FakeRenderer]]:
    @contextmanager
    def fake_chromium() -> Iterator[object]:
        yield object()

    FakeRenderer.calls, FakeRenderer.extra_pages = [], 0
    monkeypatch.setattr(content, "chromium", fake_chromium)
    monkeypatch.setattr(content, "Renderer", FakeRenderer)
    yield FakeRenderer


def test_render_feed_carousel(make_app: Callable[..., App], fake_browser: type[FakeRenderer]) -> None:
    app = make_app()
    out = render_feed(app, load_draft(), 0, "dzuhur", "posts/x/siang_feed")
    assert out == RenderedPost("carousel", [f"posts/x/siang_feed/slide-{i:02d}.png" for i in range(1, 8)], "posts/x/siang_feed/slide-01.png")
    assert all(app.storage.exists(k) for k in out.media)
    assert fake_browser.calls == [("slides", 7, "dzuhur", FEED)]


def test_render_feed_reel(make_app: Callable[..., App], fake_browser: type[FakeRenderer], monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app()
    seen: dict[str, Any] = {}

    def fake_reel(slides: list[bytes], stops: list[str]) -> bytes:
        seen["n"], seen["stops"] = len(slides), stops
        return b"mp4-bytes"

    monkeypatch.setattr(content, "render_reel", fake_reel)
    out = render_feed(app, load_draft(format="reel"), 0, "maghrib", "p")
    assert out == RenderedPost("reel", ["p/reel.mp4"], "p/slide-01.png")
    assert app.storage.get("p/reel.mp4") == b"mp4-bytes"
    assert seen == {"n": 7, "stops": app.sources.brand.palettes["maghrib"].stops}


def test_render_feed_refuses_more_than_10_slides(make_app: Callable[..., App], fake_browser: type[FakeRenderer]) -> None:
    fake_browser.extra_pages = 4  # e.g. a long ayat paginated: 7 + 4 = 11
    with pytest.raises(ValueError, match="11 slides"):
        render_feed(make_app(), load_draft(), 0, "dzuhur", "p")


def test_render_story(make_app: Callable[..., App], fake_browser: type[FakeRenderer]) -> None:
    app = make_app()
    assert render_story(app, SlideSpec("story", kicker="k"), "subuh", "posts/d/subuh_story.png") == "posts/d/subuh_story.png"
    assert app.storage.exists("posts/d/subuh_story.png")
    assert fake_browser.calls == [("one", "story", "subuh", STORY)]


def test_draft_roundtrip_through_json() -> None:
    d = load_draft()
    assert Draft.model_validate(d.model_dump(mode="json")) == d
