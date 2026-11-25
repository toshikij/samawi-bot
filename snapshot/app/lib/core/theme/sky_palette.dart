import 'package:flutter/material.dart';

import '../../features/prayer/domain/prayer_schedule.dart';

/// The sky at one moment of the day.
///
/// Samawi (سماوي) means "celestial". The interface reflects the sky at the
/// current time — see `docs/04_Design_Guidelines.md` §1. This is functional as
/// well as decorative: the user can tell which prayer period they are in before
/// reading any text, which matters for low-literacy and low-vision users.
@immutable
class SkyPalette {
  const SkyPalette({
    required this.top,
    required this.bottom,
    required this.onSky,
    required this.onSkyMuted,
    required this.accent,
    required this.isDark,
    this.mid,
    this.requiresScrim = false,
  });

  /// Gradient stops for the sky backdrop, top to horizon.
  final Color top;
  final Color bottom;

  /// Optional middle stop. Only Maghrib has one — the brand samples it at
  /// three points because sunset is the one moment a two-stop ramp cannot
  /// describe.
  final Color? mid;

  /// Whether text over this gradient needs a scrim behind it.
  ///
  /// Set where **no single foreground reaches 4.5:1 against every stop**. On
  /// Maghrib the best available colour manages 2.31:1, because the gradient
  /// runs from amber through rose to violet and nothing is simultaneously
  /// readable on all three. The brand book states foregrounds are "guaranteed
  /// to meet WCAG AA"; measured against the published hex values that is not
  /// true here, so the flag records the gap rather than hiding it.
  ///
  /// Text over a scrimmed palette must sit on [panel], a chip, or a scrim —
  /// never directly on the gradient.
  final bool requiresScrim;

  /// Primary foreground.
  ///
  /// Meets 4.5:1 against every stop **unless** [requiresScrim] is set, in which
  /// case the palette has no such colour and the UI must supply a scrim. The
  /// measured ratios are asserted in `sky_palette_contrast_test.dart`, so this
  /// claim cannot quietly rot.
  final Color onSky;

  /// Secondary foreground for supporting text. Meets 4.5:1 on the dark
  /// palettes; 3:1 (large text only) on the light ones.
  final Color onSkyMuted;

  /// The single warm accent. Marks the *next* prayer and primary actions only —
  /// design guidelines §3: if everything is accented, nothing is.
  final Color accent;

  /// Whether this moment needs light-on-dark treatment.
  final bool isDark;

  /// Brass, from the brand palette. One accent across all palettes so the
  /// brand reads as continuous while the sky moves underneath it.
  static const Color _brass = Color(0xFFC79A55);
  static const Color _brassLight = Color(0xFFE6C68B);

  /// Deep brass. Part of the brand ramp; unused as an accent since v3 moved
  /// light skies to deep ink, and kept so the ramp stays complete for anything
  /// that needs a mid-weight brass on a panel.
  static const Color brassDeep = Color(0xFFA87C36);

  /// The light-sky accent, added by brand v3.
  ///
  /// Brass is a **dark-sky accent only**. On the pale gold, amber and rose ends
  /// of Syuruq, Ashar and Maghrib no brass shade reaches AA — brass on gold is
  /// the same hue at similar lightness, so it is a hue collision rather than a
  /// tuning problem. v3 resolves it by switching the accent on light surfaces
  /// to deep ink, which measures 11.26:1 on Syuruq's gold, 7.83:1 on Ashar's
  /// tan and 7.87:1 on Maghrib's amber.
  ///
  /// It does *not* clear AA against the blue tops of those same gradients
  /// (3.07:1 on Syuruq), which is why those palettes still carry
  /// [requiresScrim]: no single accent spans a gradient that runs from blue to
  /// gold. On a panel — where the accent actually spends most of its life — it
  /// is far clear of AA.
  static const Color _deepInk = Color(0xFF0B1430);

  /// Brand neutrals.
  static const Color ink = Color(0xFF070B14);
  static const Color panelDark = Color(0xFF0F1626);
  static const Color line = Color(0xFF1E293F);
  static const Color mutedText = Color(0xFF93A0BA);
  static const Color onDarkText = Color(0xFFEEF2F9);
  static const Color paper = Color(0xFFF6F4EE);

  /// Pre-dawn: dark, still, cold. The screen a user sees at 04:30.
  static const subuh = SkyPalette(
    top: Color(0xFF0E1B44),
    bottom: Color(0xFF24407F),
    onSky: onDarkText, // 8.84:1 on the lighter stop
    // Brand muted (#93A0BA) measures 3.77:1 here, below AA for body text, so
    // this palette lightens it. Isya keeps the brand value, which passes.
    onSkyMuted: Color(0xFFA8B6CE), // 4.85:1
    accent: _brassLight, // 3.87:1 — large text and icons only
    isDark: true,
  );

  /// Sunrise: the horizon opening, cold blue lifting into pale gold.
  static const syuruq = SkyPalette(
    top: Color(0xFF3E63A8),
    bottom: Color(0xFFE9C87F),
    onSky: ink, // 3.33:1 at the blue end — large text only
    onSkyMuted: Color(0xFF2A3550),
    accent: _deepInk,
    isDark: false,
    requiresScrim: true,
  );

  /// Midday: the brightest, most neutral moment.
  static const dzuhur = SkyPalette(
    top: Color(0xFF2F72C4),
    bottom: Color(0xFF6EAAE6),
    onSky: ink, // 4.05:1 at the darker stop — just below AA body
    onSkyMuted: Color(0xFF17263C),
    accent: _deepInk,
    isDark: false,
    requiresScrim: true,
  );

  /// Afternoon: light lengthening, warmth entering the blue.
  static const ashar = SkyPalette(
    top: Color(0xFF4E7DB6),
    bottom: Color(0xFFCBA46A),
    onSky: ink, // 4.62:1 worst case — the only light sky that passes outright
    onSkyMuted: Color(0xFF1B2436),
    accent: _deepInk,
    isDark: false,
  );

  /// Sunset: the most saturated moment of the day, amber through rose to
  /// violet. The brand's only three-stop gradient.
  static const maghrib = SkyPalette(
    top: Color(0xFFE79A44),
    mid: Color(0xFFD06A78),
    bottom: Color(0xFF4E356E),
    // Nothing spans amber-to-violet: white manages 2.31:1, ink 1.93:1. Text
    // here must sit on a scrim or a panel.
    onSky: Color(0xFFFFFFFF),
    onSkyMuted: Color(0xFFF0E6EE),
    accent: _brassLight,
    isDark: true,
    requiresScrim: true,
  );

  /// Night: quiet and low-light. Not true black — that smears on OLED during
  /// scroll, and a lot of Quran reading happens here.
  static const isya = SkyPalette(
    top: Color(0xFF0B1430),
    bottom: Color(0xFF172346),
    onSky: onDarkText, // 13.70:1
    onSkyMuted: mutedText, // 5.84:1 — brand value, passes here
    accent: _brassLight, // 5.99:1
    isDark: true,
  );

  /// The palette for the period [prayer] opens.
  static SkyPalette forPrayer(PrayerName prayer) => switch (prayer) {
    PrayerName.subuh => subuh,
    PrayerName.syuruq => syuruq,
    PrayerName.dzuhur => dzuhur,
    PrayerName.ashar => ashar,
    PrayerName.maghrib => maghrib,
    PrayerName.isya => isya,
  };

  /// Every palette, in the order they occur through a day.
  static const List<SkyPalette> all = [
    subuh,
    syuruq,
    dzuhur,
    ashar,
    maghrib,
    isya,
  ];

  /// The brand accent, independent of time of day. Used where a palette is not
  /// in scope (app icon tinting, splash).
  static const Color brand = _brass;

  /// Blends toward [other]. Used for the gradual sky transition — design
  /// guidelines §7: light changes, it does not cut.
  SkyPalette lerpTo(SkyPalette other, double t) => SkyPalette(
    top: Color.lerp(top, other.top, t)!,
    bottom: Color.lerp(bottom, other.bottom, t)!,
    mid: Color.lerp(mid ?? bottom, other.mid ?? other.bottom, t),
    onSky: Color.lerp(onSky, other.onSky, t)!,
    onSkyMuted: Color.lerp(onSkyMuted, other.onSkyMuted, t)!,
    accent: Color.lerp(accent, other.accent, t)!,
    isDark: t < 0.5 ? isDark : other.isDark,
    // Conservative: a blend involving a scrimmed palette is itself scrimmed,
    // because the intermediate colours are no safer than the ends.
    requiresScrim: requiresScrim || other.requiresScrim,
  );

  /// The backdrop gradient for this moment.
  LinearGradient get gradient => LinearGradient(
    begin: Alignment.topCenter,
    end: Alignment.bottomCenter,
    colors: mid == null ? [top, bottom] : [top, mid!, bottom],
  );

  /// Background for full-height panels (bottom sheets, dialogs).
  ///
  /// Deliberately **not** a gradient stop. Reusing [bottom] tints the whole
  /// sheet with the horizon — at Maghrib that turns a list of city names
  /// amber, which is both ugly and a contrast risk. Panels get a neutral
  /// surface so long-form content stays readable at every hour.
  Color get panel => isDark ? panelDark : paper;

  /// Primary text on [panel].
  Color get onPanel => isDark ? onDarkText : ink;

  /// Secondary text on [panel]. Meets AA for body text.
  Color get onPanelMuted => isDark ? mutedText : const Color(0xFF5A6478);
}
