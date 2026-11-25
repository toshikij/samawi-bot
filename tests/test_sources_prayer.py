"""Kemenag prayer times: the Python port must match the app's Dart engine minute-for-minute."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, timezone

import adhanpy.PrayerTimes as adhan_pt
import pytest

from conftest import FIXTURES
from samawi_bot.sources import Sources
from samawi_bot.sources.prayer import (
    IHTIYAT,
    JAKARTA,
    City,
    IndonesiaTZ,
    Prayer,
    _ceil_minute,
    _floor_minute,
    apply_ihtiyat,
    calculate,
    dart_rounded_minute,
    local_noon,
    minutes_between,
    parse_cities,
)


@pytest.fixture(scope="module")
def golden() -> dict:
    return json.loads((FIXTURES / "app_engine_golden.json").read_text("utf-8"))


@pytest.fixture(scope="module")
def cities(sources: Sources) -> dict[str, City]:
    return {c.name: c for c in sources.cities}


def test_every_golden_row_matches_the_app_engine(golden: dict, cities: dict[str, City]) -> None:
    rows = golden["prayer"]
    assert len(rows) == 5204
    mismatches: list[str] = []
    for row in rows:
        city = cities[row["city"]]
        sched = calculate(city, date.fromisoformat(row["date"]))
        for p in Prayer:
            got = sched.fmt(p)
            if got != row[p.value]:
                mismatches.append(f"{row['city']} {row['date']} {p.value}: python {got}, app {row[p.value]}")
    assert not mismatches, f"{len(mismatches)} times differ from the app engine, e.g.:\n" + "\n".join(mismatches[:20])


def test_golden_cities_match_parsed_cities(golden: dict, cities: dict[str, City]) -> None:
    for g in golden["cities"]:
        c = cities[g["name"]]
        assert (c.province, c.latitude, c.longitude, c.tz.name) == (g["province"], g["lat"], g["lon"], g["tz"]), g["name"]
    assert cities["Jakarta"] == JAKARTA
    assert {c.tz for c in cities.values()} == set(IndonesiaTZ)


def test_full_year_jakarta_is_ordered(golden: dict) -> None:
    day = date(2027, 1, 1)
    while day.year == 2027:
        s = calculate(JAKARTA, day)
        times = [s.times[p] for p in Prayer]
        assert times == sorted(times) and len(set(times)) == 6, day
        assert all(t.tzinfo is UTC and t.second == 0 and t.microsecond == 0 for t in times), day
        assert s.local(Prayer.SUBUH).date() == day
        day += timedelta(days=1)


def test_leap_day_2028() -> None:
    s = calculate(JAKARTA, date(2028, 2, 29))
    assert s.day == date(2028, 2, 29)
    assert {p.value: s.fmt(p) for p in Prayer} == {
        "Subuh": "04:42",
        "Syuruq": "05:56",
        "Dzuhur": "12:07",
        "Ashar": "15:12",
        "Maghrib": "18:14",
        "Isya": "19:24",
    }


def test_wita_and_wit_local_times(golden: dict, cities: dict[str, City]) -> None:
    for name, tz in (("Makassar", IndonesiaTZ.WITA), ("Jayapura", IndonesiaTZ.WIT)):
        s = calculate(cities[name], date(2026, 1, 1))
        assert s.local(Prayer.DZUHUR).utcoffset() == timedelta(hours=tz.value)
        assert s.local(Prayer.DZUHUR).tzname() == tz.name


def test_schedule_local_is_city_timezone() -> None:
    s = calculate(JAKARTA, date(2026, 11, 26))
    local = s.local(Prayer.MAGHRIB)
    assert local.tzinfo == IndonesiaTZ.WIB.tzinfo
    assert local == s.times[Prayer.MAGHRIB]
    assert s.fmt(Prayer.MAGHRIB) == local.strftime("%H:%M")


# ── Rounding ─────────────────────────────────────────────────────────────────────────────────


def _t(h: int, m: int, s: int = 0, us: int = 0) -> datetime:
    return datetime(2026, 3, 1, h, m, s, us, tzinfo=UTC)


def test_adhanpy_rounding_is_patched_to_dart() -> None:
    assert adhan_pt.rounded_minute is dart_rounded_minute


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        (_t(5, 10, 29), _t(5, 10)),
        (_t(5, 10, 30), _t(5, 11)),  # exactly 30 s rounds up (Dart), not to even (Python round)
        (_t(5, 11, 30), _t(5, 12)),
        (_t(5, 10, 0), _t(5, 10)),
        (_t(5, 10, 59), _t(5, 11)),
        (_t(11, 59, 30), _t(12, 0)),  # carries into the next hour
        (_t(23, 59, 45), datetime(2026, 3, 2, 0, 0, tzinfo=UTC)),  # and the next day
        (_t(5, 10, 29, 999_999), _t(5, 10, 0, 999_999)),  # sub-second part kept, as in Dart
        (_t(5, 10, 30, 500_000), _t(5, 11, 0, 500_000)),
    ],
)
def test_dart_rounded_minute(when: datetime, expected: datetime) -> None:
    assert dart_rounded_minute(when) == expected


def test_floor_and_ceil_minute() -> None:
    assert _floor_minute(_t(5, 10, 59, 1)) == _t(5, 10)
    assert _ceil_minute(_t(5, 10)) == _t(5, 10)
    assert _ceil_minute(_t(5, 10, 0, 1)) == _t(5, 11)
    assert _ceil_minute(_t(23, 59, 1)) == datetime(2026, 3, 2, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize("prayer", [p for p in Prayer if p.is_window_start])
def test_ihtiyat_moves_starts_later_and_rounds_up(prayer: Prayer) -> None:
    assert apply_ihtiyat(_t(12, 0), prayer) == _t(12, 2)  # an exact minute is not pushed further
    assert apply_ihtiyat(_t(12, 0, 1), prayer) == _t(12, 3)
    assert apply_ihtiyat(_t(12, 0, 0, 1), prayer) == _t(12, 3)
    assert apply_ihtiyat(_t(12, 0, 59), prayer) == _t(12, 3)


def test_ihtiyat_moves_syuruq_earlier_and_rounds_down() -> None:
    assert apply_ihtiyat(_t(6, 0), Prayer.SYURUQ) == _t(5, 58)
    assert apply_ihtiyat(_t(6, 0, 59), Prayer.SYURUQ) == _t(5, 58)
    assert apply_ihtiyat(_t(6, 0, 0, 1), Prayer.SYURUQ) == _t(5, 58)
    assert apply_ihtiyat(_t(6, 1), Prayer.SYURUQ) == _t(5, 59)


def test_ihtiyat_custom_amount() -> None:
    assert timedelta(minutes=2) == IHTIYAT
    assert apply_ihtiyat(_t(12, 0), Prayer.ASHAR, timedelta(0)) == _t(12, 0)
    assert apply_ihtiyat(_t(6, 0, 30), Prayer.SYURUQ, timedelta(0)) == _t(6, 0)


def test_prayer_enum() -> None:
    assert [p.value for p in Prayer] == ["Subuh", "Syuruq", "Dzuhur", "Ashar", "Maghrib", "Isya"]
    assert not Prayer.SYURUQ.is_window_start
    assert all(p.is_window_start for p in Prayer if p is not Prayer.SYURUQ)


def test_timezones() -> None:
    assert IndonesiaTZ.WIB.tzinfo == timezone(timedelta(hours=7))
    assert IndonesiaTZ.WITA.tzinfo.utcoffset(None) == timedelta(hours=8)
    assert IndonesiaTZ.WIT.tzinfo.tzname(None) == "WIT"


# ── City parsing ─────────────────────────────────────────────────────────────────────────────


def test_parse_cities_requires_jakarta() -> None:
    with pytest.raises(ValueError, match="without Jakarta"):
        parse_cities("")
    surabaya = "IndonesianCity( name: 'Surabaya', province: 'Jawa Timur', latitude: -7.2575, longitude: 112.7521, timeZone: IndonesiaTimeZone.wib"
    with pytest.raises(ValueError, match="without Jakarta"):
        parse_cities(surabaya)


def test_parse_cities_minimal() -> None:
    src = (
        "IndonesianCity(\n name: 'Jakarta',\n province: 'DKI Jakarta',\n latitude: -6.2088,\n longitude: 106.8456,\n"
        " timeZone: IndonesiaTimeZone.wib),\n"
        "IndonesianCity(name: 'Jayapura', province: 'Papua', latitude: -2.5337, longitude: 140.7181, timeZone: IndonesiaTimeZone.wit)"
    )
    cities = parse_cities(src)
    assert cities == [JAKARTA, City("Jayapura", "Papua", -2.5337, 140.7181, IndonesiaTZ.WIT)]


def test_real_cities_file(sources: Sources) -> None:
    assert len(sources.cities) == 27
    assert sources.cities[0] == JAKARTA


# ── Helpers ──────────────────────────────────────────────────────────────────────────────────


def test_local_noon_and_minutes_between() -> None:
    noon = local_noon(date(2026, 11, 26))
    assert noon == datetime(2026, 11, 26, 5, 0, tzinfo=UTC)
    assert local_noon(date(2026, 11, 26), IndonesiaTZ.WIT) == datetime(2026, 11, 26, 3, 0, tzinfo=UTC)
    assert minutes_between(noon, noon + timedelta(minutes=90)) == 90
    assert minutes_between(noon + timedelta(minutes=5), noon) == -5
    assert minutes_between(noon, noon + timedelta(seconds=89)) == 1
