"""`test-post`: one verified IG story + one Threads post, at most once per day, kill switch respected."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from samawi_bot.app import App
from samawi_bot.db import session_scope
from samawi_bot.db.models import Post
from samawi_bot.jobs import test_post
from samawi_bot.jobs.runner import JobSkipped
from samawi_bot.kill_switch import KillSwitchOn, set_switch


class FakeMeta:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append((req.method, path))
        if req.method == "GET" and req.url.params.get("fields") == "permalink":
            return httpx.Response(200, json={"permalink": f"https://example.test{path}"})
        if req.method == "GET":
            return httpx.Response(200, json={"status_code": "FINISHED", "status": "FINISHED"})
        if path.endswith("/media") or path.endswith("/threads"):
            return httpx.Response(200, json={"id": "container-1"})
        return httpx.Response(200, json={"id": "published-1"})


@pytest.fixture
def fake_story(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    rendered: list[Any] = []

    def render(app: App, spec: Any, sky: str, key: str) -> str:
        rendered.append((spec, sky, key))
        app.storage.put(key, b"png")
        return key

    monkeypatch.setattr(test_post, "render_story", render)
    return rendered


def test_posts_a_story_and_a_threads_post_once_per_day(make_app: Callable[..., App], fake_story: list[Any]) -> None:
    meta = FakeMeta()
    app = make_app(handler=meta)
    out = test_post.run_test_post(app)
    assert out["instagram"]["status"] == "published" and out["threads"]["status"] == "published"
    assert out["ayat"] and fake_story[0][1] == "isya" and fake_story[0][2].endswith("/test_story.png")
    assert ("POST", "/v23.0/17841400000000000/media") in meta.calls
    assert ("POST", "/v1.0/2600000000000000/threads") in meta.calls
    with session_scope(app.db) as s:
        assert {p.idempotency_key for p in s.query(Post)} == {f"test:{out['day']}:instagram", f"test:{out['day']}:threads"}
    before = len(meta.calls)
    again = test_post.run_test_post(app)
    assert again["instagram"]["status"] == again["threads"]["status"] == "already posted today"
    assert len(meta.calls) == before


def test_kill_switch_stops_it_before_any_call(make_app: Callable[..., App], fake_story: list[Any]) -> None:
    meta = FakeMeta()
    app = make_app(handler=meta)
    with session_scope(app.db) as s:
        set_switch(s, True, "test", app.now())
    with pytest.raises(KillSwitchOn):
        test_post.run_test_post(app)
    assert meta.calls == [] and fake_story == []
    assert not issubclass(KillSwitchOn, JobSkipped)  # surfaces as a failed run, never a silent skip


def test_long_ayat_falls_back_to_a_reference(make_app: Callable[..., App]) -> None:
    app = make_app()
    assert test_post.ayat_text_post(app, "94:5-6").startswith("Ayat hari ini:\n\n“")
    short = test_post.ayat_text_post(app, "2:282")
    assert short == "Ayat hari ini: (QS. Al-Baqarah: 282). Buka mushafmu dan baca pelan-pelan."
