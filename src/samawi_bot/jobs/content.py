"""Shared pieces of produce/evergreen: check context from history, rendering, payloads."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..app import App
from ..brain.models import Draft
from ..checks.draft_checks import CheckContext, resolve_source, uses
from ..checks.rules import TooEasy, topic_hash
from ..checks.text import count_chars
from ..config import LaunchMode
from ..db.models import ContentPlan, DraftRow, Flag, SourceAudit, TopicHistory
from ..render.compose import action_line, compose_carousel
from ..render.reel import render_reel
from ..render.renderer import FEED, STORY, Renderer, chromium

TOO_EASY_FLAG = "too_easy_learned"
LICENCES = {"ayat": "Tanzil (verbatim) + Terjemahan Kemenag", "hadith": "HadeethEnc (id)", "dua": "Samawi duas.json", "fact": "computed"}


def check_context(app: App, s: Session, today: date, evergreen: bool = False) -> CheckContext:
    history = {r.topic_hash: r.used_at for r in s.scalars(select(TopicHistory).where(TopicHistory.used_at >= today - timedelta(days=61)))}
    feeds = s.scalars(
        select(ContentPlan)
        .where(ContentPlan.slot == "siang_feed", ContentPlan.date < today, ContentPlan.bridge_feature.is_not(None))
        .order_by(ContentPlan.date.desc())
        .limit(2)
    ).all()
    flag = s.get(Flag, TOO_EASY_FLAG)
    learned: list[TooEasy] = []
    if flag is not None:
        import json

        learned = [TooEasy(e["id"], e["text"], tuple(tuple(g) for g in e["all_of"])) for e in json.loads(flag.value)]
    return CheckContext(
        today=today,
        launch_mode=app.settings.launch_mode,
        topic_history=history,
        recent_bridges=[f.bridge_feature for f in feeds if f.bridge_feature],
        extra_too_easy=learned,
        evergreen=evergreen,
    )


def record_attempts(s: Session, plan_id: int, attempts: list[Any], now: Any) -> DraftRow | None:
    passed: DraftRow | None = None
    for i, a in enumerate(attempts, 1):
        row = DraftRow(
            plan_id=plan_id,
            json=a.draft.model_dump(mode="json") if a.draft else {},
            check_report={"errors": a.checks.errors if a.checks else None},
            judge_report=a.judge.model_dump(mode="json") if a.judge else None,
            attempt=i,
            status="passed" if a.passed else "failed",
            hook_index=a.gate.hook_index if a.gate else None,
            created_at=now,
        )
        s.add(row)
        s.flush()
        if a.passed:
            passed = row
    return passed


def audit_sources(app: App, s: Session, draft_row: DraftRow, draft: Draft, day: date) -> None:
    for ref in uses(draft):
        text, _ = resolve_source(app.sources, ref, day)  # the checks already proved this resolves
        s.add(SourceAudit(draft_id=draft_row.id, kind=ref.type, ref=ref.ref, text_hash=hashlib.sha256(text.encode()).hexdigest(), licence=LICENCES[ref.type]))


HADITH_CREDIT = "Terjemahan hadis: HadeethEnc.com"  # required by HadeethEnc's terms (SOURCES.md)


def final_caption(draft: Draft, mode: LaunchMode) -> str:
    parts = [draft.caption.strip()]
    credit = HADITH_CREDIT if any(s.source and s.source.type == "hadith" for s in draft.slides) else ""
    if draft.next_teaser not in draft.caption:
        parts.append(draft.next_teaser)
    if draft.bridge_line not in draft.caption:
        parts.append(draft.bridge_line)
    parts.append(action_line(mode))
    if draft.share_prompt and draft.share_prompt not in draft.caption:
        parts.append(draft.share_prompt)
    parts.append(credit)
    parts.append(" ".join(draft.hashtags))
    caption = "\n\n".join(p for p in parts if p)
    if count_chars(caption) <= 2200:
        return caption
    tail = "\n\n".join(p for p in (credit, " ".join(draft.hashtags)) if p)
    return draft.caption.strip()[: 2200 - count_chars(tail) - 2] + "\n\n" + tail


def final_thread(draft: Draft, mode: LaunchMode) -> list[str]:
    posts = list(draft.thread)
    tail = f"{posts[-1]}\n\n{action_line(mode)}"
    if count_chars(tail) <= 500:
        posts[-1] = tail
    return posts


@dataclass
class RenderedPost:
    kind: str  # carousel|reel
    media: list[str]
    cover: str


def render_feed(app: App, draft: Draft, hook_index: int, sky: str, prefix: str) -> RenderedPost:
    specs = compose_carousel(draft, hook_index, app.sources, app.settings.launch_mode, app.settings.translation_mode)
    with chromium() as browser:
        renderer = Renderer(app.sources, browser)
        rendered = renderer.render_slides(specs, sky, FEED)
        keys = []
        for i, r in enumerate(rendered, 1):
            key = f"{prefix}/slide-{i:02d}.png"
            app.storage.put(key, r.png)
            keys.append(key)
        if len(keys) > 10:
            raise ValueError(f"carousel rendered to {len(keys)} slides; Instagram allows 10")
        if draft.format.value == "reel":
            mp4 = render_reel([r.png for r in rendered], app.sources.brand.palettes[sky].stops)
            key = f"{prefix}/reel.mp4"
            app.storage.put(key, mp4)
            return RenderedPost("reel", [key], keys[0])
    return RenderedPost("carousel", keys, keys[0])


def record_topic(s: Session, topic: str, day: date) -> None:
    s.add(TopicHistory(topic_hash=topic_hash(topic), topic=topic, used_at=day))


def render_story(app: App, spec: Any, sky: str, key: str) -> str:
    with chromium() as browser:
        png = Renderer(app.sources, browser).render_one(spec, sky, STORY)
    app.storage.put(key, png)
    return key
