"""Computed facts: every number the bot states comes from one of these (spec §5, §6.3).

A fact key is a stored query, e.g. `prayer:Jakarta:2026-11-26:Subuh` or `quran:longest_ayah`.
The writer only sees facts the planner computed; the checks recompute every cited key and the
number check only admits numbers that a cited fact (or a cited source text) actually contains.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from ..sources import Sources
from ..sources.hijri import ISBAT_NOTE, RAMADAN, ayyamul_bidh, islamic_date, next_month_start
from ..sources.prayer import City, IndonesiaTZ, Prayer, calculate, minutes_between
from ..sources.quran import AyahRef, skeleton

# Places outside the app's city list, used only for the Sabang–Merauke comparisons. Coordinates
# are the town centres (Sabang, Aceh; Merauke, Papua Selatan).
EXTRA_PLACES = {
    "Sabang": City("Sabang", "Aceh", 5.8926, 95.3238, IndonesiaTZ.WIB),
    "Merauke": City("Merauke", "Papua Selatan", -8.4932, 140.4018, IndonesiaTZ.WIT),
}

# Stable constants with a recorded source. Not computed, so they are kept to a minimum.
CONSTANTS: dict[str, tuple[str, str]] = {
    "mushaf_pages": ("604", "Mushaf Standar Indonesia (Kemenag) dan Mushaf Madinah: 604 halaman"),
    "juz": ("30", "Pembagian Al-Qur'an menjadi 30 juz"),
    "salat_wajib": ("5", "Lima salat fardu"),
    "zakat_rate_pct": ("2,5", "Kadar zakat mal 2,5%"),
}


# Explanatory principles the writer may state, each with its source. Kept to what the app's own
# engine and Kemenag document; the judge still checks each claim against this text.
PRINCIPLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "posisi_matahari": (
        "Waktu salat ditentukan oleh posisi matahari di langit setempat, bukan oleh jam atau zona waktu "
        "(standar Kemenag/MABIMS yang dipakai mesin waktu salat Samawi)",
        (),
    ),
    "timur_lebih_dulu": (
        "Karena bumi berotasi ke arah timur, matahari terbit, melintas dan terbenam lebih dulu di wilayah yang lebih timur; Merauke lebih dulu dari Sabang",
        (),
    ),
    "zona_waktu": (
        "Indonesia punya tiga zona waktu: WIB (UTC+7), WITA (UTC+8), WIT (UTC+9); UTC adalah waktu standar dunia yang menjadi patokan semua zona waktu",
        ("3", "7", "8", "9"),
    ),
    "ihtiyat": ("Jadwal Kemenag menambahkan ihtiyat (margin kehati-hatian) 2 menit: awal waktu dimundurkan, Syuruq dimajukan", ("2",)),
    "hari_hijriah": ("Hari dalam kalender Hijriah dimulai saat Maghrib, bukan tengah malam", ()),
    "isbat": ("Awal Ramadan dan Syawal di Indonesia ditetapkan Kemenag melalui sidang isbat (hisab dan rukyat); tanggal hasil hitungan adalah perkiraan", ()),
}


@dataclass(frozen=True)
class Fact:
    key: str
    label: str
    values: tuple[str, ...]  # exactly as they may appear in copy (Indonesian formatting)
    detail: str = ""
    notes: tuple[str, ...] = field(default=())  # wording that must accompany the fact

    def as_prompt(self) -> str:
        note = f" (wajib sertakan: {'; '.join(self.notes)})" if self.notes else ""
        return f"- {self.key}: {self.label} → {', '.join(self.values)}{note}"


class UnknownFact(KeyError):
    pass


def fmt_num(x: float, decimals: int = 0) -> str:
    """Indonesian number format: 6.236 and 4,03."""
    s = f"{x:,.{decimals}f}"
    return s.replace(",", "_").replace(".", ",").replace("_", ".")


def _place(sources: Sources, name: str) -> City:
    for c in sources.cities:
        if c.name == name:
            return c
    if name in EXTRA_PLACES:
        return EXTRA_PLACES[name]
    raise UnknownFact(f"Unknown place {name!r}")


def _words(text: str) -> int:
    return len(skeleton(text).split())


def _quran_fact(sources: Sources, name: str, args: list[str]) -> Fact:
    q = sources.quran
    if name == "longest_ayah":
        ref = max(q.all_refs(), key=lambda r: _words(q.arabic(r)))
        return Fact(
            f"quran:{name}", f"Ayat terpanjang (jumlah kata): {q.surah_name(ref.surah)} ayat {ref.ayah}", (str(ref.ayah), str(_words(q.arabic(ref)))), f"{ref}"
        )
    if name == "shortest_surahs":
        least = min(s.ayah_count for s in q.surahs.values())
        names = [s.latin for s in q.surahs.values() if s.ayah_count == least]
        return Fact(f"quran:{name}", f"Surah terpendek ({least} ayat): {', '.join(names)}", (str(least),))
    if name == "totals":
        return Fact(f"quran:{name}", "Jumlah surah dan ayat (Tanzil)", ("114", fmt_num(len(q.all_refs()))))
    if name == "surah_ayahs":
        n = int(args[0])
        s = q.surahs[n]
        return Fact(f"quran:{name}:{n}", f"Jumlah ayat {s.latin}", (str(s.ayah_count), str(n)))
    if name == "basmala_count":
        n = int(args[0])
        target = skeleton(q.basmala)
        heading = 0 if n in (1, 9) else 1
        inner = sum(1 for a in range(1, q.surahs[n].ayah_count + 1) if target in skeleton(q.arabic(AyahRef(n, a))))
        return Fact(f"quran:{name}:{n}", f"Jumlah Bismillah di {q.surahs[n].latin} (pembuka + di dalam ayat)", (str(heading + inner),))
    if name == "khatam_per_prayer":
        pages, days, prayers = 604, int(args[0]) if args else 30, 5
        if days < 1:
            raise UnknownFact(f"quran:{name}:{days}: days must be at least 1")
        per = pages / days / prayers
        return Fact(
            f"quran:{name}:{days}",
            f"Halaman per salat agar khatam dalam {days} hari",
            ("604", str(days), "5", str(round(per)), fmt_num(per, 2)),
            f"{pages} ÷ {days} ÷ {prayers} = {per:.2f}",
        )
    raise UnknownFact(f"quran:{name}")


def _prayer_fact(sources: Sources, args: list[str]) -> Fact:
    city, day, prayer = _place(sources, args[0]), date.fromisoformat(args[1]), Prayer(args[2])
    s = calculate(city, day)
    return Fact(f"prayer:{city.name}:{day}:{prayer.value}", f"{prayer.value} {city.name} {day} ({city.tz.name})", (s.fmt(prayer),))


def _compare_fact(sources: Sources, args: list[str]) -> Fact:
    a, b, day, prayer = _place(sources, args[0]), _place(sources, args[1]), date.fromisoformat(args[2]), Prayer(args[3])
    ta, tb = calculate(a, day).times[prayer], calculate(b, day).times[prayer]
    diff = abs(minutes_between(ta, tb))
    hours, mins = divmod(diff, 60)
    la, lb = calculate(a, day).fmt(prayer), calculate(b, day).fmt(prayer)
    return Fact(
        f"compare:{a.name}:{b.name}:{day}:{prayer.value}",
        f"{prayer.value} {a.name} {la} {a.tz.name} vs {b.name} {lb} {b.tz.name}; selisih waktu nyata",
        (la, lb, str(diff), str(hours), str(mins), a.tz.name, b.tz.name),
        f"{diff} menit = {hours} jam {mins} menit",
    )


def _annual_fact(sources: Sources, args: list[str]) -> Fact:
    """Earliest and latest local time of a prayer in one city across a calendar year."""
    city, prayer, year = _place(sources, args[0]), Prayer(args[1]), int(args[2])
    day, times = date(year, 1, 1), {}
    while day.year == year:
        times[day] = calculate(city, day).local(prayer)
        day += timedelta(days=1)

    def clock(d: date) -> int:
        t = times[d]
        return t.hour * 60 + t.minute

    early, late = min(times, key=lambda d: (clock(d), d)), max(times, key=lambda d: (clock(d), -d.toordinal()))
    span = clock(late) - clock(early)
    return Fact(
        f"annual:{city.name}:{prayer.value}:{year}",
        f"{prayer.value} {city.name} ({city.tz.name}) sepanjang {year}: paling awal {times[early]:%H:%M} ({early:%d/%m}), "
        f"paling akhir {times[late]:%H:%M} ({late:%d/%m})",
        (f"{times[early]:%H:%M}", f"{times[late]:%H:%M}", str(span), str(year), str(early.day), str(late.day)),
        f"selisih {span} menit",
    )


def _cross_fact(sources: Sources, args: list[str]) -> Fact:
    """Two prayers in two cities on one day, as real instants: which comes first, and by how much."""
    a, pa, b, pb, day = _place(sources, args[0]), Prayer(args[1]), _place(sources, args[2]), Prayer(args[3]), date.fromisoformat(args[4])
    ta, tb = calculate(a, day), calculate(b, day)
    diff = minutes_between(ta.times[pa], tb.times[pb])
    first = f"{pa.value} {a.name}" if diff > 0 else f"{pb.value} {b.name}"
    gap = abs(diff)
    return Fact(
        f"cross:{a.name}:{pa.value}:{b.name}:{pb.value}:{day}",
        f"{pa.value} {a.name} {ta.fmt(pa)} {a.tz.name} vs {pb.value} {b.name} {tb.fmt(pb)} {b.tz.name}: "
        f"{first} terjadi lebih dulu, selisih waktu nyata {gap} menit",
        (ta.fmt(pa), tb.fmt(pb), str(gap), *map(str, divmod(gap, 60)), a.tz.name, b.tz.name),
        f"{first} lebih dulu {gap} menit",
    )


def _extremes_fact(sources: Sources, args: list[str]) -> Fact:
    day, prayer = date.fromisoformat(args[0]), Prayer(args[1])
    places = list(sources.cities) + list(EXTRA_PLACES.values())
    times = {c.name: calculate(c, day).times[prayer] for c in places}
    first, last = min(times, key=lambda n: times[n]), max(times, key=lambda n: times[n])
    diff = minutes_between(times[first], times[last])
    fh, fm = divmod(diff, 60)
    return Fact(
        f"extremes:{day}:{prayer.value}",
        f"{prayer.value} paling awal ({first}) dan paling akhir ({last}) di Indonesia",
        (str(diff), str(fh), str(fm)),
        f"{first} → {last}: {diff} menit",
    )


def _hijri_fact(today: date, name: str, args: list[str]) -> Fact:
    if name == "today":
        h = islamic_date(today)
        notes = (ISBAT_NOTE,) if h.near_boundary else ()
        return Fact(f"hijri:today:{today}", f"Tanggal Hijriah {today}", (str(h.day), str(h.year)), str(h), notes)
    if name == "ayyamul_bidh":
        h = islamic_date(today)
        year, month = h.year, h.month
        if h.day > 15:
            month = month % 12 + 1
            year = year + (1 if month == 1 else 0)
        days = ayyamul_bidh(year, month)
        return Fact(
            f"hijri:ayyamul_bidh:{today}",
            "Ayyamul Bidh (13–15) bulan ini, perkiraan kalender",
            (*tuple(str(d.day) for d in days), "13", "14", "15"),
            ", ".join(d.isoformat() for d in days),
        )
    if name == "days_to_ramadan":
        year, start = next_month_start(today, RAMADAN)
        n = (start - today).days
        return Fact(f"hijri:days_to_ramadan:{today}", f"Hari menuju 1 Ramadan {year} H", (str(n), str(year)), start.isoformat(), (ISBAT_NOTE,))
    raise UnknownFact(f"hijri:{name}")


def compute(sources: Sources, key: str, today: date | None = None) -> Fact:
    """Recompute a fact from its key. Raises UnknownFact for anything not in the registry."""
    parts = key.split(":")
    kind, rest = parts[0], parts[1:]
    try:
        if kind == "quran" and rest:
            return _quran_fact(sources, rest[0], rest[1:])
        if kind == "prayer" and len(rest) == 3:
            return _prayer_fact(sources, rest)
        if kind == "compare" and len(rest) == 4:
            return _compare_fact(sources, rest)
        if kind == "extremes" and len(rest) == 2:
            return _extremes_fact(sources, rest)
        if kind == "annual" and len(rest) == 3:
            return _annual_fact(sources, rest)
        if kind == "cross" and len(rest) == 5:
            return _cross_fact(sources, rest)
        if kind == "hijri" and len(rest) == 2:
            return _hijri_fact(date.fromisoformat(rest[1]), rest[0], [])
        if kind == "const" and len(rest) == 1 and rest[0] in CONSTANTS:
            value, source = CONSTANTS[rest[0]]
            return Fact(key, source, (value,))
        if kind == "prinsip" and len(rest) == 1 and rest[0] in PRINCIPLES:
            text, values = PRINCIPLES[rest[0]]
            return Fact(key, text, values)
    except (ValueError, KeyError, IndexError, ArithmeticError) as exc:  # ArithmeticError: dates outside the calendars' range
        if isinstance(exc, UnknownFact):
            raise
        raise UnknownFact(f"{key}: {exc}") from exc
    raise UnknownFact(key)


Builder = Callable[[Sources, date], list[str]]


def fact_pack(sources: Sources, pillar: str, day: date) -> list[Fact]:
    """The facts the writer may use for a pillar on a given day."""
    d = day.isoformat()
    keys: list[str] = [f"hijri:today:{d}"]
    if pillar == "langit_waktu_salat":
        keys += [f"compare:Sabang:Merauke:{d}:{p.value}" for p in (Prayer.SUBUH, Prayer.MAGHRIB)]
        keys += [f"extremes:{d}:{p.value}" for p in (Prayer.SUBUH, Prayer.MAGHRIB, Prayer.ISYA)]
        keys += [f"prayer:{c}:{d}:{p.value}" for c in ("Jakarta", "Banda Aceh", "Jayapura") for p in Prayer]
        keys += [f"annual:{c}:{p.value}:{day.year}" for c in ("Jakarta", "Banda Aceh") for p in (Prayer.SUBUH, Prayer.MAGHRIB)]
        keys += [f"cross:Jayapura:Isya:Banda Aceh:Maghrib:{d}", f"cross:Jayapura:Isya:Sabang:Maghrib:{d}", f"cross:Merauke:Maghrib:Jakarta:Ashar:{d}"]
        keys += [f"prinsip:{k}" for k in ("posisi_matahari", "timur_lebih_dulu", "zona_waktu", "ihtiyat")]
    elif pillar == "fakta_quran":
        keys += [
            "quran:longest_ayah",
            "quran:shortest_surahs",
            "quran:totals",
            "quran:basmala_count:27",
            "quran:basmala_count:9",
            "quran:khatam_per_prayer:30",
            "const:mushaf_pages",
            "const:juz",
        ]
    elif pillar == "hitung_ibadah":
        keys += ["quran:khatam_per_prayer:30", "quran:khatam_per_prayer:60", "const:mushaf_pages", "const:zakat_rate_pct", "const:juz"]
    elif pillar == "kalender_hijriah":
        keys += [f"hijri:ayyamul_bidh:{d}", f"hijri:days_to_ramadan:{d}", "prinsip:hari_hijriah", "prinsip:isbat"]
    elif pillar == "sunnah_terlupa":
        keys += [f"hijri:ayyamul_bidh:{d}", "prinsip:hari_hijriah"]
    elif pillar == "musafir":
        keys += ["prinsip:zona_waktu", "prinsip:posisi_matahari"]
    return [compute(sources, k, day) for k in keys]


def prayer_times_today(sources: Sources, city_name: str, day: date) -> dict[str, str]:
    s = calculate(_place(sources, city_name), day)
    return {p.value: s.fmt(p) for p in Prayer}


def now_wib() -> datetime:
    return datetime.now(IndonesiaTZ.WIB.tzinfo)
