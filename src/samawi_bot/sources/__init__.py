"""Every source of truth the bot is allowed to state facts from (spec §6.2)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .duas import DuaLibrary
from .features_reader import FeatureCatalog
from .hadith import HadithCache, HadithLibrary
from .palette import Brand, load_brand
from .prayer import City, load_cities
from .quran import Quran
from .snapshot import Snapshot


@dataclass
class Sources:
    snapshot: Snapshot
    quran: Quran
    hadith: HadithLibrary
    duas: DuaLibrary
    features: FeatureCatalog
    cities: list[City]
    brand: Brand

    @classmethod
    def load(cls, snapshot_dir: Path, hadith_dir: Path) -> Sources:
        snap = Snapshot(snapshot_dir)
        snap.verify_all()
        return cls(
            snapshot=snap,
            quran=Quran.load(snap),
            hadith=HadithLibrary(HadithCache(hadith_dir)),
            duas=DuaLibrary.load(snap),
            features=FeatureCatalog.load(snap),
            cities=load_cities(snap),
            brand=load_brand(snap),
        )
