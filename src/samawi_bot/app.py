"""Wires settings into the objects every job uses. Tests build an `App` from fakes instead."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.orm import Session, sessionmaker

from .alerts import LogMailer, Mailer, SesMailer
from .brain.llm import LLM
from .config import Settings
from .db import make_engine, make_sessionmaker
from .publisher.instagram import Instagram
from .publisher.meta import Graph
from .publisher.storage import LocalStorage, S3Storage, Storage
from .publisher.threads import Threads
from .sources import Sources


@dataclass
class App:
    settings: Settings
    db: sessionmaker[Session]
    storage: Storage
    mailer: Mailer
    http: httpx.Client
    llm_factory: Callable[[], LLM]
    sources_factory: Callable[[], Sources]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    sleep: Callable[[float], None] = field(default=lambda s: None)
    scheduler: Any = None  # boto3 "scheduler" client, for one-off publish schedules
    _sources: Sources | None = None

    @property
    def sources(self) -> Sources:
        if self._sources is None:
            self._sources = self.sources_factory()
        return self._sources

    def instagram(self, token: str) -> Instagram:
        return Instagram(Graph(self.http, self.settings.graph_base_url, token, sleep=self.sleep), self.settings.ig_user_id)

    def threads(self, token: str) -> Threads:
        return Threads(Graph(self.http, self.settings.threads_base_url, token, sleep=self.sleep), self.settings.threads_user_id or "me")

    @property
    def hadith_dir(self) -> Path:
        return self.settings.snapshot_dir.parent / "hadith"


def s3_client(region: str) -> Any:
    """Regional endpoint + SigV4. Jakarta (ap-southeast-3) is an opt-in region: a presigned URL on
    the global s3.amazonaws.com endpoint returns 400, so Meta could not fetch the media."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
    )


def build_app(settings: Settings) -> App:
    import time

    import anthropic
    import boto3

    from .brain.llm import ClaudeLLM

    engine = make_engine(settings.database_url)
    storage: Storage
    if settings.s3_bucket:
        storage = S3Storage(s3_client(settings.aws_region), settings.s3_bucket, settings.cdn_base_url)
    else:
        storage = LocalStorage(Path("out/media"))
    mailer: Mailer
    if settings.email_transport == "ses":
        mailer = SesMailer(boto3.client("ses", region_name=settings.aws_region), settings.alert_from, settings.alert_email)
    else:
        mailer = LogMailer()
    hadith_dir = settings.snapshot_dir.parent / "hadith"
    return App(
        settings=settings,
        db=make_sessionmaker(engine),
        storage=storage,
        mailer=mailer,
        http=httpx.Client(timeout=60, headers={"User-Agent": "samawi-bot/1.0"}),
        llm_factory=lambda: ClaudeLLM(anthropic.Anthropic(api_key=settings.anthropic_api_key or None, max_retries=3, timeout=600), model=settings.claude_model),
        sources_factory=lambda: Sources.load(settings.snapshot_dir, hadith_dir),
        sleep=time.sleep,
        scheduler=boto3.client("scheduler", region_name=settings.aws_region) if settings.scheduler_role_arn else None,
    )
