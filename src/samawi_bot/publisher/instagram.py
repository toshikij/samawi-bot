"""Instagram Graph API publishing: carousel, reel, story; insights; comments; private replies."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .meta import Graph, wait_for_container

CAROUSEL_MIN, CAROUSEL_MAX = 2, 10


@dataclass
class Published:
    media_id: str
    permalink: str | None


@dataclass
class Instagram:
    graph: Graph
    ig_user_id: str

    def _permalink(self, media_id: str) -> str | None:
        try:
            return self.graph.get(f"/{media_id}", fields="permalink").get("permalink")
        except Exception:  # noqa: BLE001 — a missing permalink never fails a published post
            return None

    def _publish(self, creation_id: str) -> Published:
        media_id = str(self.graph.post(f"/{self.ig_user_id}/media_publish", creation_id=creation_id)["id"])
        return Published(media_id, self._permalink(media_id))

    def carousel(self, image_urls: list[str], caption: str) -> Published:
        if not CAROUSEL_MIN <= len(image_urls) <= CAROUSEL_MAX:
            raise ValueError(f"A carousel needs {CAROUSEL_MIN}–{CAROUSEL_MAX} images, got {len(image_urls)}")
        children = []
        for url in image_urls:
            c = self.graph.post(f"/{self.ig_user_id}/media", image_url=url, is_carousel_item="true")
            wait_for_container(self.graph, c["id"], "status_code")
            children.append(c["id"])
        parent = self.graph.post(f"/{self.ig_user_id}/media", media_type="CAROUSEL", children=",".join(children), caption=caption)
        wait_for_container(self.graph, parent["id"], "status_code")
        return self._publish(parent["id"])

    def reel(self, video_url: str, caption: str, cover_url: str | None = None) -> Published:
        params = {"media_type": "REELS", "video_url": video_url, "caption": caption, "share_to_feed": "true"}
        if cover_url:
            params["cover_url"] = cover_url
        c = self.graph.post(f"/{self.ig_user_id}/media", **params)
        wait_for_container(self.graph, c["id"], "status_code", max_polls=60)
        return self._publish(c["id"])

    def story(self, image_url: str) -> Published:
        c = self.graph.post(f"/{self.ig_user_id}/media", image_url=image_url, media_type="STORIES")
        wait_for_container(self.graph, c["id"], "status_code")
        return self._publish(c["id"])

    def single_image(self, image_url: str, caption: str) -> Published:
        c = self.graph.post(f"/{self.ig_user_id}/media", image_url=image_url, caption=caption)
        wait_for_container(self.graph, c["id"], "status_code")
        return self._publish(c["id"])

    def delete(self, media_id: str) -> None:
        self.graph.delete(f"/{media_id}")

    def publishing_limit(self) -> tuple[int, int]:
        """(used, total) over the rolling 24 h."""
        res = self.graph.get(f"/{self.ig_user_id}/content_publishing_limit", fields="quota_usage,config")
        data = (res.get("data") or [{}])[0]
        total = int((data.get("config") or {}).get("quota_total", 50))
        return int(data.get("quota_usage", 0)), total

    def insights(self, media_id: str, metrics: list[str]) -> dict[str, int]:
        res = self.graph.get(f"/{media_id}/insights", metric=",".join(metrics))
        out: dict[str, int] = {}
        for d in res.get("data", []):
            total = d.get("total_value") or {}
            values = d.get("values") or [{}]
            out[d["name"]] = int(total.get("value", values[0].get("value", 0)) or 0)
        return out

    def account(self) -> dict[str, Any]:
        return self.graph.get(f"/{self.ig_user_id}", fields="username,followers_count,media_count")

    def comments(self, media_id: str) -> list[dict[str, Any]]:
        res = self.graph.get(f"/{media_id}/comments", fields="id,text,timestamp,from{id,username}", limit="100")
        return list(res.get("data", []))

    def private_reply(self, comment_id: str, text: str) -> None:
        """Send-API private reply to a comment (needs instagram_manage_messages)."""
        import json

        self.graph.post(f"/{self.ig_user_id}/messages", recipient=json.dumps({"comment_id": comment_id}), message=json.dumps({"text": text}))
