/// Whether a feature is free forever or requires a subscription.
enum FeatureTier {
  /// Available to everyone, always — including after a trial expires and after
  /// a subscription lapses. Never gated for any reason.
  free,

  /// Included in the free trial; requires a subscription afterwards.
  paid,
}

/// Whether a feature actually exists in the app yet.
///
/// This enum lists everything the free/paid line has been *decided* for,
/// including things not built. That is useful for planning and dangerous for
/// selling: the paywall was advertising ten benefits of which six did not
/// exist, so a subscriber would have received four of the ten things they were
/// shown. Honest billing (PRD §5.4) is not only about the price.
///
/// So availability is now explicit, the paywall derives its list from it, and a
/// test asserts nothing unbuilt can be advertised.
enum FeatureStatus {
  /// Built, reachable, and working.
  shipped,

  /// Decided but not built. Never advertised.
  planned,

  /// Built or obtainable, but held back for a reason recorded elsewhere —
  /// currently tafsir and extra translations, both on licensing.
  blocked,
}

/// Two features were removed from this enum rather than left as roadmap, on
/// 2026-07-23, and the reasons are worth keeping so nobody proposes them again:
///
/// * **`silentModeAutomation`** — "silence the phone during prayer". **iOS
///   provides no API for it at all**; an app cannot mute the ringer. Android
///   can, via DND policy access. Shipping it would mean a paid feature that
///   works for half the userbase and silently does nothing for the other half,
///   which is the honesty problem this file exists to prevent, one layer up.
///   Mode Jumat's pre-khutbah *reminder* is the honest version: it asks the
///   user to silence their phone rather than pretending the app can.
/// * **`ramadanPlanner`** — "plan a full Quran reading to finish by a chosen
///   date". That is [khatamPlanner], which already ships. It was a duplicate
///   entry, not a feature.
///
/// Every gateable capability in Samawi, and which side of the line it sits on.
///
/// ## The rule this file exists to enforce
///
/// **Obligatory worship is never paywalled.** Knowing when to pray, being
/// called to prayer, facing the qibla, knowing when to stop eating before
/// Subuh, and reading the Quran with a translation are free forever — not
/// "free during the trial", not "free with limits". A user whose subscription
/// lapsed in the middle of Ramadan must still be able to pray and read.
///
/// This is also the commercially stronger position. Those free features are the
/// acquisition engine: they are what most people use, what gets recommended,
/// and what generates the reviews and referrals a paid app cannot buy. Our
/// largest competitor gives them away too — charging for them while it does not
/// would be the weakest possible place to compete.
///
/// What we charge for is genuine convenience and depth on top of that: widgets,
/// insight, extra scholarship, planning tools. Things a committed user wants,
/// and nobody *needs* in order to worship.
enum SamawiFeature {
  // ── Free forever ──────────────────────────────────────────────────────
  /// Prayer times to the Kemenag/MABIMS standard.
  prayerTimes(FeatureTier.free),

  /// The call to prayer, including background scheduling.
  adzan(FeatureTier.free),

  /// Direction of the Kaaba.
  qibla(FeatureTier.free),

  /// Imsak, sahur, and berbuka times during Ramadan.
  ///
  /// Explicitly free: fasting is obligatory, and a paywall between someone and
  /// knowing when to stop eating would be indefensible.
  ramadanTimes(FeatureTier.free),

  /// Arabic Quran text, offline.
  quranText(FeatureTier.free),

  /// One Indonesian translation, offline.
  primaryTranslation(FeatureTier.free),

  /// Marking prayers as performed, and this week's summary.
  basicTracker(FeatureTier.free),

  /// Choosing a city.
  locationPicker(FeatureTier.free),

  /// The zakat calculator — a tool for fulfilling an obligation.
  zakatCalculator(FeatureTier.free),

  /// Everyday dzikir — pagi, petang, and around sleep — with Indonesian
  /// meaning, offline.
  ///
  /// Free on the same reasoning as [primaryTranslation]: the adhkar people
  /// actually reach for daily are the acquisition engine, and a library nobody
  /// can open converts nobody. Depth is what [fullDuaLibrary] charges for.
  dailyDhikr(FeatureTier.free),

  /// Earning referral credit for inviting friends.
  ///
  /// **Free, and it used to be listed as paid.** That was wrong twice over: the
  /// screen was already built and already ungated, so it was neither planned
  /// nor gated — and gating it would have been self-defeating anyway. The best
  /// referrers are enthusiastic free users, and the credit earned is *toward*
  /// a subscription. Charging for the right to bring us customers would close
  /// the funnel it exists to open.
  referralRewards(FeatureTier.free),

  /// Tasbih/dzikir counter with saved sets.
  ///
  /// Free, deliberately, and it used to be paid. A tasbih is a bead counter —
  /// users know it is trivial, so charging for it reads as greed rather than
  /// value, and it could never carry a subscription on its own. It is also
  /// dhikr, which §5.3's "never paywall the ability to pray" covers in spirit.
  /// What it *does* earn is daily opens, which is where the paid features are.
  dzikirCounter(FeatureTier.free),

  // ── Subscription ──────────────────────────────────────────────────────
  /// The complete dua collection beyond the everyday set — meals, home,
  /// travel, illness, and the rest of the Hisnul Muslim arrangement.
  fullDuaLibrary(FeatureTier.paid),

  /// Home-screen widgets. Consistently among the most-valued paid features in
  /// this category.
  homeScreenWidgets(FeatureTier.paid),

  /// Long-run history, patterns, and per-prayer breakdowns.
  advancedTracker(FeatureTier.paid),

  /// Additional translations beyond the bundled one.
  extraTranslations(FeatureTier.paid),

  /// Tafsir Ringkas and Tafsir Tahlili.
  tafsir(FeatureTier.paid),

  /// Alternative adzan recitations.
  ///
  /// **Stays as roadmap, unlike the two features deleted above**, and the
  /// difference is worth keeping straight: this one is blocked on *content*,
  /// not on the platform or on being a duplicate. The code is trivial — the
  /// notifier does not care how many audio files exist — and the moment a
  /// second cleared recording lands, this is an asset drop plus a picker.
  ///
  /// v1 ships one voice, decided 2026-07-23. Every openly-licensed source was
  /// checked and the pool is empty: Wikimedia Commons holds four adhan files
  /// (ours, one whose "public domain" tag contradicts its own YouTube credit,
  /// and two mosque field recordings), Pixabay holds two, both ambience or
  /// musical, and the Freesound CC0 candidate was a YouTube rip. A commission
  /// is the only route — see `docs/letters/muezzin_commission_brief.md`.
  customAdzanVoices(FeatureTier.paid, status: FeatureStatus.planned),

  /// Plan and track a full Quran reading over a chosen period.
  khatamPlanner(FeatureTier.paid),

  /// Sunnah fasts — Senin–Kamis, Ayyamul Bidh — with reminders.
  sunnahFastingTracker(FeatureTier.paid),

  /// Saved locations, for people who travel between time zones.
  multiLocation(FeatureTier.paid),

  /// Friday-specific alerts and a khutbah silence prompt.
  ///
  /// A weekly ritual almost every user observes and almost no app handles
  /// well.
  jumatMode(FeatureTier.paid),

  /// Musafir mode: detects travel beyond the qasar threshold (~88 km) and
  /// offers shortened-prayer guidance.
  ///
  /// Genuinely hard to get right — which is exactly why it is worth paying
  /// for. Must never *assert* that qasar applies; the ruling depends on
  /// intention and duration of stay, which the app cannot know.
  travelMode(FeatureTier.paid),

  /// Per-location adzan profiles — different settings at home and at work.
  adzanProfiles(FeatureTier.paid),

  /// Find the nearest masjid and mushola, offline.
  ///
  /// Searches a bundled OpenStreetMap directory on-device, so the user's
  /// location never leaves the phone — the privacy promise the whole app rests
  /// on. Paid: it is genuine convenience, not something anyone needs in order
  /// to worship. See MosqueDirectory for the honest coverage caveat.
  nearestMosque(FeatureTier.paid),

  /// Per-prayer offset to match the user's own mosque.
  ///
  /// Many Indonesian mosques call a few minutes off the calculated time.
  /// Letting someone record "my mosque adds 5 minutes to Subuh" acknowledges
  /// that people pray with a local jamaah, not with an algorithm. The
  /// underlying Kemenag times remain unchanged and free.
  mosqueAdjustment(FeatureTier.paid),

  /// Export and restore tracker, qadha, and settings data.
  ///
  /// A local file the user controls. Nothing is uploaded — backup must not
  /// become a reason to build an account system for sensitive religious data.
  backupRestore(FeatureTier.paid),

  /// Paket Ramadan: the imsakiyah as a shareable image, a sahur alarm, and
  /// the fasting log with its qadha plan.
  ///
  /// The imsakiyah itself is free — it is prayer times (see [ramadanTimes]).
  /// What is sold is the month's conveniences around it, in the one month a
  /// year when people go looking for an app.
  ramadanPack(FeatureTier.paid),

  /// Offline murottal. Not in v1 — blocked on licensing, see DATA_SOURCES.md.
  offlineMurottal(FeatureTier.paid, status: FeatureStatus.blocked);

  const SamawiFeature(this.tier, {this.status = FeatureStatus.shipped});

  final FeatureTier tier;

  /// Whether this exists yet. See [FeatureStatus].
  final FeatureStatus status;

  /// Whether it may appear on the paywall.
  bool get isAdvertisable => status == FeatureStatus.shipped;

  bool get isFree => tier == FeatureTier.free;

  static List<SamawiFeature> get freeFeatures =>
      values.where((f) => f.isFree).toList();

  static List<SamawiFeature> get paidFeatures =>
      values.where((f) => !f.isFree).toList();

  /// Paid features that exist today, in declaration order.
  ///
  /// The only list the paywall may show. Selling something that does not exist
  /// is a worse breach of §5.4 than any wording, because the user finds out
  /// after paying.
  static List<SamawiFeature> get advertisableFeatures =>
      values.where((f) => !f.isFree && f.isAdvertisable).toList();
}

/// The user's current standing.
enum EntitlementStatus {
  /// Never subscribed, trial not started or already over.
  free,

  /// Inside the free trial — full access.
  trialing,

  /// Paying subscriber.
  subscribed,

  /// Was subscribed; the subscription has lapsed.
  ///
  /// Distinct from [free] so the UI can say something accurate, and so a lapsed
  /// subscriber is never mistaken for someone who can start a fresh trial.
  lapsed,
}

/// Decides whether a feature is available.
///
/// Deliberately a pure function of ([feature], [status]) so the rule that free
/// features are never gated is testable without a store, a network, or a
/// subscription.
///
/// **This is not the security boundary.** A determined user can patch a client
/// check. Anything that costs real money — referral credit in particular —
/// must be enforced server-side against RevenueCat, per PRD §6. This exists to
/// make the *product* behave correctly, not to stop piracy.
/// What the paid tier is called, in Indonesian.
///
/// Deliberately **not** "Muslim Taat" or "Muslim Soleh". Those name the *person*
/// rather than the product, which implies a user who does not pay is less
/// devout — the same riya' problem the share cards avoid, and the kind of line
/// that gets screenshotted. "Lengkap" (complete) describes what you get and
/// grades nobody's piety.
const String kPaidTierName = 'Samawi Lengkap';

abstract final class FeatureGate {
  /// Length of the free trial.
  ///
  /// Two weeks, and the number is decided by the paid features rather than by
  /// convention. Two of them cannot demonstrate themselves in seven days:
  ///
  /// * the weekly trend in Catatan mendalam only draws once the user has
  ///   recorded prayers in **two Monday-based weeks**, so a trial beginning on
  ///   a Monday would produce one bucket and the chart would never appear —
  ///   someone could pay for a feature they were never shown;
  /// * [PrayerInsights.minimumDaysForInsight] needs five recorded days before
  ///   the least-recorded observation says anything at all;
  /// * Mode Jumat needs two Fridays to read as a feature rather than a one-off.
  ///
  /// Fourteen days clears all three whatever day someone starts on. A month
  /// would be worse, not better: the free tier is genuinely livable, so a month
  /// is long enough to form the habit on it and feel no loss when the trial
  /// ends.
  ///
  /// **The store is the authority.** This value drives the in-app countdown and
  /// the copy; the actual billing trial is configured in App Store Connect and
  /// Play Console, and Apple only offers preset durations — two weeks is one of
  /// them, ten days is not. If the two ever disagree, the store wins and this
  /// constant is the bug.
  static const Duration trialLength = Duration(days: 14);

  static bool isAvailable(SamawiFeature feature, EntitlementStatus status) {
    // Checked first and unconditionally: no entitlement state, including
    // [EntitlementStatus.lapsed], can ever gate a free feature.
    if (feature.isFree) return true;

    return switch (status) {
      EntitlementStatus.trialing || EntitlementStatus.subscribed => true,
      EntitlementStatus.free || EntitlementStatus.lapsed => false,
    };
  }

  /// Days left in a trial that started at [startedAt], as of [now].
  ///
  /// Rounds **up**, so a user with 12 hours left is told "1 day", not "0 days"
  /// while the trial is still running. Zero once expired — never negative, so
  /// the UI cannot show "-3 days left".
  static int trialDaysRemaining({
    required DateTime startedAt,
    required DateTime now,
  }) {
    final remaining = trialLength - now.difference(startedAt);
    if (remaining.isNegative || remaining == Duration.zero) return 0;
    return (remaining.inMinutes / Duration.minutesPerDay).ceil();
  }

  static bool isTrialActive({
    required DateTime startedAt,
    required DateTime now,
  }) => now.difference(startedAt) < trialLength;
}
