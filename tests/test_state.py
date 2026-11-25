"""S3-backed state that survives ephemeral ECS tasks (snapshot + hadith cache)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from samawi_bot import cli
from samawi_bot.publisher.storage import LocalStorage
from samawi_bot.state import pull, push


def test_push_then_pull_round_trips(tmp_path: Path) -> None:
    store = LocalStorage(tmp_path / "s3")
    src = tmp_path / "hadith"
    (src / "hadeethenc").mkdir(parents=True)
    (src / "hadeethenc" / "1.json").write_text('{"id": "1"}')
    assert push(store, "hadith", src)
    dest = tmp_path / "restored"
    dest.mkdir()
    (dest / "stale.txt").write_text("old")
    assert pull(store, "hadith", dest)
    assert (dest / "hadeethenc" / "1.json").read_text() == '{"id": "1"}'
    assert not (dest / "stale.txt").exists()


def test_push_missing_directory_and_pull_without_archive(tmp_path: Path) -> None:
    store = LocalStorage(tmp_path / "s3")
    assert not push(store, "snapshot", tmp_path / "nope")
    target = tmp_path / "keep"
    target.mkdir()
    (target / "x").write_text("x")
    assert not pull(store, "snapshot", target)
    assert (target / "x").read_text() == "x"


def test_cli_restores_and_persists_only_with_a_bucket(make_app: Any, tmp_path: Path, monkeypatch: Any) -> None:
    calls: list[str] = []
    monkeypatch.setattr("samawi_bot.state.pull", lambda storage, name, d: calls.append(f"pull:{name}") or True)
    monkeypatch.setattr("samawi_bot.state.push", lambda storage, name, d: calls.append(f"push:{name}") or True)
    monkeypatch.setattr(cli, "job_fn", lambda name, args: lambda app: {"ok": 1})
    app = make_app(s3_bucket="bucket")
    assert cli.main(["run", "plan"], app_factory=lambda: app) == 0
    assert calls == ["pull:snapshot", "pull:hadith", "push:snapshot", "push:hadith"]
    calls.clear()
    assert cli.main(["run", "metrics"], app_factory=lambda: app) == 0
    assert calls == ["pull:snapshot", "pull:hadith"]
    calls.clear()
    assert cli.main(["run", "plan"], app_factory=lambda: make_app()) == 0
    assert calls == []


def test_cli_prints_heartbeat(make_app: Any, monkeypatch: Any, capsys: Any) -> None:
    monkeypatch.setattr(cli, "job_fn", lambda name, args: lambda app: {})
    cli.main(["run", "publish-due"], app_factory=lambda: make_app())
    assert "SAMAWI_HEARTBEAT job=publish-due status=ok" in capsys.readouterr().out
