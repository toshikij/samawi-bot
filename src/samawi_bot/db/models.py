"""Postgres schema (spec §8.2), plus job runs, tokens and runtime flags."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, ClassVar

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON, list[Any]: JSON}


class ContentPlan(Base):
    __tablename__ = "content_plan"
    __table_args__ = (UniqueConstraint("date", "slot", name="uq_plan_date_slot"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    date: Mapped[date] = mapped_column(Date, index=True)
    slot: Mapped[str] = mapped_column(String(32))  # subuh_story, siang_feed, malam_story, subuh_threads…
    pillar: Mapped[str | None] = mapped_column(String(32))
    series_id: Mapped[str | None] = mapped_column(String(64))
    series_part: Mapped[int | None] = mapped_column(Integer)
    series_total: Mapped[int | None] = mapped_column(Integer)
    format: Mapped[str | None] = mapped_column(String(16))
    hook_formula: Mapped[str | None] = mapped_column(String(32))
    bridge_options: Mapped[list[Any]] = mapped_column(JSON, default=list)
    bridge_feature: Mapped[str | None] = mapped_column(String(48))
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sky_moment: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="planned")  # planned|ready|fallback|published|failed|skipped


class DraftRow(Base):
    __tablename__ = "drafts"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("content_plan.id"), index=True)
    json: Mapped[dict[str, Any]] = mapped_column(JSON)
    check_report: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    judge_report: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(16))  # passed|failed|evergreen
    hook_index: Mapped[int | None] = mapped_column(Integer)
    media_keys: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_post_idem"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("drafts.id"))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("content_plan.id"))
    idempotency_key: Mapped[str] = mapped_column(String(96))
    platform: Mapped[str] = mapped_column(String(16))  # instagram|threads
    media_type: Mapped[str] = mapped_column(String(16))  # carousel|reel|story|text|chain
    platform_media_id: Mapped[str | None] = mapped_column(String(64))
    progress: Mapped[list[Any]] = mapped_column(JSON, default=list)  # published ids so far (chains)
    permalink: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    utm_campaign: Mapped[str | None] = mapped_column(String(96))
    sky_moment: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending|published|failed|dry_run
    error: Mapped[str | None] = mapped_column(Text)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # lease held by the publishing run (UTC)
    pillar: Mapped[str | None] = mapped_column(String(32))
    hook_formula: Mapped[str | None] = mapped_column(String(32))
    bridge_feature: Mapped[str | None] = mapped_column(String(48))
    slot: Mapped[str | None] = mapped_column(String(32))
    format: Mapped[str | None] = mapped_column(String(16))
    hook_text: Mapped[str | None] = mapped_column(Text)
    key_fact: Mapped[str | None] = mapped_column(Text)
    novelty_pct: Mapped[int | None] = mapped_column(Integer)


class Metric(Base):
    __tablename__ = "metrics"
    __table_args__ = (UniqueConstraint("post_id", "age_hours", name="uq_metric_age"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    age_hours: Mapped[int] = mapped_column(Integer)
    reach: Mapped[int] = mapped_column(Integer, default=0)
    saves: Mapped[int] = mapped_column(Integer, default=0)
    shares: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    follows: Mapped[int] = mapped_column(Integer, default=0)
    profile_visits: Mapped[int] = mapped_column(Integer, default=0)
    replies: Mapped[int] = mapped_column(Integer, default=0)
    reposts: Mapped[int] = mapped_column(Integer, default=0)
    views: Mapped[int] = mapped_column(Integer, default=0)


class Weight(Base):
    __tablename__ = "weights"
    __table_args__ = (UniqueConstraint("dimension", "key", name="uq_weight"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dimension: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(64))
    weight: Mapped[float] = mapped_column(Float)
    alpha: Mapped[float] = mapped_column(Float, default=1.0)
    beta: Mapped[float] = mapped_column(Float, default=1.0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EvergreenPost(Base):
    __tablename__ = "evergreen_bank"

    id: Mapped[int] = mapped_column(primary_key=True)
    json: Mapped[dict[str, Any]] = mapped_column(JSON)
    rendered_media_keys: Mapped[list[Any]] = mapped_column(JSON, default=list)
    pillar: Mapped[str] = mapped_column(String(32))
    topic_hash: Mapped[str] = mapped_column(String(16), index=True)
    hook_index: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired: Mapped[bool] = mapped_column(default=False)


class TopicHistory(Base):
    __tablename__ = "topic_history"

    id: Mapped[int] = mapped_column(primary_key=True)
    topic_hash: Mapped[str] = mapped_column(String(16), index=True)
    topic: Mapped[str] = mapped_column(Text)
    used_at: Mapped[date] = mapped_column(Date)


class SourceAudit(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    draft_id: Mapped[int] = mapped_column(ForeignKey("drafts.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    ref: Mapped[str] = mapped_column(String(128))
    text_hash: Mapped[str] = mapped_column(String(64))
    licence: Mapped[str] = mapped_column(String(64))


class DmLog(Base):
    __tablename__ = "dm_log"
    __table_args__ = (UniqueConstraint("user_id", "post_id", name="uq_dm_user_post"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    comment_id: Mapped[str] = mapped_column(String(64), unique=True)
    user_id: Mapped[str] = mapped_column(String(64))
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="sent")
    error: Mapped[str | None] = mapped_column(Text)


class JobRun(Base):
    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running|ok|failed|skipped
    summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class Token(Base):
    __tablename__ = "tokens"

    platform: Mapped[str] = mapped_column(String(16), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class Flag(Base):
    __tablename__ = "flags"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[str] = mapped_column(String(128), default="")
