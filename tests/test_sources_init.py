"""`Sources.load` wires every source of truth from a verified snapshot (spec §6.2)."""

from __future__ import annotations

from pathlib import Path

from conftest import FIXTURES, SNAPSHOT
from samawi_bot.sources import Sources
from samawi_bot.sources.duas import DuaLibrary
from samawi_bot.sources.features_reader import FeatureCatalog
from samawi_bot.sources.hadith import HadithLibrary
from samawi_bot.sources.palette import Brand
from samawi_bot.sources.prayer import JAKARTA
from samawi_bot.sources.quran import Quran
from samawi_bot.sources.snapshot import Snapshot


def test_sources_load(sources: Sources, hadith_dir: Path) -> None:
    assert sources.snapshot == Snapshot(SNAPSHOT)
    assert isinstance(sources.quran, Quran)
    assert isinstance(sources.hadith, HadithLibrary) and sources.hadith.cache.root == hadith_dir
    assert isinstance(sources.duas, DuaLibrary)
    assert isinstance(sources.features, FeatureCatalog)
    assert isinstance(sources.brand, Brand)
    assert JAKARTA in sources.cities


def test_sources_load_with_missing_hadith_dir(tmp_path: Path) -> None:
    s = Sources.load(SNAPSHOT, tmp_path / "none")
    assert s.hadith.candidates() == []
    assert Sources.load(SNAPSHOT, FIXTURES / "hadith").hadith.get("3044").reference == "HR. Bukhari"
