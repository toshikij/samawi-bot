"""Duas and dzikir from the app's `duas.json`. Only entries released for production (spec §6.2)."""

from __future__ import annotations

import json
from dataclasses import dataclass

from .snapshot import DUAS, Snapshot


class DuaUnavailable(KeyError):
    pass


@dataclass(frozen=True)
class Dua:
    id: str
    chapter: str
    arabic: str
    latin: str
    indonesian: str
    reference: str
    tier: str


class DuaLibrary:
    def __init__(self, data: list[dict[str, object]]):
        self._by_id: dict[str, Dua] = {}
        self.held_back: set[str] = set()
        for collection in data:
            tier = str(collection.get("tier", "paid"))
            for chapter in collection.get("chapters", []):  # type: ignore[attr-defined]
                for d in chapter.get("duas", []):
                    if d.get("needsReview", True) is not False:
                        self.held_back.add(str(d.get("id")))
                        continue
                    self._by_id[d["id"]] = Dua(
                        id=d["id"],
                        chapter=str(chapter.get("title", "")),
                        arabic=d["arabic"],
                        latin=d.get("latin", ""),
                        indonesian=d["indonesian"],
                        reference=d.get("reference", ""),
                        tier=tier,
                    )

    @classmethod
    def load(cls, snap: Snapshot) -> DuaLibrary:
        return cls(json.loads(snap.read_text(DUAS)))

    def get(self, dua_id: str) -> Dua:
        if dua_id in self.held_back:
            raise DuaUnavailable(f"{dua_id} is not released for production (needsReview)")
        if dua_id not in self._by_id:
            raise DuaUnavailable(f"No dua {dua_id!r}")
        return self._by_id[dua_id]

    def all(self) -> list[Dua]:
        return list(self._by_id.values())
