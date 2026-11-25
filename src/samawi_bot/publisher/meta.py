"""Graph API calls shared by Instagram and Threads, with retries on transient errors only."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

TRANSIENT_CODES = frozenset({1, 2, 4, 17, 32, 341, 613})
TOKEN_CODES = frozenset({102, 190, 463, 467})


class MetaApiError(RuntimeError):
    def __init__(self, status: int, code: int | None, message: str, subcode: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.subcode = subcode

    @property
    def retryable(self) -> bool:
        return self.status >= 500 or self.status == 429 or (self.code in TRANSIENT_CODES)

    @property
    def token_problem(self) -> bool:
        return self.code in TOKEN_CODES or self.status == 401


Sleep = Callable[[float], None]


@dataclass
class Graph:
    client: httpx.Client
    base: str
    token: str
    sleep: Sleep = time.sleep
    attempts: int = 3
    base_delay: float = 2.0

    def call(self, method: str, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        params = {**(params or {}), "access_token": self.token}
        last: Exception | None = None
        for i in range(self.attempts):
            try:
                return self._once(method, path, params)
            except MetaApiError as exc:
                last = exc
                if not exc.retryable:
                    raise
            except httpx.TransportError as exc:
                last = MetaApiError(0, None, f"network error calling {path}: {exc}")
            if i < self.attempts - 1:
                self.sleep(self.base_delay * (2**i))
        assert last is not None
        raise last

    def _once(self, method: str, path: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base}{path}"
        if method == "POST":
            resp = self.client.post(url, data=params, timeout=60)
        else:
            resp = self.client.request(method, url, params=params, timeout=60)
        try:
            body = resp.json() if resp.content else {}
        except ValueError:
            raise MetaApiError(resp.status_code, None, f"{method} {path}: non-JSON response (HTTP {resp.status_code})") from None
        if not isinstance(body, dict):
            raise MetaApiError(resp.status_code, None, f"{method} {path}: unexpected response shape")
        if resp.status_code >= 400 or "error" in body:
            err = body.get("error") or {}
            msg = err.get("error_user_msg") or err.get("message") or f"HTTP {resp.status_code}"
            raise MetaApiError(resp.status_code, err.get("code"), f"Meta API {method} {path}: {msg}", err.get("error_subcode"))
        return body

    def get(self, path: str, **params: str) -> dict[str, Any]:
        return self.call("GET", path, params)

    def post(self, path: str, **params: str) -> dict[str, Any]:
        return self.call("POST", path, params)

    def delete(self, path: str) -> dict[str, Any]:
        return self.call("DELETE", path, {})


class ContainerError(MetaApiError):
    pass


def wait_for_container(graph: Graph, container_id: str, field: str, max_polls: int = 24, interval: float = 5.0) -> None:
    """Poll a media container until FINISHED. ERROR / EXPIRED, or a timeout, raise ContainerError."""
    fields = "status_code,status" if field == "status_code" else "status,error_message"
    for _ in range(max_polls):
        res = graph.get(f"/{container_id}", fields=fields)
        status = res.get(field)
        if status in ("FINISHED", "PUBLISHED"):
            return
        if status in ("ERROR", "EXPIRED"):
            raise ContainerError(400, None, f"container {container_id} {status}: {res.get('error_message') or res.get('status') or 'no detail'}")
        graph.sleep(interval)
    raise ContainerError(408, None, f"container {container_id} still IN_PROGRESS after {max_polls} polls")
