"""Hijri dates, mirroring `app/lib/features/prayer/domain/islamic_calendar.dart` (spec §5).

Calculated dates can be a day off from the *sidang isbat*. So Ramadan and Syawal starts are never
stated as fact: `ISBAT_NOTE` must accompany them, and the checks enforce it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from hijridate import Gregorian, Hijri

MONTH_NAMES = (
    "Muharram",
    "Safar",
    "Rabiul Awal",
    "Rabiul Akhir",
    "Jumadil Awal",
    "Jumadil Akhir",
    "Rajab",
    "Syaban",
    "Ramadan",
    "Syawal",
    "Zulkaidah",
    "Zulhijjah",
)

RAMADAN = 9
SYAWAL = 10
ISBAT_NOTE = "perkiraan, menunggu sidang isbat"


@dataclass(frozen=True)
class IslamicDate:
    year: int
    month: int
    day: int
    near_boundary: bool

    @property
    def month_name(self) -> str:
        return MONTH_NAMES[self.month - 1]

    @property
    def confident(self) -> bool:
        return not self.near_boundary

    def __str__(self) -> str:
        return f"{self.day} {self.month_name} {self.year} H"


def islamic_date(civil: date, after_sunset: bool = False) -> IslamicDate:
    """The Hijri date for a civil date; after Maghrib it is already the next Hijri day."""
    if after_sunset:
        civil = civil + timedelta(days=1)
    h = Gregorian(civil.year, civil.month, civil.day).to_hijri()
    length = h.month_length()
    return IslamicDate(h.year, h.month, h.day, near_boundary=h.day <= 1 or h.day >= length - 1)


def to_gregorian(year: int, month: int, day: int) -> date:
    return date(*Hijri(year, month, day).to_gregorian().datetuple())


def month_start(year: int, month: int) -> date:
    return to_gregorian(year, month, 1)


def needs_isbat_note(year: int, month: int, day: int) -> bool:
    """Ramadan and Syawal starts (and Idulfitri) are announced by sidang isbat, never by us."""
    return month in (RAMADAN, SYAWAL) and day == 1


def ayyamul_bidh(year: int, month: int) -> tuple[date, date, date]:
    """The 13th, 14th and 15th of a Hijri month, as calculated."""
    d13 = to_gregorian(year, month, 13)
    return d13, d13 + timedelta(days=1), d13 + timedelta(days=2)


def next_month_start(after: date, month: int) -> tuple[int, date]:
    """The next calculated start of Hijri `month` on or after `after`."""
    year = islamic_date(after).year
    start = month_start(year, month)
    if start < after:
        year += 1
        start = month_start(year, month)
    return year, start


def days_until(start: date, today: date) -> int:
    return (start - today).days
