"""Snapshot integrity (spec §8, §10.3): a missing or corrupt snapshot stops production."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from conftest import SNAPSHOT
from samawi_bot.sources import Sources
from samawi_bot.sources.snapshot import (
    FONT_AMIRI,
    MANIFEST,
    QURAN_TEXT,
    SNAPSHOT_FILES,
    SURAHS,
    Snapshot,
    SnapshotIntegrityError,
    github_fetcher,
    refresh_snapshot,
    sha256_bytes,
    snapshot_from_checkout,
    write_snapshot,
)


@pytest.fixture
def snap_copy(tmp_path: Path) -> Path:
    dest = tmp_path / "snap"
    shutil.copytree(SNAPSHOT, dest)
    return dest


def _manifest(root: Path) -> dict:
    return json.loads((root / MANIFEST).read_text("utf-8"))


def _write_manifest(root: Path, data: object) -> None:
    (root / MANIFEST).write_text(json.dumps(data), "utf-8")


def test_real_snapshot_verifies_every_required_file() -> None:
    snap = Snapshot(SNAPSHOT)
    assert snap.verify_all() == len(SNAPSHOT_FILES)
    assert set(snap.manifest()["files"]) == set(SNAPSHOT_FILES)


def test_sha256_bytes() -> None:
    assert sha256_bytes(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_read_text_and_path_are_verified(snap_copy: Path) -> None:
    snap = Snapshot(snap_copy)
    assert snap.read_text(SURAHS).startswith(snap_copy.joinpath(SURAHS).read_text("utf-8")[:50])
    p = snap.path(FONT_AMIRI)
    assert p.is_absolute() and p.is_file()
    assert p == (snap_copy / FONT_AMIRI).resolve()


def test_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(SnapshotIntegrityError, match="No snapshot manifest"):
        Snapshot(tmp_path).manifest()
    with pytest.raises(SnapshotIntegrityError):
        Snapshot(tmp_path).read_text(QURAN_TEXT)


def test_invalid_json_manifest(snap_copy: Path) -> None:
    (snap_copy / MANIFEST).write_text("{not json", "utf-8")
    with pytest.raises(SnapshotIntegrityError, match="not valid JSON"):
        Snapshot(snap_copy).verify_all()


@pytest.mark.parametrize("data", [[], {"ref": "x"}, {"files": []}, {"files": "abc"}, "files"])
def test_manifest_without_files_map(snap_copy: Path, data: object) -> None:
    _write_manifest(snap_copy, data)
    with pytest.raises(SnapshotIntegrityError, match="no 'files' map"):
        Snapshot(snap_copy).manifest()


def test_file_not_in_manifest(snap_copy: Path) -> None:
    m = _manifest(snap_copy)
    del m["files"][QURAN_TEXT]
    _write_manifest(snap_copy, m)
    snap = Snapshot(snap_copy)
    with pytest.raises(SnapshotIntegrityError, match="not in the snapshot manifest"):
        snap.read_text(QURAN_TEXT)
    with pytest.raises(SnapshotIntegrityError, match="not in the snapshot manifest"):
        snap.read_bytes("app/unknown.txt")


def test_missing_file(snap_copy: Path) -> None:
    (snap_copy / QURAN_TEXT).unlink()
    with pytest.raises(SnapshotIntegrityError, match="missing from the snapshot"):
        Snapshot(snap_copy).read_text(QURAN_TEXT)
    with pytest.raises(SnapshotIntegrityError, match="missing from the snapshot"):
        Snapshot(snap_copy).verify_all()


def test_checksum_mismatch_single_byte_tamper(snap_copy: Path) -> None:
    path = snap_copy / QURAN_TEXT
    data = bytearray(path.read_bytes())
    data[100] ^= 0x01
    path.write_bytes(bytes(data))
    snap = Snapshot(snap_copy)
    with pytest.raises(SnapshotIntegrityError, match="checksum mismatch"):
        snap.read_text(QURAN_TEXT)
    with pytest.raises(SnapshotIntegrityError, match="checksum mismatch"):
        snap.verify_all()
    with pytest.raises(SnapshotIntegrityError, match="checksum mismatch"):
        Sources.load(snap_copy, snap_copy / "hadith")


def test_checksum_mismatch_on_path(snap_copy: Path) -> None:
    (snap_copy / FONT_AMIRI).write_bytes(b"not a font")
    with pytest.raises(SnapshotIntegrityError, match="checksum mismatch"):
        Snapshot(snap_copy).path(FONT_AMIRI)


def test_verify_all_requires_every_snapshot_file(snap_copy: Path) -> None:
    for rel in SNAPSHOT_FILES:
        m = _manifest(snap_copy)
        del m["files"][rel]
        _write_manifest(snap_copy, m)
        with pytest.raises(SnapshotIntegrityError, match="missing required files"):
            Snapshot(snap_copy).verify_all()
        shutil.copy(SNAPSHOT / MANIFEST, snap_copy / MANIFEST)


def test_verify_all_checks_extra_listed_files(snap_copy: Path) -> None:
    m = _manifest(snap_copy)
    m["files"]["extra.txt"] = sha256_bytes(b"extra")
    _write_manifest(snap_copy, m)
    with pytest.raises(SnapshotIntegrityError, match=r"extra\.txt is missing"):
        Snapshot(snap_copy).verify_all()
    (snap_copy / "extra.txt").write_bytes(b"extra")
    assert Snapshot(snap_copy).verify_all() == len(SNAPSHOT_FILES) + 1


def test_write_snapshot_manifest(tmp_path: Path) -> None:
    now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    snap = write_snapshot(tmp_path / "out", [("b/x.txt", b"x"), ("a.txt", b"a")], "abc123", now=now)
    m = snap.manifest()
    assert m["ref"] == "abc123"
    assert m["fetched_at"] == now.isoformat()
    assert list(m["files"]) == ["a.txt", "b/x.txt"]
    assert m["files"]["a.txt"] == sha256_bytes(b"a")
    assert snap.read_text("b/x.txt") == "x"
    assert not (tmp_path / "out" / (MANIFEST + ".tmp")).exists()


def test_write_snapshot_default_clock(tmp_path: Path) -> None:
    before = datetime.now(UTC)
    snap = write_snapshot(tmp_path, [("a.txt", b"a")], "r")
    stamp = datetime.fromisoformat(snap.manifest()["fetched_at"])
    assert before <= stamp <= datetime.now(UTC)


def test_snapshot_from_checkout(tmp_path: Path) -> None:
    snap = snapshot_from_checkout(SNAPSHOT, tmp_path / "out")
    assert snap.manifest()["ref"] == "local"
    assert snap.manifest()["files"] == Snapshot(SNAPSHOT).manifest()["files"]
    assert snap.verify_all() == len(SNAPSHOT_FILES)


def test_refresh_snapshot_with_fake_fetcher(tmp_path: Path) -> None:
    fetched: list[str] = []

    def fetch(rel: str) -> bytes:
        fetched.append(rel)
        return (SNAPSHOT / rel).read_bytes()

    snap = refresh_snapshot(tmp_path / "fresh", fetch, "deadbeef")
    assert fetched == list(SNAPSHOT_FILES)
    assert snap.manifest()["ref"] == "deadbeef"
    assert snap.manifest()["files"] == Snapshot(SNAPSHOT).manifest()["files"]


def test_refresh_snapshot_propagates_fetch_failure(tmp_path: Path) -> None:
    def fetch(rel: str) -> bytes:
        raise httpx.ConnectError("down")

    with pytest.raises(httpx.ConnectError):
        refresh_snapshot(tmp_path / "fresh", fetch, "x")
    assert not (tmp_path / "fresh" / MANIFEST).exists()


def test_github_fetcher_resolves_sha_and_fetches_raw() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        assert req.headers["Authorization"] == "Bearer tok"
        assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"
        if req.url.path == "/repos/org/app/commits/main":
            assert req.headers["Accept"] == "application/vnd.github+json"
            return httpx.Response(200, json={"sha": "abc123"})
        if req.url.path == f"/repos/org/app/contents/{SURAHS}":
            assert req.url.params["ref"] == "abc123"
            assert req.headers["Accept"] == "application/vnd.github.raw"
            return httpx.Response(200, content=b"raw bytes")
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetch, sha = github_fetcher(client, "org/app", "main", "tok")
        assert sha == "abc123"
        assert fetch(SURAHS) == b"raw bytes"
        with pytest.raises(httpx.HTTPStatusError):
            fetch("app/missing.txt")
    assert seen[0].url.host == "api.github.com"


def test_github_fetcher_commit_lookup_failure() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(401, json={}))) as client, pytest.raises(httpx.HTTPStatusError):
        github_fetcher(client, "org/app", "main", "bad")


def test_github_fetcher_into_refresh(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/commits/v1"):
            return httpx.Response(200, json={"sha": "f00"})
        rel = req.url.path.split("/contents/", 1)[1]
        return httpx.Response(200, content=(SNAPSHOT / rel).read_bytes())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetch, sha = github_fetcher(client, "org/app", "v1", "t")
        snap = refresh_snapshot(tmp_path / "gh", fetch, sha)
    assert snap.manifest()["ref"] == "f00"
    assert snap.verify_all() == len(SNAPSHOT_FILES)
