"""Snapshot of the app-repo assets the bot depends on, with checksums (spec §8, §10.3).

The bot never reads the app repo live while producing. `plan` refreshes a local snapshot
(`refresh_snapshot`), and every loader goes through `Snapshot.read_*`, which verifies the file
against the manifest's SHA-256 first. A mismatch or missing file raises `SnapshotIntegrityError`
and production stops: the bot never falls back to LLM text for scripture.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

QURAN_TEXT = "app/assets/quran/quran-uthmani.txt"
TRANSLATION_KEMENAG = "app/assets/quran/translation-id-kemenag.json"
DUAS = "app/assets/duas/duas.json"
FEATURES = "app/lib/core/entitlement/samawi_features.dart"
SKY_PALETTE = "app/lib/core/theme/sky_palette.dart"
CITIES = "app/lib/features/prayer/domain/indonesian_city.dart"
SURAHS = "app/lib/features/quran/domain/surahs.dart"
FONT_AMIRI = "app/assets/fonts/AmiriQuran-Regular.ttf"
FONT_JAKARTA = "app/assets/fonts/PlusJakartaSans-Variable.ttf"
LOGO_BRASS = "app/assets/brand/3.0x/symbol-brass.png"
LOGO_WHITE = "app/assets/brand/3.0x/symbol-white.png"


SNAPSHOT_FILES: tuple[str, ...] = (
    QURAN_TEXT,
    TRANSLATION_KEMENAG,
    DUAS,
    FEATURES,
    SKY_PALETTE,
    CITIES,
    SURAHS,
    FONT_AMIRI,
    FONT_JAKARTA,
    LOGO_BRASS,
    LOGO_WHITE,
)

MANIFEST = "manifest.json"


class SnapshotIntegrityError(RuntimeError):
    """A snapshot file is missing, unlisted, or does not match its checksum. Production must stop."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    root: Path

    def manifest(self) -> dict[str, Any]:
        path = self.root / MANIFEST
        if not path.is_file():
            raise SnapshotIntegrityError(f"No snapshot manifest at {path}")
        try:
            data = json.loads(path.read_text("utf-8"))
        except json.JSONDecodeError as exc:
            raise SnapshotIntegrityError(f"Snapshot manifest is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
            raise SnapshotIntegrityError("Snapshot manifest has no 'files' map")
        return data

    def read_bytes(self, rel: str) -> bytes:
        files: dict[str, str] = self.manifest()["files"]
        expected = files.get(rel)
        if expected is None:
            raise SnapshotIntegrityError(f"{rel} is not in the snapshot manifest")
        path = self.root / rel
        if not path.is_file():
            raise SnapshotIntegrityError(f"{rel} is missing from the snapshot")
        data = path.read_bytes()
        actual = sha256_bytes(data)
        if actual != expected:
            raise SnapshotIntegrityError(f"{rel} checksum mismatch: manifest {expected[:12]}…, file {actual[:12]}…")
        return data

    def read_text(self, rel: str) -> str:
        return self.read_bytes(rel).decode("utf-8")

    def path(self, rel: str) -> Path:
        """A verified on-disk path (fonts and logos are handed to Chromium by path)."""
        self.read_bytes(rel)
        return (self.root / rel).resolve()

    def verify_all(self) -> int:
        files: dict[str, str] = self.manifest()["files"]
        missing = [f for f in SNAPSHOT_FILES if f not in files]
        if missing:
            raise SnapshotIntegrityError(f"Snapshot is missing required files: {missing[:5]}")
        for rel in files:
            self.read_bytes(rel)
        return len(files)


def write_snapshot(root: Path, contents: Iterable[tuple[str, bytes]], ref: str, now: datetime | None = None) -> Snapshot:
    """Write files and a fresh manifest atomically enough for a single-writer job."""
    root.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {}
    for rel, data in contents:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        files[rel] = sha256_bytes(data)
    manifest = {
        "ref": ref,
        "fetched_at": (now or datetime.now(UTC)).isoformat(),
        "files": dict(sorted(files.items())),
    }
    tmp = root / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1), "utf-8")
    tmp.replace(root / MANIFEST)
    return Snapshot(root)


def snapshot_from_checkout(checkout: Path, root: Path, ref: str = "local") -> Snapshot:
    """Build a snapshot from a local clone of the app repo (used for the image build and tests)."""
    return write_snapshot(root, ((rel, (checkout / rel).read_bytes()) for rel in SNAPSHOT_FILES), ref)


Fetcher = Callable[[str], bytes]


def github_fetcher(client: httpx.Client, repo: str, ref: str, token: str) -> tuple[Fetcher, str]:
    """A fetcher for raw file bytes at the commit `ref` resolves to, plus that commit SHA."""
    headers = {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
    commit = client.get(
        f"https://api.github.com/repos/{repo}/commits/{ref}",
        headers={**headers, "Accept": "application/vnd.github+json"},
    )
    commit.raise_for_status()
    sha = str(commit.json()["sha"])

    def fetch(rel: str) -> bytes:
        resp = client.get(
            f"https://api.github.com/repos/{repo}/contents/{rel}",
            params={"ref": sha},
            headers={**headers, "Accept": "application/vnd.github.raw"},
        )
        resp.raise_for_status()
        return resp.content

    return fetch, sha


def refresh_snapshot(root: Path, fetch: Fetcher, ref: str) -> Snapshot:
    """Download every required file, then verify the new snapshot end to end."""
    snap = write_snapshot(root, ((rel, fetch(rel)) for rel in SNAPSHOT_FILES), ref)
    snap.verify_all()
    return snap
