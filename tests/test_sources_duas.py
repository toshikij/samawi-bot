"""Duas: only entries released for production (`needsReview: false`) are usable (spec §6.2)."""

from __future__ import annotations

import json

import pytest

from conftest import SNAPSHOT
from samawi_bot.sources import Sources
from samawi_bot.sources.duas import Dua, DuaLibrary, DuaUnavailable
from samawi_bot.sources.snapshot import DUAS, Snapshot


def _dua(did: str, **extra: object) -> dict[str, object]:
    return {"id": did, "arabic": f"ar-{did}", "indonesian": f"id-{did}", **extra}


@pytest.fixture
def library() -> DuaLibrary:
    return DuaLibrary(
        [
            {
                "id": "harian",
                "tier": "free",
                "chapters": [
                    {
                        "title": "Pagi",
                        "duas": [
                            _dua("ok.1", latin="lat", reference="HR. Muslim", needsReview=False),
                            _dua("review.true", needsReview=True),
                            _dua("review.missing"),
                            _dua("review.null", needsReview=None),
                            _dua("review.zero", needsReview=0),
                            _dua("review.string", needsReview="false"),
                        ],
                    },
                    {"duas": [_dua("ok.2", needsReview=False)]},
                    {"title": "Kosong"},
                ],
            },
            {"id": "lengkap", "chapters": [{"title": "Safar", "duas": [_dua("ok.3", needsReview=False)]}]},
            {"id": "no-chapters", "tier": "paid"},
        ]
    )


def test_released_entries(library: DuaLibrary) -> None:
    assert library.get("ok.1") == Dua("ok.1", "Pagi", "ar-ok.1", "lat", "id-ok.1", "HR. Muslim", "free")
    assert library.get("ok.2") == Dua("ok.2", "", "ar-ok.2", "", "id-ok.2", "", "free")
    assert library.get("ok.3").tier == "paid"  # a collection without a tier is treated as paid
    assert [d.id for d in library.all()] == ["ok.1", "ok.2", "ok.3"]


@pytest.mark.parametrize("did", ["review.true", "review.missing", "review.null", "review.zero", "review.string"])
def test_needs_review_is_held_back(library: DuaLibrary, did: str) -> None:
    assert did in library.held_back
    with pytest.raises(DuaUnavailable, match="needsReview"):
        library.get(did)
    assert did not in {d.id for d in library.all()}


def test_unknown_id(library: DuaLibrary) -> None:
    with pytest.raises(DuaUnavailable, match="No dua 'nope'"):
        library.get("nope")
    with pytest.raises(KeyError):  # DuaUnavailable is a KeyError for callers that catch that
        library.get("nope")


def test_empty_library() -> None:
    lib = DuaLibrary([])
    assert lib.all() == [] and lib.held_back == set()


def test_real_file_matches_review_flags(sources: Sources) -> None:
    data = json.loads(Snapshot(SNAPSHOT).read_text(DUAS))
    released = {d["id"] for c in data for ch in c.get("chapters", []) for d in ch.get("duas", []) if d.get("needsReview", True) is False}
    held = {d["id"] for c in data for ch in c.get("chapters", []) for d in ch.get("duas", []) if d.get("needsReview", True) is not False}
    assert {d.id for d in sources.duas.all()} == released
    assert sources.duas.held_back == held
    for did in released:
        d = sources.duas.get(did)
        assert d.arabic and d.indonesian
    assert DuaLibrary.load(Snapshot(SNAPSHOT)).all() == sources.duas.all()
