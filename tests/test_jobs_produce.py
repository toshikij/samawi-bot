"""`produce`: write → check → judge → render → ready for every slot; evergreen on any feed failure.

Rendering is faked (tiny PNGs) except in the one end-to-end test, which renders for real and then
publishes in dry-run mode.
"""

from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from io import BytesIO
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select

from conftest import TODAY, FakeLLM, judge_report, load_draft
from samawi_bot.app import App
from samawi_bot.brain.models import Draft
from samawi_bot.brain.planner import SERIES_TITLES
from samawi_bot.checks.rules import topic_hash
from samawi_bot.config import LaunchMode
from samawi_bot.db import session_scope
from samawi_bot.db.models import ContentPlan, DraftRow, EvergreenPost, Post, SourceAudit, TopicHistory
from samawi_bot.jobs import produce
from samawi_bot.jobs.content import LICENCES, RenderedPost, final_caption, final_thread
from samawi_bot.jobs.plan import plan_days
from samawi_bot.jobs.produce import _fallback_subuh_thread, _seasonal, _series_title, run_produce
from samawi_bot.jobs.publish import publish_plan
from samawi_bot.render.compose import ayat_of_day
from samawi_bot.render.renderer import Overflow, RenderError
from samawi_bot.sources.hijri import islamic_date
from samawi_bot.sources.quran import parse_reference

T0 = datetime(2026, 9, 1, tzinfo=UTC)


def tiny_png(colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    out = BytesIO()
    Image.new("RGB", (4, 5), colour).save(out, "PNG")
    return out.getvalue()


class FakeRender:
    """Stands in for render_feed / render_story; can fail on cue."""

    def __init__(self, feed_errors: list[Exception] | None = None, story_errors: dict[str, Exception] | None = None):
        self.feed_errors = list(feed_errors or [])
        self.story_errors = story_errors or {}
        self.feeds: list[tuple[str, int, str, str]] = []
        self.stories: list[tuple[Any, str, str]] = []

    def feed(self, app: App, draft: Draft, hook: int, sky: str, prefix: str) -> RenderedPost:
        self.feeds.append((draft.topic, hook, sky, prefix))
        if self.feed_errors:
            raise self.feed_errors.pop(0)
        keys = [f"{prefix}/slide-{i:02d}.png" for i in range(1, len(draft.slides) + 3)]
        for k in keys:
            app.storage.put(k, tiny_png())
        if draft.format.value == "reel":
            app.storage.put(f"{prefix}/reel.mp4", b"mp4")
            return RenderedPost("reel", [f"{prefix}/reel.mp4"], keys[0])
        return RenderedPost("carousel", keys, keys[0])

    def story(self, app: App, spec: Any, sky: str, key: str) -> str:
        self.stories.append((spec, sky, key))
        slot = key.rsplit("/", 1)[1].removesuffix(".png")
        if slot in self.story_errors:
            raise self.story_errors[slot]
        app.storage.put(key, tiny_png())
        return key


@pytest.fixture
def fake_render(monkeypatch: pytest.MonkeyPatch) -> FakeRender:
    fake = FakeRender()
    monkeypatch.setattr(produce, "render_feed", fake.feed)
    monkeypatch.setattr(produce, "render_story", fake.story)
    return fake


def good_llm(draft: Draft | None = None) -> FakeLLM:
    d = draft or load_draft()
    return FakeLLM({"Draft": [d], "JudgeReport": [judge_report(d)]})


def plans_by_slot(app: App, day: date = TODAY) -> dict[str, ContentPlan]:
    with session_scope(app.db) as s:
        rows = {p.slot: p for p in s.scalars(select(ContentPlan).where(ContentPlan.date == day))}
        for r in rows.values():
            s.expunge(r)
        return rows


def payload_of(app: App, plan_id: int) -> dict[str, Any] | None:
    with session_scope(app.db) as s:
        row = s.scalars(select(DraftRow).where(DraftRow.plan_id == plan_id, DraftRow.status == "payload")).first()
        return dict(row.json) if row else None


def bank(app: App, **overrides: Any) -> int:
    data = load_draft(series=None, topic="Surah dengan dua Bismillah").model_dump(mode="json")
    data["slides"][-1]["heading"] = "Catat utangmu sekarang"
    data.update(overrides)
    with session_scope(app.db) as s:
        row = EvergreenPost(json=data, rendered_media_keys=[], pillar="fakta_quran", topic_hash=topic_hash(data["topic"]), hook_index=2, created_at=T0)
        s.add(row)
        s.flush()
        return row.id


# ── happy path ────────────────────────────────────────────────────────────────────────────────


def test_produce_happy_path(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    draft = load_draft()
    llm = good_llm(draft)
    app = make_app(llm=llm)
    plan_days(app, TODAY, 1)
    summary = run_produce(app)
    assert summary["day"] == str(TODAY)
    assert summary["feed"] == {"evergreen": False, "topic": draft.topic, "hook": draft.hooks[0], "media": len(draft.slides) + 2}
    assert summary["scheduled"] == []  # no EventBridge scheduler configured

    plans = plans_by_slot(app)
    assert {p.status for p in plans.values()} == {"ready"}
    feed_plan = plans["siang_feed"]
    assert feed_plan.bridge_feature == draft.bridge_feature

    # Writer brief carries the ayat of the day; the judge saw the draft.
    aod = ayat_of_day(TODAY.toordinal())
    assert aod == "64:11"
    writer_prompt = llm.calls[0][2]
    assert [c[0] for c in llm.calls] == ["Draft", "JudgeReport"]
    assert f"Ayat hari ini untuk `threads_subuh`: {aod}" in writer_prompt
    assert f'"id": "{feed_plan.series_id}"' in writer_prompt and f"Pilar: {feed_plan.pillar}" in writer_prompt

    feed = payload_of(app, feed_plan.id)
    assert feed is not None
    with session_scope(app.db) as s:
        drafts = s.scalars(select(DraftRow).where(DraftRow.plan_id == feed_plan.id).order_by(DraftRow.id)).all()
        assert [(d.status, d.attempt) for d in drafts] == [("passed", 1), ("payload", 0)]
        passed = drafts[0]
        assert passed.hook_index == 0 and passed.media_keys == feed["media"]
        assert passed.judge_report is not None and passed.check_report == {"errors": []}
        audits = s.scalars(select(SourceAudit).where(SourceAudit.draft_id == passed.id)).all()
        assert sorted((a.kind, a.ref) for a in audits) == [("ayat", "2:282"), ("ayat", "2:282"), ("fact", "quran:longest_ayah"), ("hadith", "2962")]
        assert all(a.licence == LICENCES[a.kind] for a in audits)
        (topic,) = s.scalars(select(TopicHistory)).all()
        assert (topic.topic, topic.used_at) == (draft.topic, TODAY)
    assert feed == {
        "kind": "carousel",
        "media": [f"posts/{TODAY}/siang_feed/slide-{i:02d}.png" for i in range(1, 8)],
        "cover": f"posts/{TODAY}/siang_feed/slide-01.png",
        "caption": final_caption(draft, LaunchMode.PRELAUNCH),
        "draft_id": passed.id,
        "hook": draft.hooks[0],
    }
    assert payload_of(app, plans["siang_threads"].id) == {"kind": "chain", "posts": final_thread(draft, LaunchMode.PRELAUNCH), "images": feed["media"]}
    assert payload_of(app, plans["subuh_threads"].id) == {"kind": "text", "posts": [draft.threads_subuh]}
    assert payload_of(app, plans["malam_threads"].id) == {"kind": "text", "posts": [draft.threads_malam]}
    for slot in ("subuh_story", "malam_story", "kahfi_story"):
        assert payload_of(app, plans[slot].id) == {"kind": "story", "media": [f"posts/{TODAY}/{slot}.png"]}

    stories = {key.rsplit("/", 1)[1]: (spec, sky) for spec, sky, key in fake_render.stories}
    subuh, sky = stories["subuh_story.png"]
    citation = app.sources.quran.citation(parse_reference(aod, app.sources.quran))
    assert sky == "subuh" and subuh.reference == citation
    h = islamic_date(TODAY)
    assert subuh.kicker == f"Ayat hari ini · {h.day} {h.month_name} {h.year} H" + (" (perkiraan)" if h.near_boundary else "")
    assert [n for n, _ in subuh.times] == ["Subuh", "Syuruq", "Dzuhur", "Ashar", "Maghrib", "Isya"]
    malam, sky = stories["malam_story.png"]
    assert sky == "isya" and malam.body == draft.next_teaser
    assert malam.thumb == "data:image/png;base64," + base64.b64encode(tiny_png()).decode()
    kahfi, sky = stories["kahfi_story.png"]
    assert sky == "maghrib" and kahfi.body == "Mode Jumat di Samawi mengingatkanmu setiap Jumat, tanpa iklan."
    assert fake_render.feeds == [(draft.topic, 0, "dzuhur", f"posts/{TODAY}/siang_feed")]
    assert app.mailer.sent == []  # type: ignore[attr-defined]


def test_produce_plans_the_day_first_when_there_is_no_plan(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    app = make_app(llm=good_llm())
    assert plans_by_slot(app) == {}
    summary = run_produce(app, TODAY)
    assert summary["feed"]["evergreen"] is False
    assert set(plans_by_slot(app)) == {"subuh_story", "subuh_threads", "siang_feed", "siang_threads", "malam_story", "malam_threads", "kahfi_story"}


@pytest.mark.parametrize("status", ["ready", "published", "fallback"])
def test_already_produced_is_a_no_op(make_app: Callable[..., App], fake_render: FakeRender, status: str) -> None:
    llm = good_llm()
    app = make_app(llm=llm)
    plan_days(app, TODAY, 1)
    with session_scope(app.db) as s:
        s.scalars(select(ContentPlan).where(ContentPlan.slot == "siang_feed")).one().status = status
    assert run_produce(app, TODAY) == {"day": str(TODAY), "feed": "already produced"}
    assert llm.calls == [] and fake_render.feeds == []


# ── fallbacks ─────────────────────────────────────────────────────────────────────────────────


def test_writer_failing_twice_publishes_from_evergreen(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    app = make_app(llm=FakeLLM())  # no Draft queued: both writer attempts fail
    plan_days(app, TODAY, 1)
    eid = bank(app)
    summary = run_produce(app, TODAY)
    assert summary["feed"]["evergreen"] is True and summary["feed"]["topic"] == "Surah dengan dua Bismillah"
    ((subject, body),) = app.mailer.sent  # type: ignore[attr-defined]
    assert subject == f"FALLBACK {TODAY} feed → evergreen"
    assert "Attempt 1:" in body and "Attempt 2:" in body and "writer: FakeLLM has no Draft" in body
    plans = plans_by_slot(app)
    assert plans["siang_feed"].status == "fallback"
    assert plans["siang_feed"].bridge_feature == "tafsir"
    feed = payload_of(app, plans["siang_feed"].id)
    assert feed is not None and feed["evergreen_id"] == eid and "draft_id" not in feed
    evergreen = Draft.model_validate(bank_json(app, eid))
    assert feed["hook"] == evergreen.hooks[2]
    assert feed["media"][0].startswith(f"posts/{TODAY}/siang_feed-evergreen/")
    with session_scope(app.db) as s:
        row = s.get(EvergreenPost, eid)
        assert row is not None and row.rendered_media_keys == feed["media"] and row.last_used_at is not None
        assert [d.status for d in s.scalars(select(DraftRow).where(DraftRow.plan_id == plans["siang_feed"].id))] == ["failed", "failed", "payload"]
        assert s.scalars(select(TopicHistory)).one().topic == evergreen.topic
    # The Subuh thread cannot use the failed draft's reflection: it quotes the ayat of the day.
    subuh = payload_of(app, plans["subuh_threads"].id)
    assert subuh is not None and subuh["posts"] == [_fallback_subuh_thread(app, ayat_of_day(TODAY.toordinal()))]
    assert subuh["posts"][0].startswith("Ayat pagi ini:")
    assert {p.status for slot, p in plans.items() if slot != "siang_feed"} == {"ready"}


def bank_json(app: App, eid: int) -> dict[str, Any]:
    with session_scope(app.db) as s:
        row = s.get(EvergreenPost, eid)
        assert row is not None
        return dict(row.json)


def test_draft_failing_checks_twice_reports_the_check_errors(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    bad = load_draft(bridge_line="Fitur Tafsir di Samawi.")  # no 'tanpa iklan'
    app = make_app(llm=FakeLLM({"Draft": [bad, bad]}))
    plan_days(app, TODAY, 1)
    bank(app)
    run_produce(app, TODAY)
    body = app.mailer.sent[0][1]  # type: ignore[attr-defined]
    assert body.count("bridge line must say 'tanpa iklan'") == 2


@pytest.mark.parametrize("error", [RenderError("contrast below WCAG AA"), Overflow("list slide overflows"), ValueError("11 slides"), OSError("disk full")])
def test_render_failure_falls_back_to_evergreen(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    fake = FakeRender(feed_errors=[error])
    monkeypatch.setattr(produce, "render_feed", fake.feed)
    monkeypatch.setattr(produce, "render_story", fake.story)
    app = make_app(llm=good_llm())
    plan_days(app, TODAY, 1)
    eid = bank(app)
    summary = run_produce(app, TODAY)
    assert summary["feed"]["evergreen"] is True
    ((subject, body),) = app.mailer.sent  # type: ignore[attr-defined]
    assert subject.startswith("FALLBACK") and f"render failed: {error}" in body
    feed_plan = plans_by_slot(app)["siang_feed"]
    feed = payload_of(app, feed_plan.id)
    assert feed is not None and feed["evergreen_id"] == eid
    with session_scope(app.db) as s:
        passed = s.scalars(select(DraftRow).where(DraftRow.plan_id == feed_plan.id, DraftRow.status == "passed")).one()
        assert passed.media_keys == []  # the verified draft never got media
        assert s.scalars(select(SourceAudit)).all() == []
    assert [f[3] for f in fake.feeds] == [f"posts/{TODAY}/siang_feed", f"posts/{TODAY}/siang_feed-evergreen"]


def test_empty_bank_tolerates_a_day_without_evening_slots(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    from samawi_bot.db import session_scope
    from samawi_bot.db.models import ContentPlan

    app = make_app(llm=FakeLLM())
    plan_days(app, TODAY, 1)
    with session_scope(app.db) as s:
        s.query(ContentPlan).filter_by(date=TODAY, slot="malam_threads").delete()
    summary = run_produce(app, TODAY)
    assert summary["feed"].startswith("skipped") and app.mailer.sent[0][0].startswith("FALLBACK")  # type: ignore[attr-defined]


def test_story_that_cannot_render_skips_only_its_slot(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    """A story that cannot render (injected here) is alerted and skipped; the rest of the day carries on."""
    day = date(2026, 11, 28)
    fake = FakeRender(story_errors={"subuh_story": Overflow("story slide content does not fit")})
    monkeypatch.setattr(produce, "render_feed", fake.feed)
    monkeypatch.setattr(produce, "render_story", fake.story)
    app = make_app(llm=good_llm())
    plan_days(app, day, 1)
    summary = run_produce(app, day)
    assert "scheduled" in summary
    ((subject, body),) = app.mailer.sent  # type: ignore[attr-defined]
    assert subject == f"STORY SKIPPED {day} subuh_story" and "does not fit" in body
    plans = plans_by_slot(app, day)
    assert plans["subuh_story"].status == "failed" and payload_of(app, plans["subuh_story"].id) is None
    assert payload_of(app, plans["malam_story"].id) is not None
    assert payload_of(app, plans["subuh_threads"].id) is not None
    assert plans["malam_threads"].status == "ready"


def test_reel_feed_threads_chain_uses_the_cover(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    draft = load_draft(format="reel")
    app = make_app(llm=good_llm(draft))
    plan_days(app, TODAY, 1)
    run_produce(app, TODAY)
    plans = plans_by_slot(app)
    feed = payload_of(app, plans["siang_feed"].id)
    assert feed is not None and feed["kind"] == "reel" and feed["media"] == [f"posts/{TODAY}/siang_feed/reel.mp4"]
    chain = payload_of(app, plans["siang_threads"].id)
    assert chain is not None and chain["images"] == [feed["cover"]]


def test_feed_only_day_and_brief_defaults(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    """A hand-made plan with only the feed slot and no planner fields still produces."""
    llm = good_llm()
    app = make_app(llm=llm)
    friday = date(2026, 11, 27)
    with session_scope(app.db) as s:
        s.add(ContentPlan(date=friday, slot="siang_feed", status="planned", bridge_options=[]))
    summary = run_produce(app, friday)
    assert summary["feed"]["evergreen"] is False
    assert list(plans_by_slot(app, friday)) == ["siang_feed"]
    prompt = llm.calls[0][2]
    assert "nuansa langit dzuhur" in prompt and "Pilar: fakta_quran · format: carousel · formula hook utama: ternyata" in prompt
    assert "Seri:" not in prompt and "Hari Jumat" in prompt
    assert fake_render.stories == []


def test_kahfi_story_without_jumat_mode_has_no_bridge(make_app: Callable[..., App], fake_render: FakeRender, monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app(llm=good_llm())
    monkeypatch.setattr(app.sources.features, "check_bridge", lambda key: "not shipped" if key == "jumatMode" else None)
    plan_days(app, TODAY, 1)
    run_produce(app, TODAY)
    kahfi = next(spec for spec, _, key in fake_render.stories if key.endswith("kahfi_story.png"))
    assert kahfi.body == ""


# ── brief helpers ─────────────────────────────────────────────────────────────────────────────


def _plan(day: date, series_id: str | None = None, pillar: str | None = "fakta_quran") -> ContentPlan:
    return ContentPlan(date=day, slot="siang_feed", series_id=series_id, pillar=pillar, bridge_options=[])


def test_series_title() -> None:
    assert _series_title(_plan(TODAY, "launch")) == "Kenalan dengan Samawi"
    assert _series_title(_plan(TODAY, "fakta_quran-x")) == SERIES_TITLES["fakta_quran"]
    assert _series_title(_plan(TODAY, "x", pillar=None)) == ""


def test_seasonal_notes() -> None:
    assert _seasonal(_plan(TODAY, "launch")).startswith("SERI PELUNCURAN")
    assert _seasonal(_plan(TODAY)) == ""  # Thursday in Jumada al-Awwal
    assert _seasonal(_plan(TODAY + timedelta(days=1))) == "Hari Jumat: tema sunnah hari Jumat."
    day = TODAY
    while islamic_date(day).month != 7:
        day += timedelta(days=1)
    while day.weekday() != 4:
        day += timedelta(days=1)
    h = islamic_date(day)
    assert h.month in (7, 8)
    assert _seasonal(_plan(day)) == f"Sekarang bulan {h.month_name}: Ramadan semakin dekat. Hari Jumat: tema sunnah hari Jumat."
    sat = day + timedelta(days=1)
    assert _seasonal(_plan(sat)) == f"Sekarang bulan {islamic_date(sat).month_name}: Ramadan semakin dekat."


def test_fallback_subuh_thread(make_app: Callable[..., App]) -> None:
    app = make_app()
    short = _fallback_subuh_thread(app, "94:5-6")
    rng = parse_reference("94:5-6", app.sources.quran)
    assert short == f"Ayat pagi ini:\n\n“{' '.join(app.sources.quran.translation(r) for r in rng.refs)}” (QS. Asy-Syarh: 5–6)"
    long = _fallback_subuh_thread(app, "73:20")  # the quote would pass Threads' 500 characters
    assert long == f"Ayat pagi ini: {app.sources.quran.citation(parse_reference('73:20', app.sources.quran))}. Buka mushafmu dan baca pelan-pelan."
    assert len(long) <= 500


# ── end to end: FakeLLM → real Chromium render → storage → dry-run publish ────────────────────


@pytest.mark.browser
def test_end_to_end_produce_render_and_dry_run_publish(make_app: Callable[..., App]) -> None:
    draft = load_draft()
    app = make_app(llm=good_llm(draft), publish_live=False)
    summary = run_produce(app)  # 02:00 WIB on TODAY; plans the day itself
    assert summary["feed"]["evergreen"] is False
    plans = plans_by_slot(app)
    assert {p.status for p in plans.values()} == {"ready"}
    feed = payload_of(app, plans["siang_feed"].id)
    assert feed is not None and feed["kind"] == "carousel"
    assert summary["feed"]["media"] == len(feed["media"]) >= len(draft.slides) + 2  # 2:282 paginates
    media = list(feed["media"])
    for slot in ("subuh_story", "malam_story", "kahfi_story"):
        p = payload_of(app, plans[slot].id)
        assert p is not None
        media += p["media"]
    for key in media:
        assert app.storage.exists(key)
        img = Image.open(BytesIO(app.storage.get(key)))
        assert img.size == ((1080, 1920) if "story" in key else (1080, 1350))

    results = [publish_plan(app, p.id) for p in sorted(plans.values(), key=lambda p: p.scheduled_at or T0)]
    assert {r["status"] for r in results} == {"dry_run"}
    with session_scope(app.db) as s:
        posts = s.scalars(select(Post).order_by(Post.id)).all()
        assert len(posts) == len(plans)
        assert {p.status for p in posts} == {"dry_run"}
        assert {(p.slot, p.platform, p.media_type) for p in posts} == {
            ("subuh_story", "instagram", "story"),
            ("subuh_threads", "threads", "text"),
            ("siang_feed", "instagram", "carousel"),
            ("siang_threads", "threads", "chain"),
            ("malam_story", "instagram", "story"),
            ("malam_threads", "threads", "text"),
            ("kahfi_story", "instagram", "story"),
        }
        feed_post = next(p for p in posts if p.slot == "siang_feed")
        assert feed_post.hook_text == draft.hooks[0] and feed_post.bridge_feature == "tafsir" and feed_post.key_fact == draft.key_fact
        assert all(p.published_at is not None for p in posts)
    assert {p.status for p in plans_by_slot(app).values()} == {"published"}
    assert publish_plan(app, plans["siang_feed"].id)["status"] == "already published"


def test_yesterdays_teaser_is_passed_to_todays_writer(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    from samawi_bot.db import session_scope
    from samawi_bot.db.models import ContentPlan, DraftRow

    app = make_app(llm=good_llm())
    yesterday = TODAY - timedelta(days=1)
    with session_scope(app.db) as s:
        p = ContentPlan(date=yesterday, slot="siang_feed", status="published", bridge_options=[])
        s.add(p)
        s.flush()
        s.add(
            DraftRow(
                plan_id=p.id, json={"next_teaser": "Besok: surah dengan dua Bismillah."}, check_report={}, attempt=1, status="passed", created_at=app.now()
            )
        )
    plan_days(app, TODAY, 1)
    run_produce(app, TODAY)
    writer_prompts = [c[2] for c in app.llm_factory().calls if c[0] == "Draft"]  # type: ignore[attr-defined]
    assert 'Kemarin kita menjanjikan: "Besok: surah dengan dua Bismillah."' in writer_prompts[0]


def test_no_promise_without_a_previous_passed_draft(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    app = make_app(llm=good_llm())
    plan_days(app, TODAY, 1)
    run_produce(app, TODAY)
    writer_prompts = [c[2] for c in app.llm_factory().calls if c[0] == "Draft"]  # type: ignore[attr-defined]
    assert "Kemarin kita menjanjikan" not in writer_prompts[0]


def test_empty_bank_skips_only_the_feed_dependent_slots(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    """Thursday: the writer fails twice and the bank is empty. Nothing unverified goes out, but the
    ayat-of-the-day story, the Subuh reflection and the Kahfi reminder still do."""
    from samawi_bot.brain.llm import LLMError

    app = make_app(llm=FakeLLM({"Draft": [LLMError("down"), LLMError("down")]}))
    plan_days(app, TODAY, 1)
    summary = run_produce(app, TODAY)
    assert summary["feed"] == "skipped: no verified post and the evergreen bank is empty"
    plans = plans_by_slot(app)
    for name in ("siang_feed", "siang_threads", "malam_story", "malam_threads"):
        assert plans[name].status == "failed", name
    assert payload_of(app, plans["subuh_story"].id) is not None
    reflection = payload_of(app, plans["subuh_threads"].id)
    assert reflection is not None and reflection["posts"][0].startswith("Ayat pagi ini")
    assert payload_of(app, plans["kahfi_story"].id) is not None
    subjects = [s for s, _ in app.mailer.sent]  # type: ignore[attr-defined]
    assert f"NO FEED {TODAY}" in subjects


# ── a bank post that will not render ──────────────────────────────────────────────────────────


def test_a_bank_post_that_fails_to_render_is_retired_and_the_next_is_used(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    """26 Nov 2026: the only bank post overflowed at 02:00 and the error aborted every slot of the day."""
    overflow = Overflow("comparison slide overflows: ['right-label', 'right-value']")
    fake = FakeRender(feed_errors=[overflow])  # the first evergreen render fails
    monkeypatch.setattr(produce, "render_feed", fake.feed)
    monkeypatch.setattr(produce, "render_story", fake.story)
    app = make_app(llm=FakeLLM())  # the writer fails twice: straight to the bank
    plan_days(app, TODAY, 1)
    broken = bank(app)
    good = bank(app, topic="Ayat terpanjang dan pencatatan utang")
    summary = run_produce(app, TODAY)
    assert summary["feed"]["evergreen"] is True and summary["feed"]["topic"] == "Ayat terpanjang dan pencatatan utang"
    with session_scope(app.db) as s:
        assert s.get(EvergreenPost, broken).retired  # type: ignore[union-attr]
        assert not s.get(EvergreenPost, good).retired  # type: ignore[union-attr]
    plans = plans_by_slot(app)
    feed = payload_of(app, plans["siang_feed"].id)
    assert feed is not None and feed["evergreen_id"] == good
    assert {p.status for slot, p in plans.items() if slot != "siang_feed"} == {"ready"}
    subjects = [s for s, _ in app.mailer.sent]  # type: ignore[attr-defined]
    assert f"EVERGREEN RETIRED {TODAY} #{broken}" in subjects
    body = dict(app.mailer.sent)[f"EVERGREEN RETIRED {TODAY} #{broken}"]  # type: ignore[attr-defined]
    assert "comparison slide overflows" in body and "Surah dengan dua Bismillah" in body


def test_a_bank_where_nothing_renders_still_produces_the_standalone_slots(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRender(feed_errors=[Overflow("list slide overflows"), ValueError("carousel rendered to 11 slides; Instagram allows 10")])
    monkeypatch.setattr(produce, "render_feed", fake.feed)
    monkeypatch.setattr(produce, "render_story", fake.story)
    app = make_app(llm=FakeLLM())
    plan_days(app, TODAY, 1)
    ids = [bank(app), bank(app, topic="Ayat terpanjang dan pencatatan utang")]
    summary = run_produce(app, TODAY)
    assert summary["feed"] == "skipped: no verified post and the evergreen bank is empty"
    with session_scope(app.db) as s:
        assert all(s.get(EvergreenPost, i).retired for i in ids)  # type: ignore[union-attr]
    plans = plans_by_slot(app)
    for name in ("siang_feed", "siang_threads", "malam_story", "malam_threads"):
        assert plans[name].status == "failed", name
    for name in ("subuh_story", "subuh_threads", "kahfi_story"):
        assert payload_of(app, plans[name].id) is not None, name
    subjects = [s for s, _ in app.mailer.sent]  # type: ignore[attr-defined]
    assert sum(s.startswith("EVERGREEN RETIRED") for s in subjects) == 2 and f"NO FEED {TODAY}" in subjects


# ── never lose a day: retries, the lease, and errors outside the verified path ────────────────

from samawi_bot.db.models import Flag  # noqa: E402


def _flag(app: App, day: date = TODAY) -> Flag | None:
    with session_scope(app.db) as s:
        row = s.get(Flag, f"produce:{day.isoformat()}")
        if row is not None:
            s.expunge(row)
        return row


def test_a_retry_after_a_finished_day_does_nothing(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    app = make_app(llm=good_llm())
    plan_days(app, TODAY, 1)
    run_produce(app, TODAY)
    calls = len(app.llm_factory().calls)  # type: ignore[attr-defined]
    assert run_produce(app, TODAY)["feed"] == "already produced"
    assert len(app.llm_factory().calls) == calls  # type: ignore[attr-defined]
    flag = _flag(app)
    assert flag is not None and flag.value == "done"


def test_a_retry_brings_back_a_dark_day(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    """02:00: the writer fails and the bank is empty. 02:45: a bank post exists, so the feed and evening go out."""
    app = make_app(llm=FakeLLM())
    plan_days(app, TODAY, 1)
    assert run_produce(app, TODAY)["feed"] == "skipped: no verified post and the evergreen bank is empty"
    eid = bank(app)
    summary = run_produce(app, TODAY)
    assert summary["feed"]["evergreen"] is True
    plans = plans_by_slot(app)
    assert plans["siang_feed"].status == "fallback"
    assert {p.status for slot, p in plans.items() if slot != "siang_feed"} == {"ready"}
    feed = payload_of(app, plans["siang_feed"].id)
    assert feed is not None and feed["evergreen_id"] == eid


def test_a_run_still_holding_the_lease_blocks_a_retry(make_app: Callable[..., App], fake_render: FakeRender, clock: Any) -> None:
    app = make_app(llm=good_llm())
    plan_days(app, TODAY, 1)
    with session_scope(app.db) as s:
        s.add(Flag(name=f"produce:{TODAY.isoformat()}", value="running", updated_at=clock.now - timedelta(minutes=45), updated_by="produce"))
    assert run_produce(app, TODAY)["feed"] == "in progress in another run"
    assert plans_by_slot(app)["siang_feed"].status == "planned"
    clock.now += timedelta(minutes=20)  # 65 minutes: the first run died; its lease has expired
    assert run_produce(app, TODAY)["feed"]["evergreen"] is False
    flag = _flag(app)
    assert flag is not None and flag.value == "done"


def test_an_unexpected_error_in_the_fresh_path_falls_back_to_the_bank(
    make_app: Callable[..., App], fake_render: FakeRender, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("database connection reset")

    monkeypatch.setattr(produce, "write_verified", boom)
    app = make_app(llm=good_llm())
    plan_days(app, TODAY, 1)
    eid = bank(app)
    summary = run_produce(app, TODAY)
    assert summary["feed"]["evergreen"] is True
    feed = payload_of(app, plans_by_slot(app)["siang_feed"].id)
    assert feed is not None and feed["evergreen_id"] == eid
    subjects = dict(app.mailer.sent)  # type: ignore[attr-defined]
    assert "RuntimeError: database connection reset" in subjects[f"FEED ERROR {TODAY}"]


def test_errors_in_both_paths_still_produce_the_standalone_slots_and_release_the_lease(
    make_app: Callable[..., App], fake_render: FakeRender, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(produce, "write_verified", boom)
    monkeypatch.setattr(produce, "take_evergreen", boom)
    app = make_app(llm=good_llm())
    plan_days(app, TODAY, 1)
    summary = run_produce(app, TODAY)
    assert summary["feed"] == "skipped: no verified post and the evergreen bank is empty"
    plans = plans_by_slot(app)
    for name in ("subuh_story", "subuh_threads", "kahfi_story"):
        assert payload_of(app, plans[name].id) is not None, name
    subjects = dict(app.mailer.sent)  # type: ignore[attr-defined]
    assert "RuntimeError: down" in subjects[f"NO FEED {TODAY}"] and "unless a retry succeeds" in subjects[f"NO FEED {TODAY}"]
    flag = _flag(app)
    assert flag is not None and flag.value == "done"


def test_the_lease_is_released_even_when_a_standalone_slot_crashes(
    make_app: Callable[..., App], fake_render: FakeRender, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("schedule API down")

    monkeypatch.setattr(produce, "schedule_day", boom)
    app = make_app(llm=good_llm())
    plan_days(app, TODAY, 1)
    with pytest.raises(RuntimeError):
        run_produce(app, TODAY)
    flag = _flag(app)
    assert flag is not None and flag.value == "done"


def test_an_empty_credit_balance_is_named_and_the_day_still_publishes_from_the_bank(make_app: Callable[..., App], fake_render: FakeRender) -> None:
    """27 Nov 2026: the account ran out of credit; the day posted from the bank but no alert said why."""
    from samawi_bot.alerts import CREDIT_SUBJECT
    from samawi_bot.brain.llm import OUT_OF_CREDIT, LLMOutOfCredit

    app = make_app(llm=FakeLLM({"Draft": [LLMOutOfCredit(OUT_OF_CREDIT), LLMOutOfCredit(OUT_OF_CREDIT)]}))
    plan_days(app, TODAY, 1)
    bank(app)
    assert run_produce(app, TODAY)["feed"]["evergreen"] is True
    subjects = [s for s, _ in app.mailer.sent]  # type: ignore[attr-defined]
    assert subjects == [CREDIT_SUBJECT, f"FALLBACK {TODAY} feed → evergreen"]
    assert "Plans & Billing" in dict(app.mailer.sent)[CREDIT_SUBJECT]  # type: ignore[attr-defined]
