"""`produce` (daily 02:00 WIB): write → check → judge → render → ready, for every slot of the day.

On any failure of the feed post the day publishes from the evergreen bank and the failure report
is emailed. A failed draft is never published (spec §6.3).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select

from ..alerts import safe_send
from ..app import App
from ..brain.models import Draft, SourceRef
from ..brain.writer import Brief, write_verified
from ..db import session_scope
from ..db.models import ContentPlan, DraftRow, EvergreenPost
from ..publisher.slots import WIB
from ..render.compose import ayat_of_day, story_kahfi, story_malam, story_subuh
from ..render.renderer import RenderError
from ..sources.hijri import islamic_date
from ..sources.quran import parse_reference
from ..verify.facts import prayer_times_today
from .content import audit_sources, check_context, final_caption, final_thread, record_attempts, record_topic, render_feed, render_story
from .evergreen import EvergreenEmpty, retire_evergreen
from .evergreen import take as take_evergreen
from .schedule import schedule_day


def payload(kind: str, **data: Any) -> dict[str, Any]:
    return {"kind": kind, **data}


def _save_payload(app: App, plan_id: int, data: dict[str, Any], status: str = "ready") -> None:
    with session_scope(app.db) as s:
        s.add(DraftRow(plan_id=plan_id, json=data, check_report={}, attempt=0, status="payload", created_at=app.now()))
        plan = s.get(ContentPlan, plan_id)
        assert plan is not None
        plan.status = status


def produce_feed(app: App, plan: ContentPlan) -> tuple[Draft, int, dict[str, Any], bool]:
    """Returns (draft, hook index, feed payload, from_evergreen)."""
    day, sky = plan.date, plan.sky_moment or "dzuhur"
    aod = ayat_of_day(day.toordinal())
    rng = parse_reference(aod, app.sources.quran)
    brief = Brief(
        day=day,
        slot=plan.slot,
        pillar=plan.pillar or "fakta_quran",
        format=plan.format or "carousel",
        hook_formula=plan.hook_formula or "ternyata",
        bridge_options=list(plan.bridge_options or []),
        sky_moment=sky,
        series={"id": plan.series_id, "title": _series_title(plan), "part": plan.series_part, "total": plan.series_total} if plan.series_id else None,
        seasonal=_seasonal(plan)
        + _promised(app, plan)
        + f" Ayat hari ini untuk `threads_subuh`: {aod} — "
        + " ".join(app.sources.quran.translation(r) for r in rng.refs),
    )
    failure = ""
    with session_scope(app.db) as s:
        ctx = check_context(app, s, day)
    ctx.extra_sources = [SourceRef(type="ayat", ref=aod)]
    attempts = write_verified(app.llm_factory(), app.sources, brief, ctx)
    with session_scope(app.db) as s:
        passed = record_attempts(s, plan.id, attempts, app.now())
        if passed is not None:
            draft = Draft.model_validate(passed.json)
            hook = passed.hook_index or 0
            try:
                rendered = render_feed(app, draft, hook, sky, f"posts/{day}/{plan.slot}")
            except (RenderError, ValueError, OSError) as exc:
                failure = f"render failed: {exc}"
            else:
                passed.media_keys = rendered.media
                audit_sources(app, s, passed, draft, day)
                record_topic(s, draft.topic, day)
                plan_row = s.get(ContentPlan, plan.id)
                assert plan_row is not None
                plan_row.bridge_feature = draft.bridge_feature
                feed = payload(
                    rendered.kind,
                    media=rendered.media,
                    cover=rendered.cover,
                    caption=final_caption(draft, app.settings.launch_mode),
                    draft_id=passed.id,
                    hook=draft.hooks[hook],
                )
                return draft, hook, feed, False
        if not failure:
            failure = "\n\n".join(f"Attempt {i}:\n{a.notes()}" for i, a in enumerate(attempts, 1))
    safe_send(app.mailer, f"FALLBACK {day} feed → evergreen", f"The {day} feed post failed verification and will publish from the evergreen bank.\n\n{failure}")
    return _evergreen_feed(app, plan)


def _evergreen_feed(app: App, plan: ContentPlan) -> tuple[Draft, int, dict[str, Any], bool]:
    """The next bank post that renders. One that fails the render checks is retired with an alert and
    the next is tried, so a bad bank post costs one alert, never the day; an exhausted bank raises
    EvergreenEmpty, which run_produce turns into a dark feed with the standalone slots still produced."""
    while True:
        item = take_evergreen(app, plan.date)
        draft = Draft.model_validate(item.json)
        try:
            rendered = render_feed(app, draft, item.hook_index, plan.sky_moment or "dzuhur", f"posts/{plan.date}/{plan.slot}-evergreen")
        except (RenderError, ValueError, OSError) as exc:
            retire_evergreen(app, item.id)
            safe_send(
                app.mailer,
                f"EVERGREEN RETIRED {plan.date} #{item.id}",
                f"Bank post {item.id} ({draft.topic}) did not pass the render checks, is retired, and the next one is tried.\n\n{exc}",
            )
            continue
        break
    with session_scope(app.db) as s:
        row = s.get(EvergreenPost, item.id)
        assert row is not None
        row.rendered_media_keys = rendered.media
        record_topic(s, draft.topic, plan.date)
        plan_row = s.get(ContentPlan, plan.id)
        assert plan_row is not None
        plan_row.bridge_feature = draft.bridge_feature
    feed = payload(
        rendered.kind,
        media=rendered.media,
        cover=rendered.cover,
        caption=final_caption(draft, app.settings.launch_mode),
        evergreen_id=item.id,
        hook=draft.hooks[item.hook_index],
    )
    return draft, item.hook_index, feed, True


def _promised(app: App, plan: ContentPlan) -> str:
    """Yesterday's teaser promised today's topic; tell the writer, so the follow hook is honest."""
    with session_scope(app.db) as s:
        prev = s.scalars(
            select(DraftRow)
            .join(ContentPlan, ContentPlan.id == DraftRow.plan_id)
            .where(ContentPlan.slot == "siang_feed", ContentPlan.date < plan.date, DraftRow.status == "passed")
            .order_by(ContentPlan.date.desc(), DraftRow.id.desc())
        ).first()
        teaser = str(prev.json.get("next_teaser", "")) if prev else ""
    if not teaser:
        return ""
    return f' Kemarin kita menjanjikan: "{teaser}" — tepati janji ini hari ini jika faktanya tersedia di daftar.'


def _series_title(plan: ContentPlan) -> str:
    from ..brain.planner import SERIES_TITLES

    if plan.series_id == "launch":
        return "Kenalan dengan Samawi"
    return SERIES_TITLES.get(plan.pillar or "", "")


def _seasonal(plan: ContentPlan) -> str:
    if plan.series_id == "launch":
        return (
            "SERI PELUNCURAN: Samawi kini tersedia di Android. Bagian 1: apa itu Samawi (tanpa iklan, waktu salat Kemenag, "
            "Al-Qur'an offline). Bagian 2: tiga fitur paling disukai (dari daftar bridge). Bagian 3: 'tanpa iklan, selamanya'."
        )
    h = islamic_date(plan.date)
    note = ""
    if h.month in (7, 8):
        note = f"Sekarang bulan {h.month_name}: Ramadan semakin dekat."
    if plan.date.weekday() == 4:
        note += " Hari Jumat: tema sunnah hari Jumat."
    return note.strip()


def _fallback_subuh_thread(app: App, ref: str) -> str:
    rng = parse_reference(ref, app.sources.quran)
    text = " ".join(app.sources.quran.translation(r) for r in rng.refs)
    post = f"Ayat pagi ini:\n\n“{text}” {app.sources.quran.citation(rng)}"
    return post if len(post) <= 500 else f"Ayat pagi ini: {app.sources.quran.citation(rng)}. Buka mushafmu dan baca pelan-pelan."


def run_produce(app: App, day: date | None = None) -> dict[str, Any]:
    day = day or app.now().astimezone(WIB).date()
    with session_scope(app.db) as s:
        plans = {p.slot: p for p in s.scalars(select(ContentPlan).where(ContentPlan.date == day))}
        for p in plans.values():
            s.expunge(p)
    if "siang_feed" not in plans:
        from .plan import plan_days

        plan_days(app, day, days=1)
        return run_produce(app, day)
    summary: dict[str, Any] = {"day": str(day)}
    feed_plan = plans["siang_feed"]
    if feed_plan.status in ("ready", "published", "fallback"):
        summary["feed"] = "already produced"
        return summary
    aod = ayat_of_day(day.toordinal())
    try:
        draft, hook, feed, from_evergreen = produce_feed(app, feed_plan)
    except EvergreenEmpty as exc:
        # No verified feed post and no bank post: the feed and everything built from it stay dark,
        # but the slots that stand on their own (ayat of the day, Kahfi reminder) still go out.
        safe_send(app.mailer, f"NO FEED {day}", f"{exc}. The feed, its Threads chain and the evening slots are skipped today.")
        for name in ("siang_feed", "siang_threads", "malam_story", "malam_threads"):
            if name in plans:
                _mark_failed(app, plans[name].id)
        summary["feed"] = "skipped: no verified post and the evergreen bank is empty"
        _standalone_slots(app, plans, day, aod, None)
        summary["scheduled"] = schedule_day(app, day)
        return summary
    _save_payload(app, feed_plan.id, feed, "fallback" if from_evergreen else "ready")
    summary["feed"] = {"evergreen": from_evergreen, "topic": draft.topic, "hook": draft.hooks[hook], "media": len(feed["media"])}
    if "siang_threads" in plans:
        _save_payload(
            app,
            plans["siang_threads"].id,
            payload("chain", posts=final_thread(draft, app.settings.launch_mode), images=feed["media"] if feed["kind"] == "carousel" else [feed["cover"]]),
        )
    _standalone_slots(app, plans, day, aod, None if from_evergreen else draft.threads_subuh)
    if "malam_story" in plans:
        cover_uri = _local_uri(app, feed["cover"])
        _story(app, plans["malam_story"], story_malam(cover_uri, draft.next_teaser), "isya", f"posts/{day}/malam_story.png")
    if "malam_threads" in plans:
        _save_payload(app, plans["malam_threads"].id, payload("text", posts=[draft.threads_malam]))
    summary["scheduled"] = schedule_day(app, day)
    return summary


def _standalone_slots(app: App, plans: dict[str, ContentPlan], day: date, aod: str, reflection: str | None) -> None:
    """Slots that do not depend on the feed post: Subuh story and reflection, Thursday's Kahfi story."""
    hijri = islamic_date(day)
    hijri_label = f"{hijri.day} {hijri.month_name} {hijri.year} H" + (" (perkiraan)" if hijri.near_boundary else "")
    if "subuh_story" in plans:
        spec = story_subuh(app.sources, aod, hijri_label, prayer_times_today(app.sources, "Jakarta", day))
        _story(app, plans["subuh_story"], spec, "subuh", f"posts/{day}/subuh_story.png")
    if "subuh_threads" in plans:
        _save_payload(app, plans["subuh_threads"].id, payload("text", posts=[reflection or _fallback_subuh_thread(app, aod)]))
    if "kahfi_story" in plans:
        jumat_ok = app.sources.features.check_bridge("jumatMode") is None
        bridge = "Mode Jumat di Samawi mengingatkanmu setiap Jumat, tanpa iklan." if jumat_ok else ""
        _story(app, plans["kahfi_story"], story_kahfi(app.sources, bridge), "maghrib", f"posts/{day}/kahfi_story.png")


def _mark_failed(app: App, plan_id: int) -> None:
    with session_scope(app.db) as s:
        row = s.get(ContentPlan, plan_id)
        assert row is not None
        row.status = "failed"


def _story(app: App, plan: ContentPlan, spec: Any, sky: str, key: str) -> None:
    """A story that cannot render (e.g. an ayat of the day too long for one story) skips its slot
    with an alert; it must not abort the rest of the day's slots."""
    try:
        media = render_story(app, spec, sky, key)
    except RenderError as exc:
        safe_send(app.mailer, f"STORY SKIPPED {plan.date} {plan.slot}", f"The {plan.slot} story did not pass the render checks and will not publish.\n\n{exc}")
        with session_scope(app.db) as s:
            row = s.get(ContentPlan, plan.id)
            assert row is not None
            row.status = "failed"
        return
    _save_payload(app, plan.id, payload("story", media=[media]))


def _local_uri(app: App, key: str) -> str:
    """A data: URI of a stored image, so the story template can embed today's cover."""
    import base64

    return "data:image/png;base64," + base64.b64encode(app.storage.get(key)).decode()
