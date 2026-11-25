"""`samawi_features.dart` reader (spec §5, §10.3): fail closed, bridge only to shipped features."""

from __future__ import annotations

import re

import pytest

from conftest import SNAPSHOT
from samawi_bot.sources import Sources
from samawi_bot.sources.features_reader import (
    DISPLAY_NAMES,
    Feature,
    FeatureCatalog,
    FeaturesParseError,
    Status,
    Tier,
    parse_features,
)
from samawi_bot.sources.snapshot import FEATURES, Snapshot


@pytest.fixture(scope="module")
def real_source() -> str:
    return Snapshot(SNAPSHOT).read_text(FEATURES)


@pytest.fixture(scope="module")
def catalog(sources: Sources) -> FeatureCatalog:
    return sources.features


def enum(body: str) -> str:
    return (
        "enum FeatureTier { free, paid }\n\nenum SamawiFeature {\n"
        + body
        + "\n;\n  const SamawiFeature(this.tier, {this.status = FeatureStatus.shipped});\n}\n"
    )


# ── The real file ────────────────────────────────────────────────────────────────────────────


def test_real_file(catalog: FeatureCatalog, real_source: str) -> None:
    f = catalog.features
    # Independent count of `name(FeatureTier.…` lines in the enum.
    assert len(f) == len(re.findall(r"^\s+[a-z][A-Za-z0-9]*\(FeatureTier\.", real_source, re.M)) == 29
    assert f["customAdzanVoices"] == Feature("customAdzanVoices", Tier.PAID, Status.PLANNED)
    assert f["offlineMurottal"] == Feature("offlineMurottal", Tier.PAID, Status.BLOCKED)
    assert f["prayerTimes"] == Feature("prayerTimes", Tier.FREE, Status.SHIPPED)
    assert f["fullDuaLibrary"].paid and f["fullDuaLibrary"].shipped
    assert not f["prayerTimes"].paid
    assert not f["customAdzanVoices"].shipped
    # Comments in the file mention removed features; they must not be parsed as entries.
    assert "silentModeAutomation" not in f and "ramadanPlanner" not in f
    assert "silentModeAutomation" in real_source
    assert FeatureCatalog.load(Snapshot(SNAPSHOT)).features == f


def test_real_check_bridge(catalog: FeatureCatalog) -> None:
    assert catalog.check_bridge("prayerTimes") is None
    assert catalog.check_bridge("customAdzanVoices") == "bridge feature 'customAdzanVoices' has status planned, not shipped"
    assert catalog.check_bridge("offlineMurottal") == "bridge feature 'offlineMurottal' has status blocked, not shipped"
    assert catalog.check_bridge("silentModeAutomation") == "bridge feature 'silentModeAutomation' is not in samawi_features.dart"
    assert catalog.check_bridge("locationPicker") == "bridge feature 'locationPicker' has no public name yet"


def test_bridgeable_is_shipped_and_named(catalog: FeatureCatalog) -> None:
    keys = {f.key for f in catalog.bridgeable()}
    assert keys == {k for k, f in catalog.features.items() if f.shipped and k in DISPLAY_NAMES}
    assert "prayerTimes" in keys and "customAdzanVoices" not in keys and "offlineMurottal" not in keys and "locationPicker" not in keys
    for key in keys:
        assert catalog.check_bridge(key) is None
        assert FeatureCatalog.display_name(key) == DISPLAY_NAMES[key]


def test_display_name_unknown_raises() -> None:
    with pytest.raises(KeyError):
        FeatureCatalog.display_name("locationPicker")


def test_renamed_feature_blocks_bridge(real_source: str) -> None:
    renamed = FeatureCatalog(parse_features(real_source.replace("  adzan(FeatureTier.free),", "  adzanCall(FeatureTier.free),")))
    assert "adzan" not in renamed.features and "adzanCall" in renamed.features
    assert renamed.check_bridge("adzan") == "bridge feature 'adzan' is not in samawi_features.dart"
    assert renamed.check_bridge("adzanCall") == "bridge feature 'adzanCall' has no public name yet"


def test_deleted_feature_blocks_bridge(real_source: str) -> None:
    deleted = FeatureCatalog(parse_features(real_source.replace("  qibla(FeatureTier.free),", "")))
    assert "qibla" not in deleted.features
    assert deleted.check_bridge("qibla") is not None
    assert "qibla" not in {f.key for f in deleted.bridgeable()}


def test_status_flip_blocks_bridge(real_source: str) -> None:
    src = real_source.replace("  khatamPlanner(FeatureTier.paid),", "  khatamPlanner(FeatureTier.paid, status: FeatureStatus.blocked),")
    assert FeatureCatalog(parse_features(src)).check_bridge("khatamPlanner") == "bridge feature 'khatamPlanner' has status blocked, not shipped"


# ── Syntax the parser has not seen ───────────────────────────────────────────────────────────


def test_comments_are_ignored() -> None:
    src = enum(
        "  // gone(FeatureTier.paid),\n"
        "  /* also(FeatureTier.paid),\n     gone(FeatureTier.free), */\n"
        "  /// Doc comment with (parens), commas, and FeatureStatus.planned.\n"
        "  a(FeatureTier.free), // was status: FeatureStatus.planned\n"
        "  b(FeatureTier.paid /* , status: FeatureStatus.blocked */),\n"
    )
    f = parse_features(src)
    assert set(f) == {"a", "b"}
    assert f["a"].status is Status.SHIPPED and f["b"].status is Status.SHIPPED


def test_trailing_commas_and_multiline_entries() -> None:
    src = enum("  a(\n    FeatureTier.free,\n  ),\n  b(FeatureTier.paid, status: FeatureStatus.planned,),\n")
    f = parse_features(src)
    assert f["a"] == Feature("a", Tier.FREE, Status.SHIPPED)
    assert f["b"] == Feature("b", Tier.PAID, Status.PLANNED)


def test_unknown_enum_arguments_are_tolerated() -> None:
    src = enum("  a(FeatureTier.paid, since: 3, label: 'Widget'),\n  b(FeatureTier.free, beta: true, status: FeatureStatus.shipped),")
    f = parse_features(src)
    assert f["a"] == Feature("a", Tier.PAID, Status.SHIPPED)
    assert f["b"] == Feature("b", Tier.FREE, Status.SHIPPED)


def test_new_feature_status_is_unknown_not_shipped() -> None:
    f = parse_features(enum("  a(FeatureTier.paid, status: FeatureStatus.beta),"))
    assert f["a"].status is Status.UNKNOWN
    assert not f["a"].shipped
    cat = FeatureCatalog({**f, "prayerTimes": Feature("prayerTimes", Tier.FREE, Status.UNKNOWN)})
    assert cat.check_bridge("prayerTimes") == "bridge feature 'prayerTimes' has status unknown, not shipped"
    assert cat.bridgeable() == []


@pytest.mark.parametrize("arg", ["status: kPlanned", "status: SomeOther.planned", "status:FeatureStatus .planned"])
def test_unreadable_status_expression_is_unknown(arg: str) -> None:
    f = parse_features(enum(f"  a(FeatureTier.paid, {arg}),"))
    assert f["a"].status is Status.UNKNOWN


def test_missing_enum() -> None:
    with pytest.raises(FeaturesParseError, match="enum SamawiFeature not found"):
        parse_features("enum OtherFeature { a(FeatureTier.free); }")
    with pytest.raises(FeaturesParseError, match="not found"):
        parse_features("// enum SamawiFeature { a(FeatureTier.free); }")
    with pytest.raises(FeaturesParseError, match="not found"):
        parse_features("")


@pytest.mark.parametrize(
    "entry",
    [
        "  a",  # no arguments
        "  A(FeatureTier.free)",  # not lowerCamel
        "  a(FeatureTier.free, since: Version(1, 2))",  # nested call
        "  a(FeatureTier.free) extra",
        "  1a(FeatureTier.free)",
    ],
)
def test_unrecognised_entry(entry: str) -> None:
    with pytest.raises(FeaturesParseError, match="Unrecognised feature entry"):
        parse_features(enum(entry + ","))


@pytest.mark.parametrize("args", ["", "status: FeatureStatus.shipped", "FeatureTier.premium", "Tier.free", "featureTier: free"])
def test_missing_or_unknown_tier(args: str) -> None:
    with pytest.raises(FeaturesParseError, match="a has no recognised FeatureTier"):
        parse_features(enum(f"  a({args}),"))


@pytest.mark.parametrize("body", ["", "  // everything removed", "  ,", "  , ,"])
def test_empty_enum(body: str) -> None:
    with pytest.raises(FeaturesParseError, match="no entries"):
        parse_features(enum(body))


def test_check_bridge_variants() -> None:
    cat = FeatureCatalog(
        {
            "prayerTimes": Feature("prayerTimes", Tier.FREE, Status.SHIPPED),
            "tafsir": Feature("tafsir", Tier.PAID, Status.BLOCKED),
            "khatamPlanner": Feature("khatamPlanner", Tier.PAID, Status.PLANNED),
            "brandNew": Feature("brandNew", Tier.PAID, Status.SHIPPED),
        }
    )
    assert cat.check_bridge("prayerTimes") is None
    assert "status blocked" in (cat.check_bridge("tafsir") or "")
    assert "status planned" in (cat.check_bridge("khatamPlanner") or "")
    assert "no public name" in (cat.check_bridge("brandNew") or "")
    assert "not in samawi_features.dart" in (cat.check_bridge("qibla") or "")
    assert [f.key for f in cat.bridgeable()] == ["prayerTimes"]
