"""Prayer-anchored daily slots, in WIB from Jakarta's Kemenag times (spec §4).

| Slot           | Anchor          | Platform   | What                                         |
| subuh_story    | Subuh + 30      | Instagram  | Ayat of the day + today's prayer times        |
| subuh_threads  | Subuh + 32      | Threads    | Short reflection on that ayat                 |
| siang_feed     | Dzuhur + 45     | Instagram  | Carousel (Mon/Wed/Fri/Sun) or reel (Tue/Thu/Sat) |
| siang_threads  | Dzuhur + 50     | Threads    | The same post as a chain                      |
| malam_story    | Isya + 60       | Instagram  | Today's feed post + "Besok:" teaser           |
| malam_threads  | Isya + 62       | Threads    | Discussion question                           |
| kahfi_story    | Thu Maghrib +20 | Instagram  | Malam Jumat: Al-Kahfi reminder (Thursdays)    |

Offsets are distinct so no two slots ever share a minute; `resolve_collisions` enforces it anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..sources.prayer import JAKARTA, Prayer, calculate

WIB = JAKARTA.tz.tzinfo


@dataclass(frozen=True)
class SlotSpec:
    name: str
    prayer: Prayer
    offset_min: int
    platform: str
    sky: str
    essential: bool  # still publishes when quota is at ≥80%


SLOTS: tuple[SlotSpec, ...] = (
    SlotSpec("subuh_story", Prayer.SUBUH, 30, "instagram", "subuh", False),
    SlotSpec("subuh_threads", Prayer.SUBUH, 32, "threads", "subuh", False),
    SlotSpec("siang_feed", Prayer.DZUHUR, 45, "instagram", "dzuhur", True),
    SlotSpec("siang_threads", Prayer.DZUHUR, 50, "threads", "dzuhur", True),
    SlotSpec("malam_story", Prayer.ISYA, 60, "instagram", "isya", False),
    SlotSpec("malam_threads", Prayer.ISYA, 62, "threads", "isya", False),
)
KAHFI = SlotSpec("kahfi_story", Prayer.MAGHRIB, 20, "instagram", "maghrib", False)

CAROUSEL_DAYS = {0, 2, 4, 6}  # Mon, Wed, Fri, Sun (datetime.weekday)
REEL_DAYS = {1, 3, 5}


@dataclass(frozen=True)
class SlotTime:
    spec: SlotSpec
    at: datetime  # aware, WIB


def feed_format(day: date) -> str:
    return "carousel" if day.weekday() in CAROUSEL_DAYS else "reel"


def is_thursday(day: date) -> bool:
    return day.weekday() == 3


def slots_for(day: date) -> list[SlotTime]:
    schedule = calculate(JAKARTA, day)
    specs = list(SLOTS) + ([KAHFI] if is_thursday(day) else [])
    times = [SlotTime(s, schedule.local(s.prayer) + timedelta(minutes=s.offset_min)) for s in specs]
    return resolve_collisions(sorted(times, key=lambda t: t.at))


def resolve_collisions(times: list[SlotTime], gap: timedelta = timedelta(minutes=2)) -> list[SlotTime]:
    """Push any slot within `gap` of the previous one later, so publishes never overlap."""
    out: list[SlotTime] = []
    for t in sorted(times, key=lambda x: x.at):
        if out and t.at - out[-1].at < gap:
            t = SlotTime(t.spec, out[-1].at + gap)
        out.append(t)
    return out


def slot_spec(name: str) -> SlotSpec:
    for s in (*SLOTS, KAHFI):
        if s.name == name:
            return s
    raise KeyError(name)
