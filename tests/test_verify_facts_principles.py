"""Explanatory principle facts (prinsip:*) and the pillars that offer them."""

from __future__ import annotations

from datetime import date

import pytest

from samawi_bot.sources import Sources
from samawi_bot.verify.facts import PRINCIPLES, UnknownFact, compute, fact_pack

DAY = date(2026, 11, 26)


@pytest.mark.parametrize("name", sorted(PRINCIPLES))
def test_every_principle_computes_with_its_text_and_numbers(sources: Sources, name: str) -> None:
    fact = compute(sources, f"prinsip:{name}", DAY)
    assert fact.key == f"prinsip:{name}"
    assert fact.label == PRINCIPLES[name][0]
    assert fact.values == PRINCIPLES[name][1]


def test_zona_waktu_carries_its_numbers_and_no_angle_principle_exists(sources: Sources) -> None:
    assert compute(sources, "prinsip:zona_waktu").values == ("3", "7", "8", "9")
    # The Subuh angle is the 18° vs 20° khilafiyah (a blocked topic, spec §6.4): never offered as a fact.
    assert not any("derajat" in text for text, _ in PRINCIPLES.values())


def test_annual_extremes_for_jakarta_subuh_2026(sources: Sources) -> None:
    f = compute(sources, "annual:Jakarta:Subuh:2026")
    assert f.values == ("04:05", "04:45", "40", "2026", "13", "18")
    assert "13/11" in f.label and "18/07" in f.label


def test_cross_city_instants_are_compared_in_real_time(sources: Sources) -> None:
    f = compute(sources, "cross:Jayapura:Isya:Banda Aceh:Maghrib:2026-11-26")
    assert f.values == ("18:48", "18:23", "95", "1", "35", "WIT", "WIB")
    assert "Isya Jayapura terjadi lebih dulu" in f.label
    g = compute(sources, "cross:Banda Aceh:Maghrib:Jayapura:Isya:2026-11-26")
    assert "Isya Jayapura terjadi lebih dulu" in g.label and g.values[2] == "95"


@pytest.mark.parametrize(
    "key", ["annual:Jakarta:Subuh", "annual:Nowhere:Subuh:2026", "cross:Jayapura:Isya:Sabang", "cross:Jayapura:Dhuha:Sabang:Isya:2026-11-26"]
)
def test_malformed_annual_and_cross_keys(sources: Sources, key: str) -> None:
    with pytest.raises(UnknownFact):
        compute(sources, key, DAY)


@pytest.mark.parametrize("key", ["prinsip:unknown", "prinsip", "prinsip:a:b"])
def test_unknown_principles_are_rejected(sources: Sources, key: str) -> None:
    with pytest.raises(UnknownFact):
        compute(sources, key, DAY)


@pytest.mark.parametrize(
    ("pillar", "expected"),
    [
        ("langit_waktu_salat", {"prinsip:posisi_matahari", "prinsip:timur_lebih_dulu", "prinsip:zona_waktu", "prinsip:ihtiyat"}),
        ("kalender_hijriah", {"prinsip:hari_hijriah", "prinsip:isbat"}),
        ("sunnah_terlupa", {"prinsip:hari_hijriah"}),
        ("musafir", {"prinsip:zona_waktu", "prinsip:posisi_matahari"}),
        ("fakta_quran", set()),
    ],
)
def test_fact_packs_offer_the_right_principles(sources: Sources, pillar: str, expected: set[str]) -> None:
    keys = {f.key for f in fact_pack(sources, pillar, DAY) if f.key.startswith("prinsip:")}
    assert keys == expected
