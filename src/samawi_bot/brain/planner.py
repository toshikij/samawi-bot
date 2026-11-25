"""The weekly plan (spec §5, §8.1): pillar per day, series continuity, seasonal weighting, bridge
rotation (≥ 60% paid, no feature 3 days running), hook formula, and format by weekday."""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ..publisher.slots import feed_format, slots_for
from ..sources.features_reader import FeatureCatalog
from ..sources.hijri import islamic_date
from .models import HookFormula, Pillar

INITIAL_PILLAR_WEIGHTS: dict[str, float] = {
    Pillar.LANGIT: 0.15,
    Pillar.QURAN: 0.20,
    Pillar.SUNNAH: 0.15,
    Pillar.DOA: 0.10,
    Pillar.MUSAFIR: 0.05,
    Pillar.HITUNG: 0.10,
    Pillar.KALENDER: 0.10,
    Pillar.SEJARAH: 0.15,
}
INITIAL_HOOK_WEIGHTS: dict[str, float] = {h.value: 1 / len(HookFormula) for h in HookFormula}

SERIES_TITLES: dict[str, str] = {
    Pillar.LANGIT: "Seri Langit & Waktu Salat",
    Pillar.QURAN: "Seri Ayat yang Jarang Dibahas",
    Pillar.SUNNAH: "Seri Sunnah yang Terlupa",
    Pillar.DOA: "Seri Doa yang Jarang Diamalkan",
    Pillar.MUSAFIR: "Seri Musafir",
    Pillar.HITUNG: "Seri Hitung-hitungan Ibadah",
    Pillar.KALENDER: "Seri Kalender Hijriah",
    Pillar.SEJARAH: "Seri Kisah yang Jarang Diceritakan",
}
SERIES_LENGTH = 5

BRIDGES: dict[str, list[str]] = {
    Pillar.LANGIT: ["homeScreenWidgets", "prayerTimes", "adzan", "multiLocation"],
    Pillar.QURAN: ["khatamPlanner", "tafsir", "primaryTranslation", "quranText"],
    Pillar.SUNNAH: ["jumatMode", "sunnahFastingTracker", "advancedTracker"],
    Pillar.DOA: ["fullDuaLibrary", "dailyDhikr", "dzikirCounter"],
    Pillar.MUSAFIR: ["travelMode", "nearestMosque", "multiLocation"],
    Pillar.HITUNG: ["advancedTracker", "khatamPlanner", "zakatCalculator"],
    Pillar.KALENDER: ["ramadanPack", "sunnahFastingTracker", "homeScreenWidgets"],
    Pillar.SEJARAH: ["tafsir", "qibla", "quranText"],
}
PAID_SHARE_MIN = 0.6


@dataclass
class DayPlan:
    day: date
    pillar: str
    format: str
    hook_formula: str
    bridge_options: list[str]
    series_id: str
    series_title: str
    series_part: int
    series_total: int
    seasonal: str


@dataclass
class SeriesState:
    series_id: str
    pillar: str
    part: int  # last published/planned part
    total: int


def seasonal_weights(base: dict[str, float], day: date) -> tuple[dict[str, float], str]:
    """Kalender up to 30% in Rajab–Syaban–Ramadan; Musafir up to 25% around mudik (spec §5)."""
    h = islamic_date(day)
    total = sum(base.values())
    shares = {k: v / total for k, v in base.items()}
    targets: dict[str, float] = {}
    note = ""
    if h.month in (7, 8, 9):
        targets[Pillar.KALENDER] = 0.30
        note = f"Bulan {h.month_name}: menjelang/di bulan Ramadan."
    if (h.month == 9 and h.day >= 15) or (h.month == 10 and h.day <= 10):
        targets[Pillar.MUSAFIR] = 0.25
        note = (note + " Musim mudik.").strip()
    if not targets:
        return shares, note
    # The targets are shares of the final mix, so fix them first and scale the rest into what is left.
    fixed = {k: max(shares.get(k, 0.0), t) for k, t in targets.items()}
    rest = {k: v for k, v in shares.items() if k not in fixed}
    rest_total = sum(rest.values())
    remaining = max(0.0, 1 - sum(fixed.values()))
    w = {k: (v / rest_total * remaining if rest_total > 0 else 0.0) for k, v in rest.items()}
    w.update(fixed)
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}, note


def choose_bridges(catalog: FeatureCatalog, pillar: str, recent: Sequence[str], paid_so_far: int, total_so_far: int) -> list[str]:
    """Bridge options for the writer: shipped, named, not used the previous two days, paid first
    whenever the running paid share would otherwise fall below 60%."""
    blocked = {recent[0]} if len(recent) >= 2 and recent[0] == recent[1] else set()
    shipped = {f.key: f for f in catalog.bridgeable()}
    options = [k for k in BRIDGES.get(pillar, []) if k in shipped and k not in blocked]
    paid = [k for k in options if shipped[k].paid]
    # A small margin over 60% so the share never dips under it when a free bridge is picked.
    need_paid = total_so_far == 0 or paid_so_far / total_so_far < PAID_SHARE_MIN + 0.1
    if need_paid and paid:
        return paid
    if options:
        return options
    fallback = [k for k, f in shipped.items() if f.paid and k not in blocked]
    return fallback[:3]


def weighted_choice(rng: random.Random, weights: dict[str, float]) -> str:
    keys = sorted(weights)
    return rng.choices(keys, [max(weights[k], 1e-6) for k in keys])[0]


def plan_week(
    start: date,
    catalog: FeatureCatalog,
    pillar_weights: dict[str, float],
    hook_weights: dict[str, float],
    open_series: dict[str, SeriesState],
    recent_pillars: list[str],
    recent_bridges: list[str],
    paid_bridges: int,
    total_bridges: int,
    seed: int,
    days: int = 7,
) -> list[DayPlan]:
    rng = random.Random(seed)
    plans: list[DayPlan] = []
    pillars = list(recent_pillars)
    bridges = list(recent_bridges)
    series = {k: SeriesState(v.series_id, v.pillar, v.part, v.total) for k, v in open_series.items()}
    for i in range(days):
        day = start + timedelta(days=i)
        weights, note = seasonal_weights(pillar_weights, day)
        if day.weekday() == 4:  # Friday: the Jumat sunnah series
            pillar = Pillar.SUNNAH.value
            note = (note + " Hari Jumat: sunnah Jumat.").strip()
        else:
            choices = {k: v for k, v in weights.items() if not pillars or k != pillars[0]}
            pillar = weighted_choice(rng, choices)
        state = series.get(pillar)
        if state is None or state.part >= state.total:
            sid = f"{pillar}-{day.isoformat()}"
            state = SeriesState(sid, pillar, 0, SERIES_LENGTH)
        state.part += 1
        series[pillar] = state
        options = choose_bridges(catalog, pillar, bridges, paid_bridges, total_bridges)
        if options:
            first = catalog.features[options[0]]
            paid_bridges += 1 if first.paid else 0
            total_bridges += 1
            bridges.insert(0, options[0])
        plans.append(
            DayPlan(
                day=day,
                pillar=pillar,
                format=feed_format(day),
                hook_formula=HookFormula.SERIES.value if state.part > 1 and rng.random() < 0.2 else weighted_choice(rng, hook_weights),
                bridge_options=options,
                series_id=state.series_id,
                series_title=SERIES_TITLES[pillar],
                series_part=state.part,
                series_total=state.total,
                seasonal=note,
            )
        )
        pillars.insert(0, pillar)
    return plans


def slot_times(day: date) -> dict[str, object]:
    return {t.spec.name: t.at for t in slots_for(day)}
