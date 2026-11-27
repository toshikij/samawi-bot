"""build_app wiring, Settings, the database helpers and migrations."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws
from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import StaticPool

from conftest import SNAPSHOT, make_settings
from samawi_bot.alerts import LogMailer, SesMailer
from samawi_bot.app import App, build_app
from samawi_bot.brain.llm import ClaudeLLM
from samawi_bot.config import LaunchMode, Settings, TranslationMode, get_settings
from samawi_bot.db import create_all, make_engine, make_sessionmaker, session_scope
from samawi_bot.db.migrate import config as alembic_config
from samawi_bot.db.migrate import upgrade
from samawi_bot.db.models import Flag
from samawi_bot.publisher.instagram import Instagram
from samawi_bot.publisher.storage import LocalStorage, S3Storage
from samawi_bot.publisher.threads import Threads
from samawi_bot.sources import Sources

REGION = "ap-southeast-3"


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield


def built(**overrides: object) -> App:
    return build_app(make_settings(**overrides))


# ── build_app ────────────────────────────────────────────────────────────────────────────────


def test_build_app_local(aws: None) -> None:
    app = built()
    try:
        assert isinstance(app.storage, LocalStorage)
        assert app.storage.root == Path("out/media")
        assert isinstance(app.mailer, LogMailer)
        assert app.scheduler is None
        assert app.http.headers["User-Agent"] == "samawi-bot/1.0"
        assert app.http.timeout.read == 60
        import time

        assert app.sleep is time.sleep
        assert app.now().tzinfo is not None
        assert str(app.db.kw["bind"].url) == "sqlite://"
        llm = app.llm_factory()
        assert isinstance(llm, ClaudeLLM)
        assert llm.model == "claude-opus-5-5"
        assert llm.client.max_retries == 3
        assert llm.client.api_key == "test"
    finally:
        app.http.close()


def test_build_app_s3_ses_scheduler(aws: None) -> None:
    boto3.client("s3", region_name=REGION).create_bucket(Bucket="samawi-media", CreateBucketConfiguration={"LocationConstraint": REGION})
    app = built(
        s3_bucket="samawi-media",
        cdn_base_url="https://cdn.example.com/",
        email_transport="ses",
        scheduler_role_arn="arn:aws:iam::123456789012:role/scheduler",
        claude_model="claude-test-model",
        aws_region=REGION,
    )
    try:
        assert isinstance(app.storage, S3Storage)
        assert app.storage.bucket == "samawi-media"
        app.storage.put("posts/a.png", b"png")
        assert app.storage.get("posts/a.png") == b"png"
        assert app.storage.url("posts/a.png") == "https://cdn.example.com/posts/a.png"
        assert isinstance(app.mailer, SesMailer)
        assert app.mailer.sender == "alerts@example.com" and app.mailer.to == "owner@example.com"
        assert app.scheduler is not None and app.scheduler.meta.service_model.service_name == "scheduler"
        assert app.scheduler.meta.region_name == REGION
        llm = app.llm_factory()
        assert isinstance(llm, ClaudeLLM) and llm.model == "claude-test-model" and llm.writer_model == "claude-haiku-5-5"
    finally:
        app.http.close()


def test_build_app_without_api_key_lets_sdk_read_env(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    app = built(anthropic_api_key="")
    try:
        llm = app.llm_factory()
        assert isinstance(llm, ClaudeLLM) and llm.client.api_key == "from-env"
    finally:
        app.http.close()


def test_build_app_sources_factory(aws: None, hadith_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, Path]] = []
    real = Sources.load

    def spy(snapshot: Path, hadith: Path) -> Sources:
        calls.append((snapshot, hadith))
        return real(snapshot, hadith_dir)

    monkeypatch.setattr(Sources, "load", staticmethod(spy))
    app = built()
    try:
        assert app.hadith_dir == SNAPSHOT.parent / "hadith"
        first = app.sources
        assert app.sources is first  # loaded once
        assert calls == [(SNAPSHOT, SNAPSHOT.parent / "hadith")]
    finally:
        app.http.close()


def test_app_platform_clients(make_app: object) -> None:
    app = make_app()  # type: ignore[operator]
    ig = app.instagram("tok")
    assert isinstance(ig, Instagram)
    assert ig.ig_user_id == "17841400000000000"
    assert ig.graph.base == "https://graph.test/v23.0" and ig.graph.token == "tok"
    th = app.threads("tok2")
    assert isinstance(th, Threads)
    assert th.user_id == "2600000000000000" and th.graph.base == "https://threads.test/v1.0"
    assert make_app(threads_user_id="").threads("t").user_id == "me"  # type: ignore[operator]


def test_app_defaults() -> None:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    app = App(
        settings=make_settings(),
        db=make_sessionmaker(engine),
        storage=LocalStorage(Path("/nonexistent")),
        mailer=LogMailer(),
        http=None,  # type: ignore[arg-type]
        llm_factory=lambda: None,  # type: ignore[arg-type,return-value]
        sources_factory=lambda: None,  # type: ignore[arg-type,return-value]
    )
    before = datetime.now(UTC)
    assert before <= app.now() <= datetime.now(UTC)
    assert app.sleep(5) is None
    assert app.scheduler is None


# ── Settings ─────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def test_settings_defaults(clean_env: None) -> None:
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.claude_model == "claude-opus-5-5"  # the judges
    assert s.claude_writer_model == "claude-haiku-5-5"  # the writer, checked by the judges
    assert s.anthropic_api_key == ""
    assert s.launch_mode is LaunchMode.PRELAUNCH
    assert s.translation_mode is TranslationMode.KEMENAG
    assert s.kill_switch is False
    assert s.publish_live is False
    assert s.play_store_url == ""
    assert s.email_transport == "log"
    assert s.database_url == "sqlite:///./samawi-bot.db"
    assert s.graph_base_url == "https://graph.facebook.com/v23.0"
    assert s.threads_base_url == "https://graph.threads.net/v1.0"
    assert s.aws_region == "ap-southeast-3"
    assert s.snapshot_dir == Path("snapshot")
    assert s.admin_base_path == "/samawi"
    assert s.dashboard_url == "http://localhost:3200/samawi"
    assert s.scheduler_role_arn == "" and s.s3_bucket == ""
    for secret in ("ig_access_token", "threads_access_token", "meta_app_secret", "samawi_repo_token"):
        assert getattr(s, secret) == ""


def test_settings_from_env(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAUNCH_MODE", "live")
    monkeypatch.setenv("KILL_SWITCH", "true")
    monkeypatch.setenv("TRANSLATION_MODE", "makna")
    monkeypatch.setenv("PUBLISH_LIVE", "1")
    monkeypatch.setenv("SOMETHING_ELSE", "ignored")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.launch_mode is LaunchMode.LIVE and s.kill_switch is True and s.publish_live is True
    assert s.translation_mode is TranslationMode.MAKNA


def test_settings_env_file(clean_env: None, tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("ALERT_EMAIL=me@example.com\nS3_BUCKET=b\n")
    s = Settings(_env_file=env)  # type: ignore[call-arg]
    assert s.alert_email == "me@example.com" and s.s3_bucket == "b"


def test_get_settings_is_cached(clean_env: None) -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


# ── db ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("postgres://u:p@db.example.com:5432/samawi", "postgresql+psycopg://u:p@db.example.com:5432/samawi"),
        ("postgresql://u:p@db.example.com/samawi", "postgresql+psycopg://u:p@db.example.com/samawi"),
        ("postgresql+psycopg://u:p@h/d", "postgresql+psycopg://u:p@h/d"),
        ("sqlite://", "sqlite://"),
    ],
)
def test_make_engine_rewrites_urls(url: str, expected: str) -> None:
    engine = make_engine(url)
    assert engine.url.render_as_string(hide_password=False) == expected
    assert engine.pool._pre_ping is True  # type: ignore[attr-defined]


def memory_db() -> object:
    engine = create_engine("sqlite://", poolclass=StaticPool)
    create_all(engine)
    return make_sessionmaker(engine)


def test_session_scope_commits() -> None:
    factory = memory_db()
    now = datetime(2026, 11, 25, tzinfo=UTC)
    with session_scope(factory) as s:  # type: ignore[arg-type]
        s.add(Flag(name="a", value="1", updated_at=now))
    with session_scope(factory) as s:  # type: ignore[arg-type]
        f = s.get(Flag, "a")
        assert f is not None and f.value == "1"
    # expire_on_commit=False: objects stay readable after the scope closes
    with session_scope(factory) as s:  # type: ignore[arg-type]
        row = s.get(Flag, "a")
    assert row is not None and row.value == "1"


def test_session_scope_rolls_back_and_reraises() -> None:
    factory = memory_db()
    now = datetime(2026, 11, 25, tzinfo=UTC)
    with pytest.raises(RuntimeError, match="boom"), session_scope(factory) as s:  # type: ignore[arg-type]
        s.add(Flag(name="b", value="1", updated_at=now))
        s.flush()
        raise RuntimeError("boom")
    with session_scope(factory) as s:  # type: ignore[arg-type]
        assert s.get(Flag, "b") is None


def test_migrate_config_escapes_percent() -> None:
    cfg = alembic_config("postgresql+psycopg://u:p%40ss@h/d")
    assert cfg.get_main_option("sqlalchemy.url") == "postgresql+psycopg://u:p%40ss@h/d"
    assert cfg.get_main_option("script_location", "").endswith("migrations")


def test_upgrade_head_matches_models(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'm.db'}"
    upgrade(url)
    migrated = inspect(create_engine(url))
    fresh_engine = create_engine("sqlite://")
    create_all(fresh_engine)
    fresh = inspect(fresh_engine)
    assert set(fresh.get_table_names()) <= set(migrated.get_table_names())
    for table in fresh.get_table_names():
        assert {c["name"] for c in fresh.get_columns(table)} == {c["name"] for c in migrated.get_columns(table)}, table


def test_s3_presigned_urls_use_the_regional_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """ap-southeast-3 is an opt-in region: global-endpoint presigned URLs return 400 to Meta."""
    from samawi_bot.app import s3_client

    # Signing is offline; fake credentials keep the test independent of the machine's AWS setup.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIATESTTESTTESTTEST")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test-secret")
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    url = s3_client("ap-southeast-3").generate_presigned_url("get_object", Params={"Bucket": "samawi-test-bucket", "Key": "k.png"}, ExpiresIn=60)
    assert url.startswith("https://samawi-test-bucket.s3.ap-southeast-3.amazonaws.com/k.png?")
    assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
