"""Shared fixtures. No test touches the network: HTTP goes through httpx.MockTransport or respx,
Claude through FakeLLM, AWS through moto, and the clock is fixed."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from samawi_bot.alerts import LogMailer
from samawi_bot.app import App
from samawi_bot.brain.llm import LLMError
from samawi_bot.brain.models import Draft, JudgeReport
from samawi_bot.checks.draft_checks import CheckContext
from samawi_bot.config import LaunchMode, Settings
from samawi_bot.db import create_all, make_sessionmaker
from samawi_bot.publisher.storage import LocalStorage
from samawi_bot.sources import Sources

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
SNAPSHOT = ROOT / "snapshot"
TODAY = date(2026, 11, 26)  # a Thursday
NOW = datetime(2026, 11, 25, 19, 0, tzinfo=UTC)  # 02:00 WIB on 26 Nov


@pytest.fixture(scope="session")
def hadith_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("hadith")
    shutil.copytree(FIXTURES / "hadith", d, dirs_exist_ok=True)
    return d


@pytest.fixture(scope="session")
def sources(hadith_dir: Path) -> Sources:
    return Sources.load(SNAPSHOT, hadith_dir)


def load_draft(**overrides: Any) -> Draft:
    data = json.loads((FIXTURES / "good_draft.json").read_text("utf-8"))
    data.update(overrides)
    return Draft.model_validate(data)


@pytest.fixture
def draft() -> Draft:
    return load_draft()


@pytest.fixture
def ctx() -> CheckContext:
    return CheckContext(today=TODAY, launch_mode=LaunchMode.PRELAUNCH)


def judge_report(draft: Draft, **overrides: Any) -> JudgeReport:
    data: dict[str, Any] = {
        "claims": [{"index": i, "verdict": "supported", "note": ""} for i in range(len(draft.claims))],
        "contested_framing": False,
        "contested_note": "",
        "hooks": [{"index": i, "curiosity_gap": 9, "specificity": 9, "payoff_honesty": 9, "thumb_stop": 9 - (i % 2)} for i in range(len(draft.hooks))],
        "novelty_pct": 75,
        "accessibility": 9,
        "payoff_by_slide_3": True,
        "tone_ok": True,
        "notes": "",
    }
    data.update(overrides)
    return JudgeReport.model_validate(data)


class FakeLLM:
    """Returns queued responses per schema name; an Exception in the queue is raised."""

    def __init__(self, responses: dict[str, list[Any]] | None = None):
        self.responses = responses or {}
        self.calls: list[tuple[str, str, str]] = []
        self.roles: list[tuple[str, str]] = []

    def structured(self, system: str, user: str, schema: type[BaseModel], effort: str = "high", role: str = "judge") -> Any:
        self.calls.append((schema.__name__, system, user))
        self.roles.append((schema.__name__, role))
        queue = self.responses.get(schema.__name__) or []
        if not queue:
            raise LLMError(f"FakeLLM has no {schema.__name__} response queued")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(user)
        return item


def make_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "anthropic_api_key": "test",
        "ig_user_id": "17841400000000000",
        "ig_access_token": "IG-TOKEN",
        "threads_user_id": "2600000000000000",
        "threads_access_token": "TH-TOKEN",
        "meta_app_id": "123",
        "meta_app_secret": "secret",
        "database_url": "sqlite://",
        "alert_email": "owner@example.com",
        "alert_from": "alerts@example.com",
        "snapshot_dir": SNAPSHOT,
        "session_secret": "test-secret",
        "publish_live": True,
        "graph_base_url": "https://graph.test/v23.0",
        "threads_base_url": "https://threads.test/v1.0",
        "hadeethenc_base_url": "https://hadeethenc.test/api/v1",
        "fawaz_hadith_base_url": "https://fawaz.test",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture
def make_app(tmp_path: Path, sources: Sources, clock: Clock) -> Iterator[Callable[..., App]]:
    """Build an App over in-memory SQLite, temp storage, a LogMailer and a given HTTP handler."""
    clients: list[httpx.Client] = []

    def build(handler: Handler | None = None, llm: FakeLLM | None = None, **settings: Any) -> App:
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        create_all(engine)
        client = httpx.Client(transport=httpx.MockTransport(handler or (lambda r: httpx.Response(404, json={}))))
        clients.append(client)
        fake = llm or FakeLLM()
        return App(
            settings=make_settings(**settings),
            db=make_sessionmaker(engine),
            storage=LocalStorage(tmp_path / "media"),
            mailer=LogMailer(),
            http=client,
            llm_factory=lambda: fake,
            sources_factory=lambda: sources,
            now=clock,
        )

    yield build
    for c in clients:
        c.close()
