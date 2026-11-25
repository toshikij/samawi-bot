"""Quota guard, kill switch, prayer-anchored slots, timeutil (spec §4, §8.6, §10.3)."""

from __future__ import annotations

import itertools
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from conftest import NOW
from samawi_bot.app import App
from samawi_bot.db import create_all, make_sessionmaker, session_scope
from samawi_bot.db.models import Flag
from samawi_bot.kill_switch import FLAG, KillSwitchOn, ensure_off, is_on, set_switch
from samawi_bot.publisher.quota import WARN_AT, QuotaDecision, QuotaStatus, allowed
from samawi_bot.publisher.slots import (
    KAHFI,
    SLOTS,
    WIB,
    SlotTime,
    feed_format,
    is_thursday,
    resolve_collisions,
    slot_spec,
    slots_for,
)
from samawi_bot.sources.prayer import JAKARTA, Prayer, calculate
from samawi_bot.timeutil import utc

# ---- quota ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "used,total,decision",
    [
        (0, 50, QuotaDecision.OK),
        (79, 100, QuotaDecision.OK),
        (39, 50, QuotaDecision.OK),  # 78%
        (80, 100, QuotaDecision.ESSENTIAL_ONLY),
        (40, 50, QuotaDecision.ESSENTIAL_ONLY),
        (99, 100, QuotaDecision.ESSENTIAL_ONLY),
        (100, 100, QuotaDecision.BLOCKED),
        (50, 50, QuotaDecision.BLOCKED),
        (60, 50, QuotaDecision.BLOCKED),
        (0, 0, QuotaDecision.BLOCKED),
        (0, -1, QuotaDecision.BLOCKED),
    ],
)
def test_quota_decision(used: int, total: int, decision: QuotaDecision) -> None:
    assert QuotaStatus(used, total).decision() is decision


def test_quota_ratio() -> None:
    assert QuotaStatus(40, 50).ratio == WARN_AT == 0.8
    assert QuotaStatus(3, 0).ratio == 1.0


@pytest.mark.parametrize("essential", [True, False])
def test_allowed_at_79_percent(essential: bool) -> None:
    assert allowed(QuotaStatus(79, 100), essential)


def test_allowed_at_80_percent_essential_only() -> None:
    assert allowed(QuotaStatus(80, 100), essential=True)
    assert not allowed(QuotaStatus(80, 100), essential=False)


@pytest.mark.parametrize("essential", [True, False])
def test_allowed_at_100_percent_blocked(essential: bool) -> None:
    assert not allowed(QuotaStatus(100, 100), essential)


@pytest.mark.parametrize("essential", [True, False])
def test_allowed_total_zero(essential: bool) -> None:
    assert not allowed(QuotaStatus(0, 0), essential)
    assert not allowed(QuotaStatus(0, 0), essential, needed=0)


def test_needed_more_than_remaining() -> None:
    assert allowed(QuotaStatus(10, 100), True, needed=90)
    assert not allowed(QuotaStatus(10, 100), True, needed=91)
    assert not allowed(QuotaStatus(0, 5), True, needed=6)


def test_needed_fits_but_crosses_warn_line_still_ok_below_80() -> None:
    # The decision is on current usage; `needed` only has to fit in what remains.
    assert allowed(QuotaStatus(70, 100), False, needed=20)


def test_needed_zero_blocked_when_full() -> None:
    assert not allowed(QuotaStatus(100, 100), True, needed=0)


# ---- kill switch ------------------------------------------------------------------------------


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    create_all(engine)
    return make_sessionmaker(engine)()


class _Exploding:
    """A session that must not be touched."""

    def get(self, *a: object, **k: object) -> None:
        raise AssertionError("session used although env is on")


def test_env_true_is_on_without_touching_db() -> None:
    assert is_on(_Exploding(), True)  # type: ignore[arg-type]


def test_no_flag_is_off(session: Session) -> None:
    assert not is_on(session, False)
    ensure_off(session, False)


def test_flag_on_and_off(session: Session) -> None:
    set_switch(session, True, "owner@example.com", NOW)
    assert is_on(session, False)
    with pytest.raises(KillSwitchOn, match="publishing halted"):
        ensure_off(session, False)
    set_switch(session, False, "owner@example.com", NOW)
    assert not is_on(session, False)
    ensure_off(session, False)


def test_env_wins_over_flag_off(session: Session) -> None:
    set_switch(session, False, "x", NOW)
    assert is_on(session, True)
    with pytest.raises(KillSwitchOn):
        ensure_off(session, True)


def test_set_switch_creates_then_updates(session: Session) -> None:
    set_switch(session, True, "a@example.com", NOW)
    flag = session.get(Flag, FLAG)
    assert flag is not None and (flag.value, flag.updated_by) == ("on", "a@example.com")
    later = NOW + timedelta(hours=1)
    set_switch(session, False, "b@example.com", later)
    flag = session.get(Flag, FLAG)
    assert flag is not None
    assert (flag.value, flag.updated_by, utc(flag.updated_at)) == ("off", "b@example.com", later)
    assert session.query(Flag).count() == 1


def test_unexpected_flag_value_is_not_on(session: Session) -> None:
    session.add(Flag(name=FLAG, value="paused", updated_at=NOW, updated_by=""))
    session.flush()
    assert not is_on(session, False)


class _BrokenSession:
    def get(self, *a: object, **k: object) -> None:
        raise OperationalError("SELECT flags", {}, Exception("database is down"))


def test_db_error_treated_as_on() -> None:
    assert is_on(_BrokenSession(), False)  # type: ignore[arg-type]
    with pytest.raises(KillSwitchOn):
        ensure_off(_BrokenSession(), False)  # type: ignore[arg-type]


def test_missing_table_treated_as_on(session: Session) -> None:
    session.execute(text("DROP TABLE flags"))
    assert is_on(session, False)


def test_kill_switch_via_app_session(make_app: Callable[..., App]) -> None:
    app = make_app()
    with session_scope(app.db) as s:
        set_switch(s, True, "dash", NOW)
    with session_scope(app.db) as s, pytest.raises(KillSwitchOn):
        ensure_off(s, app.settings.kill_switch)


# ---- slots ------------------------------------------------------------------------------------


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def test_full_year_jakarta_2027_ordered_and_spaced() -> None:
    days = _days(date(2027, 1, 1), date(2027, 12, 31))
    assert len(days) == 365
    for day in days:
        slots = slots_for(day)
        expected = {s.name for s in SLOTS} | ({KAHFI.name} if day.weekday() == 3 else set())
        assert {t.spec.name for t in slots} == expected, day
        assert all(t.at.tzinfo is not None and t.at.utcoffset() == timedelta(hours=7) for t in slots)
        assert all(t.at.date() == day for t in slots), day
        for a, b in itertools.pairwise(slots):
            assert b.at - a.at >= timedelta(minutes=2), (day, a, b)
        order = [t.spec.name for t in slots if t.spec.name != KAHFI.name]
        assert order == ["subuh_story", "subuh_threads", "siang_feed", "siang_threads", "malam_story", "malam_threads"], day


def test_slots_anchor_to_prayer_plus_offset() -> None:
    day = date(2027, 6, 15)
    schedule = calculate(JAKARTA, day)
    for t in slots_for(day):
        assert t.at == schedule.local(t.spec.prayer) + timedelta(minutes=t.spec.offset_min)


def test_thursday_adds_kahfi_story_after_maghrib() -> None:
    thursday = date(2026, 11, 26)
    assert is_thursday(thursday) and not is_thursday(thursday + timedelta(days=1))
    slots = slots_for(thursday)
    kahfi = [t for t in slots if t.spec.name == "kahfi_story"]
    assert len(kahfi) == 1
    maghrib = calculate(JAKARTA, thursday).local(Prayer.MAGHRIB)
    assert kahfi[0].at == maghrib + timedelta(minutes=20)
    assert kahfi[0].spec.platform == "instagram" and not kahfi[0].spec.essential
    names = [t.spec.name for t in slots]
    assert names.index("siang_threads") < names.index("kahfi_story") < names.index("malam_story")
    assert len(slots_for(thursday + timedelta(days=1))) == 6


@pytest.mark.parametrize(
    "day,fmt",
    [
        (date(2026, 11, 23), "carousel"),  # Mon
        (date(2026, 11, 24), "reel"),  # Tue
        (date(2026, 11, 25), "carousel"),  # Wed
        (date(2026, 11, 26), "reel"),  # Thu
        (date(2026, 11, 27), "carousel"),  # Fri
        (date(2026, 11, 28), "reel"),  # Sat
        (date(2026, 11, 29), "carousel"),  # Sun
        (date(2028, 2, 29), "reel"),  # leap day, a Tuesday
    ],
)
def test_feed_format_by_weekday(day: date, fmt: str) -> None:
    assert feed_format(day) == fmt


def test_ramadan_2027_slots() -> None:
    # Ramadan 1448 H starts around 8 Feb 2027; the cadence and spacing do not change.
    for day in _days(date(2027, 2, 8), date(2027, 3, 9)):
        slots = slots_for(day)
        assert len(slots) == (7 if day.weekday() == 3 else 6)
        subuh = next(t for t in slots if t.spec.name == "subuh_story")
        assert 4 <= subuh.at.hour <= 5


def test_leap_day_2028() -> None:
    slots = slots_for(date(2028, 2, 29))
    assert len(slots) == 6
    assert all(t.at.date() == date(2028, 2, 29) for t in slots)
    assert slots_for(date(2028, 3, 1))[0].at.date() == date(2028, 3, 1)


def test_resolve_collisions_same_minute() -> None:
    at = datetime(2027, 1, 1, 12, 0, tzinfo=WIB)
    a, b, c = SLOTS[2], SLOTS[3], SLOTS[4]
    out = resolve_collisions([SlotTime(b, at), SlotTime(a, at), SlotTime(c, at + timedelta(minutes=10))])
    assert [t.at for t in out] == [at, at + timedelta(minutes=2), at + timedelta(minutes=10)]
    assert {out[0].spec.name, out[1].spec.name} == {a.name, b.name}


def test_resolve_collisions_cascades() -> None:
    at = datetime(2027, 1, 1, 12, 0, tzinfo=WIB)
    out = resolve_collisions([SlotTime(s, at) for s in SLOTS[:3]] + [SlotTime(SLOTS[3], at + timedelta(minutes=5))])
    assert [t.at - at for t in out] == [timedelta(0), timedelta(minutes=2), timedelta(minutes=4), timedelta(minutes=6)]


def test_resolve_collisions_custom_gap_and_empty() -> None:
    at = datetime(2027, 1, 1, 12, 0, tzinfo=WIB)
    assert resolve_collisions([]) == []
    out = resolve_collisions([SlotTime(SLOTS[0], at), SlotTime(SLOTS[1], at + timedelta(minutes=3))], gap=timedelta(minutes=5))
    assert out[1].at == at + timedelta(minutes=5)


def test_slot_spec_lookup() -> None:
    assert slot_spec("siang_feed").essential
    assert slot_spec("siang_threads").platform == "threads"
    assert slot_spec("kahfi_story") is KAHFI
    assert not slot_spec("subuh_story").essential
    with pytest.raises(KeyError):
        slot_spec("tengah_malam")


def test_only_feed_and_its_chain_are_essential() -> None:
    assert {s.name for s in (*SLOTS, KAHFI) if s.essential} == {"siang_feed", "siang_threads"}


# ---- timeutil ---------------------------------------------------------------------------------


def test_utc_naive_is_utc() -> None:
    assert utc(datetime(2026, 1, 1, 12, 0)) == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_utc_aware_is_converted() -> None:
    wib = datetime(2026, 1, 1, 19, 0, tzinfo=timezone(timedelta(hours=7)))
    out = utc(wib)
    assert out == datetime(2026, 1, 1, 12, 0, tzinfo=UTC) and out.tzinfo is UTC
