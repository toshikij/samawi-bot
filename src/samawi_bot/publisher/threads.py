"""Threads API: text posts, image carousels, reply chains that resume instead of duplicating."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..checks.text import count_chars
from .meta import Graph, wait_for_container

MAX_CHARS = 500
CAROUSEL_MIN, CAROUSEL_MAX = 2, 20


@dataclass
class Threads:
    graph: Graph
    user_id: str = "me"

    def _publish(self, creation_id: str) -> str:
        return str(self.graph.post(f"/{self.user_id}/threads_publish", creation_id=creation_id)["id"])

    def text(self, text: str, reply_to: str | None = None) -> str:
        if count_chars(text) > MAX_CHARS:
            raise ValueError(f"Threads post is {count_chars(text)} characters; the cap is {MAX_CHARS}")
        params = {"media_type": "TEXT", "text": text}
        if reply_to:
            params["reply_to_id"] = reply_to
        c = self.graph.post(f"/{self.user_id}/threads", **params)
        wait_for_container(self.graph, c["id"], "status")
        return self._publish(c["id"])

    def image(self, text: str, image_url: str, reply_to: str | None = None) -> str:
        params = {"media_type": "IMAGE", "image_url": image_url, "text": text}
        if reply_to:
            params["reply_to_id"] = reply_to
        c = self.graph.post(f"/{self.user_id}/threads", **params)
        wait_for_container(self.graph, c["id"], "status")
        return self._publish(c["id"])

    def carousel(self, text: str, image_urls: list[str], reply_to: str | None = None) -> str:
        if not CAROUSEL_MIN <= len(image_urls) <= CAROUSEL_MAX:
            raise ValueError(f"A Threads carousel needs {CAROUSEL_MIN}–{CAROUSEL_MAX} items, got {len(image_urls)}")
        children = []
        for url in image_urls:
            c = self.graph.post(f"/{self.user_id}/threads", media_type="IMAGE", image_url=url, is_carousel_item="true")
            wait_for_container(self.graph, c["id"], "status")
            children.append(c["id"])
        params = {"media_type": "CAROUSEL", "children": ",".join(children), "text": text}
        if reply_to:
            params["reply_to_id"] = reply_to
        parent = self.graph.post(f"/{self.user_id}/threads", **params)
        wait_for_container(self.graph, parent["id"], "status")
        return self._publish(parent["id"])

    def chain(
        self,
        posts: list[str],
        published: list[str],
        on_published: Callable[[int, str], None],
        first_images: list[str] | None = None,
        before_each: Callable[[], None] | None = None,
    ) -> list[str]:
        """Each post replies to the previous one. Resumes after `published`; never reposts."""
        ids = list(published)
        for i in range(len(ids), len(posts)):
            if before_each:
                before_each()
            reply_to = ids[i - 1] if i else None
            if i == 0 and first_images:
                pid = self.carousel(posts[0], first_images) if len(first_images) >= 2 else self.image(posts[0], first_images[0])
            else:
                pid = self.text(posts[i], reply_to)
            on_published(i, pid)
            ids.append(pid)
        return ids

    def permalink(self, post_id: str) -> str | None:
        try:
            return self.graph.get(f"/{post_id}", fields="permalink").get("permalink")
        except Exception:  # noqa: BLE001
            return None

    def delete(self, post_id: str) -> None:
        self.graph.delete(f"/{post_id}")

    def publishing_limit(self) -> tuple[int, int, int, int]:
        """(posts used, posts total, replies used, replies total) over 24 h."""
        res = self.graph.get(f"/{self.user_id}/threads_publishing_limit", fields="quota_usage,config,reply_quota_usage,reply_config")
        d = (res.get("data") or [{}])[0]
        return (
            int(d.get("quota_usage", 0)),
            int((d.get("config") or {}).get("quota_total", 250)),
            int(d.get("reply_quota_usage", 0)),
            int((d.get("reply_config") or {}).get("quota_total", 1000)),
        )

    def insights(self, post_id: str) -> dict[str, int]:
        res = self.graph.get(f"/{post_id}/insights", metric="views,likes,replies,reposts,quotes,shares")
        out: dict[str, int] = {}
        for d in res.get("data", []):
            total = d.get("total_value") or {}
            values = d.get("values") or [{}]
            out[d["name"]] = int(total.get("value", values[0].get("value", 0)) or 0)
        return out

    def me(self) -> dict[str, str]:
        return {k: str(v) for k, v in self.graph.get("/me", fields="id,username").items()}
