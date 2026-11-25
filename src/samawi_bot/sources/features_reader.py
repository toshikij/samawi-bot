"""Reads `samawi_features.dart`, the app's single source of truth for features (spec §5).

The bot may only bridge to a feature whose status is `shipped`, the same rule the paywall uses.
The parser fails closed: anything it cannot read confidently is not shipped. A feature renamed or
deleted in the app disappears from `shipped()` and every draft bridging to it is blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from .snapshot import FEATURES, Snapshot


class Tier(StrEnum):
    FREE = "free"
    PAID = "paid"


class Status(StrEnum):
    SHIPPED = "shipped"
    PLANNED = "planned"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Feature:
    key: str
    tier: Tier
    status: Status

    @property
    def shipped(self) -> bool:
        return self.status is Status.SHIPPED

    @property
    def paid(self) -> bool:
        return self.tier is Tier.PAID


class FeaturesParseError(ValueError):
    pass


# How each feature is named in a post. A feature missing here can still be read, but cannot be
# bridged to until it has a name, so a new app feature never appears under a made-up label.
DISPLAY_NAMES: dict[str, str] = {
    "prayerTimes": "Waktu salat Kemenag",
    "adzan": "Adzan yang benar-benar berbunyi",
    "qibla": "Arah kiblat",
    "ramadanTimes": "Jadwal imsak & berbuka",
    "quranText": "Al-Qur'an offline",
    "primaryTranslation": "Al-Qur'an dengan terjemahan",
    "basicTracker": "Catatan salat",
    "zakatCalculator": "Kalkulator zakat",
    "dailyDhikr": "Dzikir harian",
    "dzikirCounter": "Penghitung dzikir",
    "fullDuaLibrary": "Kumpulan doa lengkap",
    "homeScreenWidgets": "Widget layar utama",
    "advancedTracker": "Catatan ibadah mendalam",
    "tafsir": "Tafsir",
    "khatamPlanner": "Rencana khatam",
    "sunnahFastingTracker": "Pengingat puasa sunnah",
    "multiLocation": "Banyak lokasi",
    "jumatMode": "Mode Jumat",
    "travelMode": "Mode musafir",
    "adzanProfiles": "Profil adzan",
    "nearestMosque": "Masjid terdekat",
    "mosqueAdjustment": "Penyesuaian waktu masjid",
    "ramadanPack": "Paket Ramadan",
    "backupRestore": "Cadangan data",
}

_ENUM_RE = re.compile(r"\benum\s+SamawiFeature\s*\{(?P<body>.*?);", re.S)
_ENTRY_RE = re.compile(r"^\s*(?P<key>[a-z][A-Za-z0-9]*)\s*\((?P<args>[^()]*)\)\s*$", re.S)
_TIER_RE = re.compile(r"FeatureTier\.(\w+)")
_STATUS_RE = re.compile(r"\bstatus\s*:\s*FeatureStatus\.(\w+)")
_STATUS_ARG_RE = re.compile(r"\bstatus\s*:")


def _strip_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _split_top_level(body: str) -> list[str]:
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return [p for p in (s.strip() for s in parts) if p]


def parse_features(dart_source: str) -> dict[str, Feature]:
    m = _ENUM_RE.search(_strip_comments(dart_source))
    if not m:
        raise FeaturesParseError("enum SamawiFeature not found")
    features: dict[str, Feature] = {}
    for raw in _split_top_level(m["body"]):
        entry = _ENTRY_RE.match(raw)
        if not entry:
            raise FeaturesParseError(f"Unrecognised feature entry: {raw[:60]!r}")
        tier_m = _TIER_RE.search(entry["args"])
        if not tier_m or tier_m[1] not in Tier.__members__.values():
            raise FeaturesParseError(f"{entry['key']} has no recognised FeatureTier")
        status_m = _STATUS_RE.search(entry["args"])
        if status_m is None:
            # The Dart constructor's default, unless a status is given in a form we cannot read
            # (e.g. `status: kPlanned`), which must fail closed rather than read as shipped.
            status = Status.UNKNOWN if _STATUS_ARG_RE.search(entry["args"]) else Status.SHIPPED
        else:
            status = Status(status_m[1]) if status_m[1] in {s.value for s in Status} else Status.UNKNOWN
        features[entry["key"]] = Feature(entry["key"], Tier(tier_m[1]), status)
    if not features:
        raise FeaturesParseError("SamawiFeature has no entries")
    return features


class FeatureCatalog:
    def __init__(self, features: dict[str, Feature]):
        self.features = features

    @classmethod
    def load(cls, snap: Snapshot) -> FeatureCatalog:
        return cls(parse_features(snap.read_text(FEATURES)))

    def bridgeable(self) -> list[Feature]:
        """Shipped features that also have a public name: the only ones a post may mention."""
        return [f for f in self.features.values() if f.shipped and f.key in DISPLAY_NAMES]

    def check_bridge(self, key: str) -> str | None:
        """None if `key` may be bridged to, else the reason it may not."""
        feature = self.features.get(key)
        if feature is None:
            return f"bridge feature {key!r} is not in samawi_features.dart"
        if not feature.shipped:
            return f"bridge feature {key!r} has status {feature.status.value}, not shipped"
        if key not in DISPLAY_NAMES:
            return f"bridge feature {key!r} has no public name yet"
        return None

    @staticmethod
    def display_name(key: str) -> str:
        return DISPLAY_NAMES[key]
