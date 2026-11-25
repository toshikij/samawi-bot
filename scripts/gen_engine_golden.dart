import 'dart:convert';
import 'dart:io';
import 'package:samawi/features/prayer/domain/kemenag_prayer_calculator.dart';
import 'package:samawi/features/prayer/domain/indonesian_city.dart';
import 'package:samawi/features/prayer/domain/prayer_schedule.dart';
import 'package:samawi/features/prayer/domain/islamic_calendar.dart';
import 'package:samawi/features/prayer/domain/indonesia_time_zone.dart';

void main() {
  const calc = KemenagPrayerCalculator();
  final out = <String, dynamic>{};
  final prayers = <Map<String, dynamic>>[];
  // Every city, every 7th day of 2026-2028 (incl. leap day), plus all of 2027 for Jakarta.
  final start = DateTime.utc(2026, 1, 1);
  for (final city in IndonesianCities.all) {
    for (var d = 0; d < 365 * 3 + 1; d++) {
      final date = start.add(Duration(days: d));
      final daily = city.name == 'Jakarta';
      if (!daily && d % 7 != 0 && !(date.month == 2 && date.day == 29)) continue;
      final s = calc.calculate(latitude: city.latitude, longitude: city.longitude, date: date, timeZone: city.timeZone);
      prayers.add({
        'city': city.name,
        'date': '${date.year.toString().padLeft(4,'0')}-${date.month.toString().padLeft(2,'0')}-${date.day.toString().padLeft(2,'0')}',
        for (final p in PrayerName.values) p.label: s.format(p),
      });
    }
  }
  out['prayer'] = prayers;
  final hijri = <Map<String, dynamic>>[];
  for (var d = 0; d < 365 * 3 + 1; d++) {
    final date = start.add(Duration(days: d));
    final noon = DateTime.utc(date.year, date.month, date.day, 5); // 12:00 WIB
    final h = IslamicCalendar.dateFor(noon, timeZone: IndonesiaTimeZone.wib);
    hijri.add({'date': date.toIso8601String().substring(0, 10), 'y': h.year, 'm': h.month, 'd': h.day, 'near': h.isNearMonthBoundary});
  }
  out['hijri'] = hijri;
  out['cities'] = [for (final c in IndonesianCities.all) {'name': c.name, 'province': c.province, 'lat': c.latitude, 'lon': c.longitude, 'tz': c.timeZone.abbreviation}];
  stdout.write(jsonEncode(out));
}
