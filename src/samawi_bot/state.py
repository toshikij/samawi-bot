"""Durable local state across ephemeral ECS tasks.

Every job runs in a fresh container, so the refreshed app snapshot and the HadeethEnc/fawazahmed0
cache are archived to S3 (`state/<name>.tar.gz`) after `plan` refreshes them and restored before
any job runs. The image's bundled snapshot is the fallback when no archive exists yet.
"""

from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

from .publisher.storage import Storage


def _key(name: str) -> str:
    return f"state/{name}.tar.gz"


def push(storage: Storage, name: str, directory: Path) -> bool:
    if not directory.is_dir():
        return False
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        tar.add(directory, arcname=".")
    storage.put(_key(name), buf.getvalue())
    return True


def pull(storage: Storage, name: str, directory: Path) -> bool:
    """Restore `directory` from its archive; leaves it untouched if there is none."""
    if not storage.exists(_key(name)):
        return False
    data = storage.get(_key(name))
    tmp = directory.with_name(directory.name + ".restore")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(tmp, filter="data")
    shutil.rmtree(directory, ignore_errors=True)
    tmp.rename(directory)
    return True
