"""Hijri dates: parity with the app's calendar, and the isbat rule (spec §5, §6.2)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from conftest import FIXTURES
from samawi_bot.sources.hijri import (
    ISBAT_NOTE,
    MONTH_NAMES,
    RAMADAN,
    SYAWAL,
    IslamicDate,
    ayyamul_bidh,
    days_until,
    islamic_date,
    month_start,
    needs_isbat_note,
    next_month_start,
    to_gregorian,
)


@pytest.fixture(scope="module")
def golden_hijri() -> list[dict]:
    return json.loads((FIXTURES / "app_engine_golden.json").read_text("utf-8"))["hijri"]


def test_every_golden_day_matches_the_app(golden_hijri: list[dict]) -> None:
    assert len(golden_hijri) == 1096
    mismatches: list[str] = []
    for row in golden_hijri:
        got = islamic_date(date.fromisoformat(row["date"]))
        want = (row["y"], row["m"], row["d"], row["near"])
        if (got.year, got.month, got.day, got.near_boundary) != want:
            mismatches.append(f"{row['date']}: python {got} near={got.near_boundary}, app {want}")
    assert not mismatches, f"{len(mismatches)} Hijri days differ from the app:\n" + "\n".join(mismatches[:20])


def test_golden_covers_boundaries(golden_hijri: list[dict]) -> None:
    near = [r for r in golden_hijri if r["near"]]
    assert near and all(r["d"] <= 1 or r["d"] >= 28 for r in near)
    assert all(not r["near"] for r in golden_hijri if 2 <= r["d"] <= 27)


def test_near_boundary_flags() -> None:
    # 1 Ramadan 1447 is calculated as 18 Feb 2026; Ramadan 1447 has 29 days here.
    start = month_start(1447, RAMADAN)
    first = islamic_date(start)
    assert (first.month, first.day, first.near_boundary, first.confident) == (RAMADAN, 1, True, False)
    second = islamic_date(start + timedelta(days=1))
    assert (second.day, second.near_boundary, second.confident) == (2, False, True)
    month_length = (month_start(1447, SYAWAL) - start).days
    penultimate = islamic_date(start + timedelta(days=month_length - 2))
    assert penultimate.day == month_length - 1 and penultimate.near_boundary
    before = islamic_date(start + timedelta(days=month_length - 3))
    assert before.day == month_length - 2 and not before.near_boundary
    last = islamic_date(start + timedelta(days=month_length - 1))
    assert last.day == month_length and last.near_boundary


def test_after_sunset_is_the_next_hijri_day() -> None:
    d = date(2026, 11, 26)
    assert islamic_date(d, after_sunset=True) == islamic_date(d + timedelta(days=1))
    assert islamic_date(d, after_sunset=True) != islamic_date(d)
    eve = month_start(1447, RAMADAN) - timedelta(days=1)
    assert islamic_date(eve).month == 8
    assert (islamic_date(eve, after_sunset=True).month, islamic_date(eve, after_sunset=True).day) == (RAMADAN, 1)


def test_islamic_date_formatting() -> None:
    d = IslamicDate(1447, RAMADAN, 1, near_boundary=True)
    assert d.month_name == "Ramadan"
    assert str(d) == "1 Ramadan 1447 H"
    assert IslamicDate(1448, 12, 10, False).month_name == "Zulhijjah"
    assert IslamicDate(1448, 1, 10, False).month_name == "Muharram"
    assert len(MONTH_NAMES) == 12


def test_to_gregorian_round_trip() -> None:
    for civil in (date(2026, 1, 1), date(2027, 6, 15), date(2028, 2, 29)):
        h = islamic_date(civil)
        assert to_gregorian(h.year, h.month, h.day) == civil
    assert month_start(1447, RAMADAN) == to_gregorian(1447, RAMADAN, 1)


def test_needs_isbat_note() -> None:
    assert ISBAT_NOTE == "perkiraan, menunggu sidang isbat"
    assert needs_isbat_note(1447, RAMADAN, 1)
    assert needs_isbat_note(1447, SYAWAL, 1)
    assert not needs_isbat_note(1447, RAMADAN, 2)
    assert not needs_isbat_note(1447, SYAWAL, 2)
    assert not needs_isbat_note(1447, 12, 1)
    assert not needs_isbat_note(1447, 8, 1)


def test_ayyamul_bidh() -> None:
    d13, d14, d15 = ayyamul_bidh(1448, 5)
    assert (d14 - d13, d15 - d14) == (timedelta(days=1), timedelta(days=1))
    assert [islamic_date(d).day for d in (d13, d14, d15)] == [13, 14, 15]
    assert {islamic_date(d).month for d in (d13, d14, d15)} == {5}


def test_next_month_start_same_year() -> None:
    after = date(2026, 11, 25)
    assert islamic_date(after).year == 1448
    year, start = next_month_start(after, RAMADAN)
    assert year == 1448 and start == month_start(1448, RAMADAN) and start >= after


def test_next_month_start_rolls_to_next_year() -> None:
    after = date(2026, 11, 25)
    year, start = next_month_start(after, 1)  # Muharram 1448 already began in 2026
    assert month_start(1448, 1) < after
    assert year == 1449 and start == month_start(1449, 1) and start > after


def test_next_month_start_on_the_day() -> None:
    start = month_start(1448, RAMADAN)
    assert next_month_start(start, RAMADAN) == (1448, start)
    year, nxt = next_month_start(start + timedelta(days=1), RAMADAN)
    assert year == 1449 and nxt == month_start(1449, RAMADAN)


def test_days_until() -> None:
    assert days_until(date(2026, 12, 1), date(2026, 11, 26)) == 5
    assert days_until(date(2026, 11, 26), date(2026, 11, 26)) == 0
    assert days_until(date(2026, 11, 25), date(2026, 11, 26)) == -1
