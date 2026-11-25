"""A small fake Meta server (Instagram Graph + Threads) for contract-style publishing tests.

It keeps state the way Meta does: containers with a status sequence, published media, quota
usage, and errors to inject on cue. It validates request shapes (access_token present, the
right media_type / children / creation_id / reply_to_id) and answers like the real API, so the
code under test has to talk to it correctly to get anywhere.

Cassettes under tests/fixtures/meta/ record the exact request shapes; `check_cassette` compares
against them (set RECORD_META_CASSETTES=1 to rewrite them after an intentional change).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx

CASSETTES = Path(__file__).resolve().parent / "fixtures" / "meta"
IG_USER = "17841400000000000"
TH_USER = "2600000000000000"
VALID_TOKENS = {"IG-TOKEN", "TH-TOKEN", "IG-NEW", "TH-NEW"}


@dataclass
class Req:
    method: str
    host: str
    path: str  # with the API version stripped, for routing
    params: dict[str, str]
    raw_path: str = ""

    def shape(self) -> dict[str, Any]:
        """What a cassette records: everything but the secret's value."""
        params = {k: v for k, v in sorted(self.params.items()) if k != "access_token"}
        return {"method": self.method, "url": f"https://{self.host}{self.raw_path}", "params": params}


@dataclass
class Container:
    id: str
    api: str  # "ig" | "threads"
    params: dict[str, str]
    statuses: list[str]
    polls: int = 0
    media_id: str | None = None

    @property
    def status(self) -> str:
        return self.statuses[min(self.polls, len(self.statuses) - 1)]


@dataclass
class Fault:
    method: str
    pattern: re.Pattern[str]
    result: httpx.Response | BaseException | Callable[[], httpx.Response]
    times: int
    when: Callable[[Req], bool] | None = None


def error(status: int, code: int | None, message: str = "boom", subcode: int | None = None, **extra: Any) -> httpx.Response:
    err: dict[str, Any] = {"message": message, "type": "OAuthException", **extra}
    if code is not None:
        err["code"] = code
    if subcode is not None:
        err["error_subcode"] = subcode
    return httpx.Response(status, json={"error": err})


@dataclass
class FakeMeta:
    ig_user: str = IG_USER
    th_user: str = TH_USER
    default_statuses: list[str] = field(default_factory=lambda: ["IN_PROGRESS", "FINISHED"])
    ig_quota: dict[str, Any] = field(default_factory=lambda: {"quota_usage": 0, "config": {"quota_total": 50, "quota_duration": 86400}})
    th_quota: dict[str, Any] = field(
        default_factory=lambda: {"quota_usage": 0, "config": {"quota_total": 250}, "reply_quota_usage": 0, "reply_config": {"quota_total": 1000}}
    )
    requests: list[Req] = field(default_factory=list)
    containers: dict[str, Container] = field(default_factory=dict)
    published: dict[str, Container] = field(default_factory=dict)
    faults: list[Fault] = field(default_factory=list)
    next_statuses: list[list[str]] = field(default_factory=list)
    insights: dict[str, Any] = field(default_factory=dict)
    comments: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    refresh_response: dict[str, Any] = field(default_factory=lambda: {"access_token": "IG-NEW", "token_type": "bearer", "expires_in": 5184000})
    threads_refresh_response: dict[str, Any] = field(default_factory=lambda: {"access_token": "TH-NEW", "token_type": "bearer", "expires_in": 5184000})
    on_publish: Callable[[str, Container], None] | None = None
    on_request: Callable[[Req], None] | None = None
    _n: int = 0

    # ---- control ---------------------------------------------------------------------------
    def fail(
        self,
        method: str,
        path: str,
        result: httpx.Response | BaseException | Callable[[], httpx.Response],
        times: int = 1,
        when: Callable[[Req], bool] | None = None,
    ) -> None:
        """Make the next `times` matching requests fail with `result` (a response or an exception)."""
        self.faults.append(Fault(method, re.compile(path), result, times, when))

    def statuses_for_next(self, *seqs: list[str]) -> None:
        self.next_statuses.extend(seqs)

    def calls(self, method: str | None = None, path: str | None = None) -> list[Req]:
        return [r for r in self.requests if (method is None or r.method == method) and (path is None or re.fullmatch(path, r.path))]

    @property
    def ig_published(self) -> list[Container]:
        return [c for c in self.published.values() if c.api == "ig"]

    @property
    def threads_published(self) -> list[Container]:
        return [c for c in self.published.values() if c.api == "threads"]

    # ---- transport -------------------------------------------------------------------------
    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        if request.method == "POST":
            params.update(parse_qsl(request.content.decode()))
        host = request.url.host
        path = request.url.path
        for prefix in ("/v23.0", "/v1.0"):
            if path.startswith(prefix + "/"):
                path = path[len(prefix) :]
        req = Req(request.method, host, path, params, request.url.path)
        self.requests.append(req)
        if self.on_request:
            self.on_request(req)
        for f in self.faults:
            if f.times > 0 and f.method == req.method and f.pattern.fullmatch(req.path) and (f.when is None or f.when(req)):
                f.times -= 1
                if isinstance(f.result, BaseException):
                    raise f.result
                return f.result if isinstance(f.result, httpx.Response) else f.result()
        token = params.get("access_token")
        if not token:
            return error(400, 190, "An active access token must be used to query information about the current user.")
        if token not in VALID_TOKENS:
            return error(400, 190, "Error validating access token: Session has expired", subcode=463)
        if host == "graph.test":
            return self._graph(req)
        if host == "threads.test":
            return self._threads(req)
        return error(404, 100, f"unknown host {host}")

    def _new_container(self, api: str, params: dict[str, str]) -> Container:
        self._n += 1
        statuses = self.next_statuses.pop(0) if self.next_statuses else list(self.default_statuses)
        c = Container(f"{api}_c{self._n}", api, {k: v for k, v in params.items() if k != "access_token"}, statuses)
        self.containers[c.id] = c
        return c

    def _poll(self, c: Container, fields: str) -> httpx.Response:
        status = c.status
        c.polls += 1
        if c.api == "ig":
            assert fields == "status_code,status", fields
            body: dict[str, Any] = {"status_code": status, "id": c.id}
            if status == "ERROR":
                body["status"] = "Error: Media upload has failed with error code 2207026"
            return httpx.Response(200, json=body)
        assert fields == "status,error_message", fields
        body = {"status": status, "id": c.id}
        if status == "ERROR":
            body["error_message"] = "FAILED_PROCESSING_IMAGE"
        return httpx.Response(200, json=body)

    def _publish(self, api: str, creation_id: str | None) -> httpx.Response:
        c = self.containers.get(creation_id or "")
        if c is None or c.api != api:
            return error(400, 100, "Invalid parameter", subcode=2207006)
        if c.params.get("is_carousel_item") == "true":
            return error(400, 100, "Carousel items cannot be published on their own")
        if c.status != "FINISHED":
            return error(400, 9007, "Media ID is not available", subcode=2207027)
        if c.media_id is not None:
            return error(400, 100, "Container already published", subcode=2207008)
        self._n += 1
        c.media_id = f"{api}_m{self._n}"
        self.published[c.media_id] = c
        if self.on_publish:
            self.on_publish(c.media_id, c)
        return httpx.Response(200, json={"id": c.media_id})

    def _graph(self, req: Req) -> httpx.Response:
        p, m, q = req.path, req.method, req.params
        u = self.ig_user
        if m == "POST" and p == f"/{u}/media":
            if q.get("media_type") == "CAROUSEL":
                kids = [self.containers.get(k) for k in q.get("children", "").split(",")]
                if not 2 <= len(kids) <= 10 or any(k is None or k.params.get("is_carousel_item") != "true" or k.status != "FINISHED" for k in kids):
                    return error(400, 100, "Invalid children")
            elif q.get("media_type") == "REELS":
                if not q.get("video_url"):
                    return error(400, 100, "video_url is required")
            elif q.get("media_type") not in (None, "STORIES") or not q.get("image_url"):
                return error(400, 100, "image_url is required")
            return httpx.Response(200, json={"id": self._new_container("ig", q).id})
        if m == "POST" and p == f"/{u}/media_publish":
            return self._publish("ig", q.get("creation_id"))
        if m == "POST" and p == f"/{u}/messages":
            recipient, message = json.loads(q["recipient"]), json.loads(q["message"])
            assert set(recipient) == {"comment_id"} and set(message) == {"text"}
            return httpx.Response(200, json={"recipient_id": "1789", "message_id": "mid.1"})
        if m == "GET" and p == f"/{u}/content_publishing_limit":
            assert q.get("fields") == "quota_usage,config"
            return httpx.Response(200, json={"data": [self.ig_quota]})
        if m == "GET" and p == f"/{u}":
            return httpx.Response(200, json={"username": "samawi.indonesia", "followers_count": 1234, "media_count": 56, "id": u})
        if m == "GET" and p == "/oauth/access_token":
            return httpx.Response(200, json=self.refresh_response)
        if m == "GET" and p.endswith("/insights"):
            return httpx.Response(200, json=self.insights.get(p.split("/")[1], {"data": []}))
        if m == "GET" and p.endswith("/comments"):
            return httpx.Response(200, json={"data": self.comments.get(p.split("/")[1], [])})
        return self._common(req, "ig")

    def _threads(self, req: Req) -> httpx.Response:
        p, m, q = req.path, req.method, req.params
        u = self.th_user
        if m == "POST" and p == f"/{u}/threads":
            kind = q.get("media_type")
            if kind == "TEXT":
                if not q.get("text") or len(q["text"]) > 500:
                    return error(400, 100, "text is required and at most 500 characters")
            elif kind == "IMAGE":
                if not q.get("image_url"):
                    return error(400, 100, "image_url is required")
            elif kind == "CAROUSEL":
                kids = [self.containers.get(k) for k in q.get("children", "").split(",")]
                if not 2 <= len(kids) <= 20 or any(k is None or k.params.get("is_carousel_item") != "true" for k in kids):
                    return error(400, 100, "Invalid children")
            else:
                return error(400, 100, f"bad media_type {kind}")
            if "reply_to_id" in q and q["reply_to_id"] not in self.published:
                return error(400, 100, "reply_to_id does not exist")
            return httpx.Response(200, json={"id": self._new_container("threads", q).id})
        if m == "POST" and p == f"/{u}/threads_publish":
            return self._publish("threads", q.get("creation_id"))
        if m == "GET" and p == f"/{u}/threads_publishing_limit":
            return httpx.Response(200, json={"data": [self.th_quota]})
        if m == "GET" and p == "/refresh_access_token":
            assert q.get("grant_type") == "th_refresh_token"
            return httpx.Response(200, json=self.threads_refresh_response)
        if m == "GET" and p == "/me":
            return httpx.Response(200, json={"id": u, "username": "samawi.indonesia"})
        if m == "GET" and p.endswith("/insights"):
            return httpx.Response(200, json=self.insights.get(p.split("/")[1], {"data": []}))
        return self._common(req, "threads")

    def _common(self, req: Req, api: str) -> httpx.Response:
        obj = req.path.strip("/")
        if "/" in obj:
            return error(404, 100, f"Unknown path {req.path}")
        if req.method == "DELETE":
            if obj in self.published:
                del self.published[obj]
                return httpx.Response(200, json={"success": True})
            return error(400, 100, "Object does not exist")
        fields = req.params.get("fields", "")
        if obj in self.containers and fields != "permalink":
            return self._poll(self.containers[obj], fields)
        if obj in self.published and fields == "permalink":
            host = "www.instagram.com/p" if api == "ig" else "www.threads.net/@samawi.indonesia/post"
            return httpx.Response(200, json={"permalink": f"https://{host}/{obj}/", "id": obj})
        return error(400, 100, f"Unsupported get request. Object with ID '{obj}' does not exist")


def check_cassette(name: str, requests: list[Req]) -> None:
    """Compare the request shapes against tests/fixtures/meta/<name>.json."""
    for r in requests:
        assert r.params.get("access_token"), f"{r.method} {r.path} sent without access_token"
    actual = [r.shape() for r in requests]
    path = CASSETTES / f"{name}.json"
    if os.environ.get("RECORD_META_CASSETTES"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"name": name, "requests": actual}, indent=2, ensure_ascii=False) + "\n", "utf-8")
    expected = json.loads(path.read_text("utf-8"))["requests"]
    assert actual == expected


class Sleeps(list[float]):
    """A fake sleep that records the delays it was asked for."""

    def __call__(self, seconds: float) -> None:
        self.append(seconds)
