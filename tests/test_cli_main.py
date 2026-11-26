"""`samawi-bot run <job>`, `migrate` and `admin` (spec §8.1)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, inspect

from samawi_bot import cli
from samawi_bot.app import App
from samawi_bot.db import session_scope
from samawi_bot.db.models import JobRun
from samawi_bot.jobs.runner import JobSkipped

# (job name, module, attribute) of every job the CLI can run
JOB_TARGETS = [
    ("plan", "samawi_bot.jobs.plan", "run_plan"),
    ("produce", "samawi_bot.jobs.produce", "run_produce"),
    ("produce-retry", "samawi_bot.jobs.produce", "run_produce"),
    ("bank", "samawi_bot.jobs.evergreen", "maintain_bank"),
    ("publish", "samawi_bot.jobs.publish", "publish_due"),
    ("publish-due", "samawi_bot.jobs.publish", "publish_due"),
    ("metrics", "samawi_bot.jobs.ops", "run_metrics"),
    ("learn", "samawi_bot.jobs.ops", "run_learn"),
    ("comments-dm", "samawi_bot.jobs.ops", "run_comments_dm"),
    ("tokens", "samawi_bot.jobs.ops", "run_tokens"),
    ("healthcheck", "samawi_bot.jobs.ops", "run_healthcheck"),
    ("evergreen", "samawi_bot.jobs.evergreen", "top_up"),
    ("refresh-sources", "samawi_bot.jobs.plan", "refresh_sources"),
    ("test-post", "samawi_bot.jobs.test_post", "run_test_post"),
]


def test_every_job_is_routed() -> None:
    assert sorted(j for j, _, _ in JOB_TARGETS) == sorted(cli.JOBS)


def test_every_scheduled_job_exists() -> None:
    """EventBridge runs `run <key>` for each schedule.json key: a key the CLI does not know would fail nightly."""
    import json

    schedule = json.loads((Path(__file__).parents[1] / "infra/terraform/schedule.json").read_text())
    assert set(schedule) <= set(cli.JOBS)
    assert schedule["produce-retry"]["cron"] == "cron(45 2,3 * * ? *)" and schedule["bank"]["cron"] == "cron(0 10 * * ? *)"


@pytest.mark.parametrize(("job", "module", "attr"), JOB_TARGETS)
def test_run_each_job(
    make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], job: str, module: str, attr: str
) -> None:
    app = make_app()
    calls: list[tuple[Any, ...]] = []

    def fake(*args: Any) -> dict[str, Any]:
        calls.append(args)
        return {"job": job}

    monkeypatch.setattr(f"{module}.{attr}", fake)
    assert cli.main(["run", job], app_factory=lambda: app) == 0
    assert len(calls) == 1 and calls[0][0] is app
    assert capsys.readouterr().out.strip().splitlines()[-1] == f"{job}: ok {{'job': '{job}'}}"
    with session_scope(app.db) as s:
        run = s.query(JobRun).one()
        assert (run.job, run.status, run.summary) == (job, "ok", {"job": job})


def test_job_arguments(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app()
    seen: dict[str, Any] = {}
    monkeypatch.setattr("samawi_bot.jobs.publish.publish_plan", lambda a, pid: seen.setdefault("plan_id", pid) and {})
    monkeypatch.setattr("samawi_bot.jobs.produce.run_produce", lambda a, day: seen.setdefault("day", day) and {})
    monkeypatch.setattr("samawi_bot.jobs.evergreen.top_up", lambda a, n: seen.setdefault("count", n) and {})
    assert cli.main(["run", "publish", "--plan-id", "42"], app_factory=lambda: app) == 0
    assert cli.main(["run", "produce", "--day", "2026-11-27"], app_factory=lambda: app) == 0
    assert cli.main(["run", "evergreen", "--count", "5"], app_factory=lambda: app) == 0
    assert seen == {"plan_id": 42, "day": date(2026, 11, 27), "count": 5}


def test_defaults(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app()
    seen: dict[str, Any] = {}
    monkeypatch.setattr("samawi_bot.jobs.produce.run_produce", lambda a, day: seen.update(day=day) or {})
    monkeypatch.setattr("samawi_bot.jobs.evergreen.top_up", lambda a, n: seen.update(count=n) or {})
    cli.main(["run", "produce"], app_factory=lambda: app)
    cli.main(["run", "evergreen"], app_factory=lambda: app)
    assert seen == {"day": None, "count": 30}


def test_failed_job_exits_1(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app()

    def boom(a: App) -> dict[str, Any]:
        raise RuntimeError("meta is down")

    monkeypatch.setattr("samawi_bot.jobs.ops.run_metrics", boom)
    assert cli.main(["run", "metrics"], app_factory=lambda: app) == 1
    assert capsys.readouterr().out.strip().splitlines()[-1] == "metrics: failed RuntimeError: meta is down"
    assert app.mailer.sent[-1][0] == "FAIL metrics"  # type: ignore[attr-defined]


def test_skipped_job_exits_0(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    app = make_app()

    def skip(a: App) -> dict[str, Any]:
        raise JobSkipped("kill switch")

    monkeypatch.setattr("samawi_bot.jobs.publish.publish_due", skip)
    assert cli.main(["run", "publish-due"], app_factory=lambda: app) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "publish-due: skipped {'reason': 'kill switch'}"


@pytest.mark.parametrize("argv", [[], ["run"], ["run", "nope"], ["run", "plan", "--plan-id", "x"]])
def test_bad_arguments_exit_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as info:
        cli.main(argv, app_factory=lambda: pytest.fail("no app should be built"))
    assert info.value.code == 2


def test_migrate_creates_every_table(make_app: Callable[..., App], tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'bot.db'}"
    app = make_app(database_url=url)
    assert cli.main(["migrate"], app_factory=lambda: app) == 0
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {
        "content_plan",
        "drafts",
        "posts",
        "metrics",
        "weights",
        "evergreen_bank",
        "topic_history",
        "sources",
        "dm_log",
        "job_runs",
        "tokens",
        "flags",
        "alembic_version",
    } <= tables
    # idempotent
    assert cli.main(["migrate"], app_factory=lambda: app) == 0


def test_admin_runs_uvicorn(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app()
    seen: dict[str, Any] = {}
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda web, **kw: seen.update(web=web, **kw))
    assert cli.main(["admin", "--port", "9999"], app_factory=lambda: app) == 0
    assert seen["host"] == "0.0.0.0" and seen["port"] == 9999
    paths = {getattr(r, "path", "") for r in seen["web"].routes}
    assert "/samawi/healthz" in paths
    assert cli.main(["admin"], app_factory=lambda: app) == 0
    assert seen["port"] == 3200


def test_default_factory_builds_from_settings(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch) -> None:
    app = make_app()
    sentinel = object()
    monkeypatch.setattr(cli, "get_settings", lambda: sentinel)
    monkeypatch.setattr(cli, "build_app", lambda s: app if s is sentinel else pytest.fail("wrong settings"))
    monkeypatch.setattr("samawi_bot.jobs.ops.run_tokens", lambda a: {"ok": a is app})
    assert cli.main(["run", "tokens"]) == 0


def test_http_client_loggers_never_log_request_urls(make_app: Callable[..., App], monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Meta GETs carry access_token in the URL; httpx logs URLs at INFO. Those loggers must be quiet."""
    import logging

    monkeypatch.setattr(cli, "job_fn", lambda name, args: lambda app: {})
    cli.main(["run", "metrics"], app_factory=lambda: make_app())
    for name in cli.HTTP_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING, name
    with caplog.at_level(logging.INFO):
        logging.getLogger("httpx").info("HTTP Request: GET https://graph.facebook.com/x?access_token=SECRET")
    assert "SECRET" not in caplog.text
