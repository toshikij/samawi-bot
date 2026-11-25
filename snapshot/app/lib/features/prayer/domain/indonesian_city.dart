import 'indonesia_time_zone.dart';

/// A city the user can pick as their prayer location.
///
/// Ships bundled rather than fetched: picking a city must work with no network
/// and no location permission, which is the first thing a new user does.
class IndonesianCity {
  const IndonesianCity({
    required this.name,
    required this.province,
    required this.latitude,
    required this.longitude,
    required this.timeZone,
  });

  final String name;
  final String province;
  final double latitude;
  final double longitude;
  final IndonesiaTimeZone timeZone;

  /// Match against a search query, case- and accent-insensitively enough for
  /// Indonesian place names.
  bool matches(String query) {
    final q = query.trim().toLowerCase();
    if (q.isEmpty) return true;
    return name.toLowerCase().contains(q) || province.toLowerCase().contains(q);
  }

  @override
  String toString() => '$name, $province';
}

/// Major cities across all three Indonesian time zones.
///
/// Deliberately spans WIB/WITA/WIT rather than being Java-centric — a Jakarta
/// default with no eastern coverage is a common failing in Indonesian apps.
abstract final class IndonesianCities {
  static const jakarta = IndonesianCity(
    name: 'Jakarta',
    province: 'DKI Jakarta',
    latitude: -6.2088,
    longitude: 106.8456,
    timeZone: IndonesiaTimeZone.wib,
  );

  static const all = <IndonesianCity>[
    jakarta,
    // — WIB —
    IndonesianCity(
      name: 'Surabaya',
      province: 'Jawa Timur',
      latitude: -7.2575,
      longitude: 112.7521,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Bandung',
      province: 'Jawa Barat',
      latitude: -6.9175,
      longitude: 107.6191,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Medan',
      province: 'Sumatera Utara',
      latitude: 3.5952,
      longitude: 98.6722,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Semarang',
      province: 'Jawa Tengah',
      latitude: -6.9932,
      longitude: 110.4203,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Palembang',
      province: 'Sumatera Selatan',
      latitude: -2.9761,
      longitude: 104.7754,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Yogyakarta',
      province: 'DI Yogyakarta',
      latitude: -7.7956,
      longitude: 110.3695,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Banda Aceh',
      province: 'Aceh',
      latitude: 5.5483,
      longitude: 95.3238,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Pontianak',
      province: 'Kalimantan Barat',
      latitude: -0.0263,
      longitude: 109.3425,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Padang',
      province: 'Sumatera Barat',
      latitude: -0.9471,
      longitude: 100.4172,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Malang',
      province: 'Jawa Timur',
      latitude: -7.9666,
      longitude: 112.6326,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Bogor',
      province: 'Jawa Barat',
      latitude: -6.5971,
      longitude: 106.8060,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Batam',
      province: 'Kepulauan Riau',
      latitude: 1.0456,
      longitude: 104.0305,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Pekanbaru',
      province: 'Riau',
      latitude: 0.5071,
      longitude: 101.4478,
      timeZone: IndonesiaTimeZone.wib,
    ),
    IndonesianCity(
      name: 'Bandar Lampung',
      province: 'Lampung',
      latitude: -5.3971,
      longitude: 105.2668,
      timeZone: IndonesiaTimeZone.wib,
    ),
    // — WITA —
    IndonesianCity(
      name: 'Makassar',
      province: 'Sulawesi Selatan',
      latitude: -5.1477,
      longitude: 119.4327,
      timeZone: IndonesiaTimeZone.wita,
    ),
    IndonesianCity(
      name: 'Denpasar',
      province: 'Bali',
      latitude: -8.6705,
      longitude: 115.2126,
      timeZone: IndonesiaTimeZone.wita,
    ),
    IndonesianCity(
      name: 'Balikpapan',
      province: 'Kalimantan Timur',
      latitude: -1.2379,
      longitude: 116.8529,
      timeZone: IndonesiaTimeZone.wita,
    ),
    IndonesianCity(
      name: 'Manado',
      province: 'Sulawesi Utara',
      latitude: 1.4748,
      longitude: 124.8421,
      timeZone: IndonesiaTimeZone.wita,
    ),
    IndonesianCity(
      name: 'Mataram',
      province: 'Nusa Tenggara Barat',
      latitude: -8.5833,
      longitude: 116.1167,
      timeZone: IndonesiaTimeZone.wita,
    ),
    IndonesianCity(
      name: 'Kupang',
      province: 'Nusa Tenggara Timur',
      latitude: -10.1772,
      longitude: 123.6070,
      timeZone: IndonesiaTimeZone.wita,
    ),
    IndonesianCity(
      name: 'Banjarmasin',
      province: 'Kalimantan Selatan',
      latitude: -3.3194,
      longitude: 114.5906,
      timeZone: IndonesiaTimeZone.wita,
    ),
    IndonesianCity(
      name: 'Samarinda',
      province: 'Kalimantan Timur',
      latitude: -0.5022,
      longitude: 117.1536,
      timeZone: IndonesiaTimeZone.wita,
    ),
    // — WIT —
    IndonesianCity(
      name: 'Jayapura',
      province: 'Papua',
      latitude: -2.5337,
      longitude: 140.7181,
      timeZone: IndonesiaTimeZone.wit,
    ),
    IndonesianCity(
      name: 'Ambon',
      province: 'Maluku',
      latitude: -3.6954,
      longitude: 128.1814,
      timeZone: IndonesiaTimeZone.wit,
    ),
    IndonesianCity(
      name: 'Sorong',
      province: 'Papua Barat Daya',
      latitude: -0.8762,
      longitude: 131.2558,
      timeZone: IndonesiaTimeZone.wit,
    ),
    IndonesianCity(
      name: 'Ternate',
      province: 'Maluku Utara',
      latitude: 0.7900,
      longitude: 127.3856,
      timeZone: IndonesiaTimeZone.wit,
    ),
  ];

  /// Cities matching [query], in declaration order.
  static List<IndonesianCity> search(String query) =>
      all.where((c) => c.matches(query)).toList();
}
