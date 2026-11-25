"""Rendered media storage. Meta fetches images and videos by URL, so each needs a public one.

S3 objects stay private; Meta gets a presigned URL valid for a few hours (or a CDN URL when
CDN_BASE_URL is set). Locally, files go to a directory and URLs are file:// (dry runs only).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

CONTENT_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".mp4": "video/mp4", ".json": "application/json"}


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def url(self, key: str) -> str: ...
    def exists(self, key: str) -> bool: ...


@dataclass
class LocalStorage:
    root: Path

    def put(self, key: str, data: bytes) -> None:
        p = self.root / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def get(self, key: str) -> bytes:
        return (self.root / key).read_bytes()

    def url(self, key: str) -> str:
        return (self.root / key).resolve().as_uri()

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()


@dataclass
class S3Storage:
    client: Any
    bucket: str
    cdn_base: str = ""
    expires: int = 6 * 3600

    def put(self, key: str, data: bytes) -> None:
        ctype = CONTENT_TYPES.get(Path(key).suffix, "application/octet-stream")
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=ctype)

    def get(self, key: str) -> bytes:
        return bytes(self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read())

    def url(self, key: str) -> str:
        if self.cdn_base:
            return f"{self.cdn_base.rstrip('/')}/{key}"
        return str(self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=self.expires))

    def exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:  # noqa: BLE001 — botocore ClientError for 404
            return False
