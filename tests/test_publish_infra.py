"""Media storage (local + S3 via moto), alert mailers (SES via moto), the job runner, and the
EventBridge one-off publish schedules."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import select

from conftest import NOW, TODAY
from samawi_bot.alerts import LogMailer, SesMailer, safe_send
from samawi_bot.app import App
from samawi_bot.db import session_scope
from samawi_bot.db.models import ContentPlan, JobRun
from samawi_bot.jobs.runner import JobSkipped, _jsonable, run_job
from samawi_bot.jobs.schedule import schedule_day
from samawi_bot.publisher.slots import WIB
from samawi_bot.publisher.storage import LocalStorage, S3Storage

REGION = "us-east-1"


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield


# ---- storage ----------------------------------------------------------------------------------


def test_local_storage_roundtrip(tmp_path: Path) -> None:
    st = LocalStorage(tmp_path / "media")
    assert not st.exists("posts/a/b.png")
    st.put("posts/a/b.png", b"\x89PNG")
    assert st.exists("posts/a/b.png")
    assert st.get("posts/a/b.png") == b"\x89PNG"
    url = st.url("posts/a/b.png")
    assert url.startswith("file://") and url.endswith("/media/posts/a/b.png")
    st.put("posts/a/b.png", b"new")
    assert st.get("posts/a/b.png") == b"new"


def test_local_storage_directory_is_not_a_file(tmp_path: Path) -> None:
    st = LocalStorage(tmp_path)
    st.put("posts/x.png", b"1")
    assert not st.exists("posts")


@pytest.fixture
def s3(aws: None) -> Any:
    client = boto3.client("s3", region_name=REGION)
    client.create_bucket(Bucket="samawi-media")
    return client


@pytest.mark.parametrize(
    "key,ctype",
    [
        ("posts/1.png", "image/png"),
        ("posts/1.jpg", "image/jpeg"),
        ("posts/reel.mp4", "video/mp4"),
        ("posts/payload.json", "application/json"),
        ("posts/blob.bin", "application/octet-stream"),
        ("posts/noext", "application/octet-stream"),
    ],
)
def test_s3_put_sets_content_type(s3: Any, key: str, ctype: str) -> None:
    st = S3Storage(s3, "samawi-media")
    st.put(key, b"data")
    assert s3.head_object(Bucket="samawi-media", Key=key)["ContentType"] == ctype
    assert st.get(key) == b"data"


def test_s3_exists(s3: Any) -> None:
    st = S3Storage(s3, "samawi-media")
    assert not st.exists("posts/missing.png")
    st.put("posts/here.png", b"x")
    assert st.exists("posts/here.png")


def test_s3_exists_false_when_bucket_missing(s3: Any) -> None:
    assert not S3Storage(s3, "no-such-bucket").exists("k.png")


def _expires_in(url: str) -> float:
    q = parse_qs(urlparse(url).query)
    if "X-Amz-Expires" in q:  # SigV4
        return float(q["X-Amz-Expires"][0])
    return float(q["Expires"][0]) - time.time()  # SigV2: an absolute epoch


def test_s3_presigned_url(s3: Any) -> None:
    st = S3Storage(s3, "samawi-media")
    st.put("posts/2026-11-26/siang_feed/01.png", b"x")
    url = st.url("posts/2026-11-26/siang_feed/01.png")
    u = urlparse(url)
    assert u.scheme == "https"
    assert "samawi-media" in u.netloc + u.path
    assert u.path.endswith("/posts/2026-11-26/siang_feed/01.png")
    assert "Signature" in u.query
    assert abs(_expires_in(url) - 6 * 3600) < 60


def test_s3_presigned_url_custom_expiry(s3: Any) -> None:
    assert abs(_expires_in(S3Storage(s3, "samawi-media", expires=600).url("k.png")) - 600) < 60


@pytest.mark.parametrize("cdn", ["https://cdn.samawi.test", "https://cdn.samawi.test/"])
def test_s3_cdn_url(s3: Any, cdn: str) -> None:
    assert S3Storage(s3, "samawi-media", cdn_base=cdn).url("posts/a.png") == "https://cdn.samawi.test/posts/a.png"


# ---- alerts -----------------------------------------------------------------------------------


def _ses_sent() -> list[Any]:
    from moto.core import DEFAULT_ACCOUNT_ID
    from moto.ses.models import ses_backends

    return list(ses_backends[DEFAULT_ACCOUNT_ID][REGION].sent_messages)


def test_ses_mailer_sends(aws: None) -> None:
    client = boto3.client("ses", region_name=REGION)
    client.verify_email_identity(EmailAddress="alerts@example.com")
    SesMailer(client, "alerts@example.com", "owner@example.com").send("FAIL produce", "body text")
    sent = _ses_sent()
    assert len(sent) == 1
    msg = sent[0]
    assert msg.source == "alerts@example.com"
    assert msg.destinations["ToAddresses"] == ["owner@example.com"]
    assert msg.subject == "[SAMAWI] FAIL produce"
    assert msg.body == "body text"


def test_ses_subject_is_capped(aws: None) -> None:
    client = boto3.client("ses", region_name=REGION)
    client.verify_email_identity(EmailAddress="alerts@example.com")
    SesMailer(client, "alerts@example.com", "owner@example.com").send("x" * 500, "b")
    assert len(_ses_sent()[0].subject) == 200


def test_ses_unverified_sender_is_swallowed_by_safe_send(aws: None, caplog: pytest.LogCaptureFixture) -> None:
    client = boto3.client("ses", region_name=REGION)
    mailer = SesMailer(client, "unverified@example.com", "owner@example.com")
    with caplog.at_level(logging.ERROR, "samawi.alerts"):
        assert safe_send(mailer, "subj", "body") is False
    assert "could not send alert subj" in caplog.text
    assert _ses_sent() == []


def test_log_mailer_records_and_logs(caplog: pytest.LogCaptureFixture) -> None:
    m = LogMailer()
    with caplog.at_level(logging.WARNING, "samawi.alerts"):
        assert safe_send(m, "S", "B") is True
    assert m.sent == [("S", "B")]
    assert "ALERT S" in caplog.text


def test_safe_send_swallows_mailer_exception() -> None:
    class Broken:
        def send(self, subject: str, body: str) -> None:
            raise ConnectionError("smtp down")

    assert safe_send(Broken(), "s", "b") is False


# ---- runner -----------------------------------------------------------------------------------


@dataclass
class Thing:
    n: int

    def __str__(self) -> str:
        return f"Thing({self.n})"


def _runs(app: App) -> list[JobRun]:
    with session_scope(app.db) as s:
        rows = list(s.scalars(select(JobRun)))
        for r in rows:
            s.expunge(r)
        return rows


def test_run_job_ok_json_safe_summary(make_app: Callable[..., App]) -> None:
    app = make_app()
    summary = {"day": date(2026, 11, 26), "at": NOW, "slots": {"b", "a"}, "obj": Thing(3), "nested": {1: (1, 2.5, None, True)}}
    run = run_job(app, "produce", lambda a: summary)
    assert run.status == "ok" and run.error is None
    assert run.summary["day"] == "2026-11-26"
    assert run.summary["at"] == str(NOW)
    assert sorted(run.summary["slots"]) == ["a", "b"]
    assert run.summary["obj"] == "Thing(3)"
    assert run.summary["nested"] == {"1": [1, 2.5, None, True]}
    json.dumps(run.summary)
    assert run.finished_at is not None
    (stored,) = _runs(app)
    assert stored.status == "ok" and stored.summary == run.summary
    assert app.mailer.sent == []  # type: ignore[attr-defined]


def test_run_job_none_summary_is_empty(make_app: Callable[..., App]) -> None:
    app = make_app()
    assert run_job(app, "x", lambda a: None).summary == {}  # type: ignore[arg-type, return-value]


def test_run_job_skipped_not_alerted(make_app: Callable[..., App]) -> None:
    app = make_app()

    def fn(a: App) -> dict[str, Any]:
        raise JobSkipped("kill switch is on")

    run = run_job(app, "publish", fn)
    assert run.status == "skipped" and run.summary == {"reason": "kill switch is on"} and run.error is None
    assert app.mailer.sent == []  # type: ignore[attr-defined]


def test_run_job_failure_alerts_and_records(make_app: Callable[..., App]) -> None:
    app = make_app()

    def fn(a: App) -> dict[str, Any]:
        raise ValueError("bad thing")

    run = run_job(app, "metrics", fn)
    assert run.status == "failed" and run.error == "ValueError: bad thing"
    ((subject, body),) = app.mailer.sent  # type: ignore[attr-defined]
    assert subject == "FAIL metrics"
    assert "ValueError: bad thing" in body and "Traceback" in body
    assert _runs(app)[0].status == "failed"


def test_run_job_failure_with_broken_mailer_still_recorded(make_app: Callable[..., App]) -> None:
    app = make_app()

    class Broken:
        def send(self, subject: str, body: str) -> None:
            raise RuntimeError("ses down")

    app.mailer = Broken()

    def fn(a: App) -> dict[str, Any]:
        raise KeyError("k")

    assert run_job(app, "x", fn).status == "failed"


def test_jsonable_scalars() -> None:
    assert _jsonable([1, "a", 1.5, False, None]) == [1, "a", 1.5, False, None]
    assert _jsonable(Path("/x")) == "/x"


# ---- schedule_day (EventBridge Scheduler) -----------------------------------------------------

SCHED = {
    "ecs_cluster_arn": "arn:aws:ecs:us-east-1:123456789012:cluster/samawi",
    "scheduler_role_arn": "arn:aws:iam::123456789012:role/samawi-scheduler",
    "ecs_task_definition_arn": "arn:aws:ecs:us-east-1:123456789012:task-definition/samawi:3",
    "ecs_subnets": "subnet-a,subnet-b,",
    "ecs_security_groups": "sg-1",
}


def _plan(app: App, slot: str, at: datetime | None, day: date = TODAY) -> int:
    with session_scope(app.db) as s:
        p = ContentPlan(date=day, slot=slot, scheduled_at=at, status="ready")
        s.add(p)
        s.flush()
        return p.id


@pytest.fixture
def scheduler(aws: None) -> Any:
    client = boto3.client("scheduler", region_name=REGION)
    client.create_schedule_group(Name="samawi")
    return client


def test_schedule_day_creates_future_slots_only(make_app: Callable[..., App], scheduler: Any) -> None:
    app = make_app(**SCHED)
    app.scheduler = scheduler
    _plan(app, "subuh_story", NOW - timedelta(minutes=1))  # already past
    _plan(app, "subuh_threads", NOW)  # exactly now counts as past
    feed = _plan(app, "siang_feed", datetime(2026, 11, 26, 5, 30))  # naive = UTC (SQLite)
    _plan(app, "malam_story", None)
    _plan(app, "siang_feed", NOW + timedelta(hours=1), day=TODAY + timedelta(days=1))  # another day
    made = schedule_day(app, TODAY)
    assert made == ["samawi-20261126-siang-feed"]
    got = scheduler.get_schedule(Name="samawi-20261126-siang-feed", GroupName="samawi")
    assert got["ScheduleExpression"] == "at(2026-11-26T05:30:00)"
    assert got["ScheduleExpressionTimezone"] == "UTC"
    assert got["FlexibleTimeWindow"] == {"Mode": "OFF"}
    assert got["ActionAfterCompletion"] == "DELETE"
    t = got["Target"]
    assert t["Arn"] == SCHED["ecs_cluster_arn"] and t["RoleArn"] == SCHED["scheduler_role_arn"]
    assert json.loads(t["Input"]) == {"containerOverrides": [{"name": "pipeline", "command": ["run", "publish", "--plan-id", str(feed)]}]}
    ecs = t["EcsParameters"]
    assert ecs["TaskDefinitionArn"] == SCHED["ecs_task_definition_arn"] and ecs["LaunchType"] == "FARGATE"
    assert ecs["NetworkConfiguration"]["awsvpcConfiguration"] == {"Subnets": ["subnet-a", "subnet-b"], "SecurityGroups": ["sg-1"], "AssignPublicIp": "ENABLED"}
    assert t["RetryPolicy"] == {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 900}


def test_schedule_day_wib_slot_expressed_in_utc(make_app: Callable[..., App], scheduler: Any) -> None:
    # plan.py stores slot times as UTC (SQLite would drop a +07:00 offset); the expression is UTC.
    app = make_app(**SCHED)
    app.scheduler = scheduler
    _plan(app, "malam_threads", datetime(2026, 11, 26, 20, 15, tzinfo=WIB).astimezone(UTC))
    assert schedule_day(app, TODAY) == ["samawi-20261126-malam-threads"]
    got = scheduler.get_schedule(Name="samawi-20261126-malam-threads", GroupName="samawi")
    assert got["ScheduleExpression"] == "at(2026-11-26T13:15:00)"


def test_schedule_day_conflict_updates(make_app: Callable[..., App], scheduler: Any) -> None:
    app = make_app(**SCHED)
    app.scheduler = scheduler
    pid = _plan(app, "siang_feed", datetime(2026, 11, 26, 5, 30))
    assert schedule_day(app, TODAY) == ["samawi-20261126-siang-feed"]
    with session_scope(app.db) as s:
        p = s.get(ContentPlan, pid)
        assert p is not None
        p.scheduled_at = datetime(2026, 11, 26, 5, 45)
    assert schedule_day(app, TODAY) == ["samawi-20261126-siang-feed"]
    got = scheduler.get_schedule(Name="samawi-20261126-siang-feed", GroupName="samawi")
    assert got["ScheduleExpression"] == "at(2026-11-26T05:45:00)"
    assert len(scheduler.list_schedules(GroupName="samawi")["Schedules"]) == 1


class FakeScheduler:
    class exceptions:
        class ConflictException(Exception):
            pass

    def __init__(self) -> None:
        self.created: dict[str, dict[str, Any]] = {}
        self.updated: list[dict[str, Any]] = []

    def create_schedule(self, **kw: Any) -> None:
        if kw["Name"] in self.created:
            raise self.exceptions.ConflictException(kw["Name"])
        self.created[kw["Name"]] = kw

    def update_schedule(self, **kw: Any) -> None:
        self.updated.append(kw)


def test_schedule_day_fake_client_conflict_keeps_delete_after_completion(make_app: Callable[..., App]) -> None:
    app = make_app(**SCHED, scheduler_group="samawi-test")
    fake = FakeScheduler()
    app.scheduler = fake
    _plan(app, "kahfi_story", datetime(2026, 11, 26, 11, 20))
    schedule_day(app, TODAY)
    schedule_day(app, TODAY)
    assert list(fake.created) == ["samawi-20261126-kahfi-story"]
    (upd,) = fake.updated
    assert upd["ActionAfterCompletion"] == "DELETE" and upd["GroupName"] == "samawi-test"
    assert upd["ScheduleExpression"] == "at(2026-11-26T11:20:00)"
    assert upd == fake.created["samawi-20261126-kahfi-story"]


def test_schedule_day_no_scheduler(make_app: Callable[..., App]) -> None:
    app = make_app(**SCHED)
    _plan(app, "siang_feed", NOW + timedelta(hours=3))
    assert schedule_day(app, TODAY) == []


def test_schedule_day_no_cluster(make_app: Callable[..., App]) -> None:
    app = make_app()
    app.scheduler = FakeScheduler()
    _plan(app, "siang_feed", NOW + timedelta(hours=3))
    assert schedule_day(app, TODAY) == []
    assert app.scheduler.created == {}


def test_schedule_day_empty_day(make_app: Callable[..., App]) -> None:
    app = make_app(**SCHED)
    app.scheduler = FakeScheduler()
    assert schedule_day(app, TODAY) == []


# ---- migration 0002: posts.claimed_at ---------------------------------------------------------


def test_migration_0002_adds_and_drops_claimed_at(tmp_path: Path) -> None:
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from samawi_bot.db.migrate import config as alembic_config
    from samawi_bot.db.migrate import upgrade

    url = f"sqlite:///{tmp_path / 'm.db'}"

    def columns() -> set[str]:
        engine = create_engine(url)
        try:
            return {c["name"] for c in inspect(engine).get_columns("posts")}
        finally:
            engine.dispose()

    upgrade(url)
    assert "claimed_at" in columns()
    command.downgrade(alembic_config(url), "0001")
    cols = columns()
    assert "claimed_at" not in cols and {"idempotency_key", "status", "progress"} <= cols
    upgrade(url)
    assert "claimed_at" in columns()
