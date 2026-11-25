"""The weekly plan (spec §5, §8.1): pillars, Friday, series, seasons, bridge rotation, formats."""

from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from samawi_bot.brain.models import HookFormula, Pillar
from samawi_bot.brain.planner import (
    BRIDGES,
    INITIAL_HOOK_WEIGHTS,
    INITIAL_PILLAR_WEIGHTS,
    SERIES_LENGTH,
    SERIES_TITLES,
    DayPlan,
    SeriesState,
    choose_bridges,
    plan_week,
    seasonal_weights,
    slot_times,
    weighted_choice,
)
from samawi_bot.sources import Sources
from samawi_bot.sources.features_reader import Feature, FeatureCatalog, Status, Tier
from samawi_bot.sources.hijri import to_gregorian

MONDAY = date(2026, 11, 23)  # 13 Jumadil Akhir 1448: no seasonal boost


def plan(catalog: FeatureCatalog, start: date = MONDAY, seed: int = 1, **kw: object) -> list[DayPlan]:
    args: dict[str, object] = {
        "pillar_weights": dict(INITIAL_PILLAR_WEIGHTS),
        "hook_weights": dict(INITIAL_HOOK_WEIGHTS),
        "open_series": {},
        "recent_pillars": [],
        "recent_bridges": [],
        "paid_bridges": 0,
        "total_bridges": 0,
    }
    args.update(kw)
    return plan_week(start, catalog, seed=seed, **args)  # type: ignore[arg-type]


def catalog_of(*features: tuple[str, Tier, Status]) -> FeatureCatalog:
    return FeatureCatalog({k: Feature(k, t, s) for k, t, s in features})


# ── determinism, pillars, Friday ───────────────────────────────────────────────────────────


def test_plan_week_is_deterministic_for_a_seed(sources: Sources) -> None:
    a = plan(sources.features, seed=42)
    b = plan(sources.features, seed=42)
    assert a == b
    assert len(a) == 7
    assert [p.day for p in a] == [MONDAY + timedelta(days=i) for i in range(7)]
    variants = {tuple((p.pillar, p.hook_formula) for p in plan(sources.features, seed=s)) for s in range(10)}
    assert len(variants) > 1


@pytest.mark.parametrize("seed", range(40))
def test_never_the_same_pillar_two_days_running_except_friday(sources: Sources, seed: int) -> None:
    start = MONDAY + timedelta(days=seed % 7)
    recent = [Pillar.QURAN.value]
    plans = plan(sources.features, start=start, seed=seed, recent_pillars=recent, days=14)
    previous = recent[0]
    for p in plans:
        if p.day.weekday() == 4:
            assert p.pillar == Pillar.SUNNAH.value
            assert "Hari Jumat: sunnah Jumat." in p.seasonal
        else:
            assert p.pillar != previous, (p.day, p.pillar)
        assert p.series_title == SERIES_TITLES[p.pillar]
        previous = p.pillar


def test_friday_after_a_sunnah_thursday_is_still_sunnah(sources: Sources) -> None:
    thursday = MONDAY + timedelta(days=3)
    plans = plan(sources.features, start=thursday, days=3, pillar_weights={Pillar.SUNNAH: 1.0, Pillar.QURAN: 1.0}, recent_pillars=[Pillar.QURAN.value])
    assert [p.pillar for p in plans] == [Pillar.SUNNAH.value, Pillar.SUNNAH.value, Pillar.QURAN.value]


def test_format_follows_weekday(sources: Sources) -> None:
    plans = plan(sources.features, days=7)
    assert [p.format for p in plans] == ["carousel", "reel", "carousel", "reel", "carousel", "reel", "carousel"]


# ── series ─────────────────────────────────────────────────────────────────────────────────


def test_series_continue_to_five_then_a_new_series_starts(sources: Sources) -> None:
    weights = {Pillar.QURAN: 1.0, Pillar.SEJARAH: 1.0}
    open_series = {Pillar.QURAN.value: SeriesState("fakta_quran-old", Pillar.QURAN.value, 3, 5)}
    plans = plan(sources.features, pillar_weights=weights, open_series=open_series, recent_pillars=[Pillar.SEJARAH.value], days=28)
    quran = [p for p in plans if p.pillar == Pillar.QURAN.value]
    assert [(p.series_id, p.series_part) for p in quran[:2]] == [("fakta_quran-old", 4), ("fakta_quran-old", 5)]
    third = quran[2]
    assert third.series_id == f"fakta_quran-{third.day.isoformat()}"
    assert third.series_part == 1 and third.series_total == SERIES_LENGTH
    parts = [p.series_part for p in quran]
    assert parts[:9] == [4, 5, 1, 2, 3, 4, 5, 1, 2]
    for pillar in {p.pillar for p in plans}:
        seq = [p.series_part for p in plans if p.pillar == pillar]
        assert all(1 <= x <= SERIES_LENGTH for x in seq)
    # the caller's open series is not mutated
    assert open_series[Pillar.QURAN.value].part == 3


def test_series_hook_formula_only_after_part_one(sources: Sources) -> None:
    seen_series = False
    for seed in range(60):
        for p in plan(sources.features, seed=seed, days=14):
            assert p.hook_formula in {h.value for h in HookFormula}
            if p.hook_formula == HookFormula.SERIES.value and p.series_part == 1:
                # only reachable through the weighted draw, never the forced continuation
                continue
            if p.series_part > 1 and p.hook_formula == HookFormula.SERIES.value:
                seen_series = True
    assert seen_series


def test_hook_formula_follows_weights(sources: Sources) -> None:
    only = {HookFormula.KENAPA.value: 1.0}
    plans = plan(sources.features, hook_weights=only, days=7, seed=3)
    assert all(p.hook_formula in (HookFormula.KENAPA.value, HookFormula.SERIES.value) for p in plans)
    assert plans[0].hook_formula == HookFormula.KENAPA.value  # part 1 is never the series formula


# ── seasons ────────────────────────────────────────────────────────────────────────────────


def test_no_season_normalises_only() -> None:
    w, note = seasonal_weights({Pillar.QURAN: 2.0, Pillar.SEJARAH: 2.0}, MONDAY)
    assert w == {Pillar.QURAN: 0.5, Pillar.SEJARAH: 0.5}
    assert note == ""


@pytest.mark.parametrize(("month", "day"), [(7, 1), (7, 20), (8, 10), (9, 1), (9, 14)])
def test_rajab_to_ramadan_kalender_at_least_30pct(month: int, day: int) -> None:
    w, note = seasonal_weights(dict(INITIAL_PILLAR_WEIGHTS), to_gregorian(1448, month, day))
    assert w[Pillar.KALENDER] >= 0.30 - 1e-9
    assert sum(w.values()) == pytest.approx(1.0)
    assert note.startswith("Bulan ")
    assert "Ramadan" in note
    assert all(v > 0 for v in w.values())
    # the others keep their relative order
    assert w[Pillar.QURAN] > w[Pillar.LANGIT] > w[Pillar.MUSAFIR]


@pytest.mark.parametrize(("month", "day", "kalender"), [(9, 15, True), (9, 29, True), (10, 1, False), (10, 10, False)])
def test_mudik_musafir_at_least_25pct(month: int, day: int, kalender: bool) -> None:
    w, note = seasonal_weights(dict(INITIAL_PILLAR_WEIGHTS), to_gregorian(1448, month, day))
    assert w[Pillar.MUSAFIR] >= 0.25 - 1e-9
    assert "Musim mudik." in note
    assert sum(w.values()) == pytest.approx(1.0)
    if kalender:
        assert w[Pillar.KALENDER] >= 0.30 - 1e-9
    else:
        assert w[Pillar.KALENDER] < 0.30


@pytest.mark.parametrize(("month", "day"), [(6, 29), (9, 14), (10, 11), (12, 1)])
def test_outside_mudik_no_musafir_boost(month: int, day: int) -> None:
    w, note = seasonal_weights(dict(INITIAL_PILLAR_WEIGHTS), to_gregorian(1448, month, day))
    assert w[Pillar.MUSAFIR] < 0.25
    assert "mudik" not in note


def test_learned_weight_above_target_is_kept() -> None:
    base = {**INITIAL_PILLAR_WEIGHTS, Pillar.KALENDER: 0.5}
    total = sum(base.values())
    w, _ = seasonal_weights(base, to_gregorian(1448, 7, 5))
    assert w[Pillar.KALENDER] == pytest.approx(0.5 / total)


def test_only_boosted_pillars_and_overfull_targets() -> None:
    ramadan_mudik = to_gregorian(1448, 9, 20)
    w, _ = seasonal_weights({Pillar.KALENDER: 1.0, Pillar.MUSAFIR: 1.0}, ramadan_mudik)
    assert w == {Pillar.KALENDER: 0.5, Pillar.MUSAFIR: 0.5}
    w, _ = seasonal_weights({Pillar.KALENDER: 0.9, Pillar.MUSAFIR: 0.05, Pillar.QURAN: 0.05}, ramadan_mudik)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w[Pillar.QURAN] == 0.0
    assert w[Pillar.KALENDER] > w[Pillar.MUSAFIR] > 0.2


def test_ramadan_plan_uses_kalender_more(sources: Sources) -> None:
    start = to_gregorian(1448, 7, 1)
    counts = {"in": 0, "out": 0}
    for seed in range(30):
        counts["in"] += sum(p.pillar == Pillar.KALENDER.value for p in plan(sources.features, start=start, seed=seed, days=7))
        counts["out"] += sum(p.pillar == Pillar.KALENDER.value for p in plan(sources.features, start=MONDAY, seed=seed, days=7))
    assert counts["in"] > counts["out"] * 1.5
    assert "Bulan Rajab" in plan(sources.features, start=start)[0].seasonal


# ── bridges ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("seed", range(30))
def test_bridge_rotation_paid_share_and_no_three_in_a_row(sources: Sources, seed: int) -> None:
    feats = sources.features.features
    recent: list[str] = []
    paid = total = 0
    start = MONDAY
    chosen: list[str] = []
    for _week in range(4):
        plans = plan(sources.features, start=start, seed=seed + _week, recent_bridges=recent, paid_bridges=paid, total_bridges=total)
        for p in plans:
            assert p.bridge_options, p
            for k in p.bridge_options:
                assert feats[k].shipped
                # never offered when it was the bridge on both of the previous two days
                assert not (len(chosen) >= 2 and chosen[-1] == chosen[-2] == k)
            first = p.bridge_options[0]
            chosen.append(first)
            paid += feats[first].paid
            total += 1
            assert paid / total >= 0.6, (p.day, paid, total)
        recent = list(reversed(chosen))
        start += timedelta(days=7)
    week_paid = sum(feats[k].paid for k in chosen[:7]) / 7
    assert week_paid >= 0.6


def test_choose_bridges_prefers_paid_when_share_is_low(sources: Sources) -> None:
    opts = choose_bridges(sources.features, Pillar.LANGIT.value, [], 0, 0)
    assert opts and all(sources.features.features[k].paid for k in opts)
    assert set(opts) <= set(BRIDGES[Pillar.LANGIT])
    assert choose_bridges(sources.features, Pillar.LANGIT.value, [], 6, 10) == ["homeScreenWidgets", "multiLocation"]


def test_choose_bridges_allows_free_when_share_is_high(sources: Sources) -> None:
    opts = choose_bridges(sources.features, Pillar.LANGIT.value, [], 9, 10)
    assert opts == ["homeScreenWidgets", "prayerTimes", "adzan", "multiLocation"]


def test_choose_bridges_blocks_a_feature_used_two_days_running(sources: Sources) -> None:
    opts = choose_bridges(sources.features, Pillar.SEJARAH.value, ["tafsir", "tafsir"], 0, 0)
    assert "tafsir" not in opts
    assert opts == ["qibla", "quranText"]  # no paid option left: free ones rather than a third tafsir
    # used once, or not on consecutive days: still allowed
    assert choose_bridges(sources.features, Pillar.SEJARAH.value, ["tafsir", "qibla", "tafsir"], 0, 0) == ["tafsir"]
    assert choose_bridges(sources.features, Pillar.SEJARAH.value, ["tafsir"], 0, 0) == ["tafsir"]


def test_choose_bridges_skips_unshipped_features() -> None:
    cat = catalog_of(("tafsir", Tier.PAID, Status.PLANNED), ("qibla", Tier.FREE, Status.SHIPPED), ("quranText", Tier.FREE, Status.BLOCKED))
    assert choose_bridges(cat, Pillar.SEJARAH.value, [], 0, 0) == ["qibla"]


def test_choose_bridges_fallback_when_pillar_has_no_option() -> None:
    cat = catalog_of(
        ("tafsir", Tier.PAID, Status.SHIPPED),
        ("jumatMode", Tier.PAID, Status.SHIPPED),
        ("khatamPlanner", Tier.PAID, Status.SHIPPED),
        ("travelMode", Tier.PAID, Status.SHIPPED),
        ("qibla", Tier.FREE, Status.SHIPPED),
    )
    assert choose_bridges(cat, "unknown_pillar", [], 0, 0) == ["tafsir", "jumatMode", "khatamPlanner"]
    assert choose_bridges(cat, "unknown_pillar", ["tafsir", "tafsir"], 0, 0) == ["jumatMode", "khatamPlanner", "travelMode"]
    assert choose_bridges(catalog_of(("qibla", Tier.FREE, Status.SHIPPED)), "unknown_pillar", [], 0, 0) == []


def test_plan_week_without_any_bridgeable_feature() -> None:
    plans = plan(FeatureCatalog({}), days=3)
    assert all(p.bridge_options == [] for p in plans)


def test_weighted_choice_never_picks_zero_but_tolerates_it() -> None:
    rng = random.Random(0)
    picks = {weighted_choice(rng, {"a": 0.0, "b": 1.0}) for _ in range(200)}
    assert picks == {"b"}
    assert weighted_choice(random.Random(0), {"only": 0.0}) == "only"


def test_slot_times() -> None:
    thursday = MONDAY + timedelta(days=3)
    assert set(slot_times(MONDAY)) == {"subuh_story", "subuh_threads", "siang_feed", "siang_threads", "malam_story", "malam_threads"}
    times = slot_times(thursday)
    assert "kahfi_story" in times and len(times) == 7
