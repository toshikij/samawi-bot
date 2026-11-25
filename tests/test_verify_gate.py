"""verify/gate.py: the judge report turned into publish / no-publish. Fails closed."""

from __future__ import annotations

from typing import Any

import pytest

from conftest import judge_report, load_draft
from samawi_bot.brain.models import Draft, HookScore
from samawi_bot.verify import gate as G
from samawi_bot.verify.gate import GateResult, decide, validate_report


@pytest.fixture
def d() -> Draft:
    return load_draft()


def hooks(*scores: tuple[int, int, int, int]) -> list[dict[str, Any]]:
    return [{"index": i, "curiosity_gap": a, "specificity": b, "payoff_honesty": c, "thumb_stop": t} for i, (a, b, c, t) in enumerate(scores)]


ALL9 = (9, 9, 9, 9)


def test_thresholds() -> None:
    assert (G.HOOK_MIN, G.THUMB_STOP_MIN, G.NOVELTY_MIN, G.ACCESSIBILITY_MIN) == (8, 7, 60, 8)


def test_good_report_passes(d: Draft) -> None:
    # conftest scores hook 0 with thumb_stop 9 and hook 1 with 8; hook 0 ranks highest.
    assert decide(d, judge_report(d)) == GateResult(True, 0, [])


def test_gate_result_defaults() -> None:
    assert GateResult(False, None).reasons == []
    assert GateResult(False, None).reasons is not GateResult(False, None).reasons


# ── malformed reports fail closed ─────────────────────────────────────────────


def test_missing_claim_index(d: Draft) -> None:
    r = judge_report(d, claims=[{"index": 0, "verdict": "supported"}])
    assert validate_report(d, r) == ["judge covered claims [0], expected 0..1"]
    assert decide(d, r) == GateResult(False, None, ["malformed judge report: judge covered claims [0], expected 0..1"])


def test_duplicate_claim_index(d: Draft) -> None:
    r = judge_report(d, claims=[{"index": 0, "verdict": "supported"}, {"index": 0, "verdict": "supported"}])
    assert validate_report(d, r) == ["judge covered claims [0, 0], expected 0..1"]


def test_extra_claim_index(d: Draft) -> None:
    claims = [{"index": i, "verdict": "supported"} for i in (0, 1, 2)]
    assert validate_report(d, judge_report(d, claims=claims)) == ["judge covered claims [0, 1, 2], expected 0..1"]


def test_claims_out_of_order_is_fine(d: Draft) -> None:
    r = judge_report(d, claims=[{"index": 1, "verdict": "supported"}, {"index": 0, "verdict": "supported"}])
    assert validate_report(d, r) == []
    assert decide(d, r).passed is True


def test_missing_hook(d: Draft) -> None:
    r = judge_report(d, hooks=hooks(ALL9, ALL9, ALL9, ALL9))
    assert validate_report(d, r) == ["judge scored hooks [0, 1, 2, 3], expected 0..4"]
    assert decide(d, r).passed is False


def test_duplicate_hook(d: Draft) -> None:
    h = hooks(ALL9, ALL9, ALL9, ALL9, ALL9)
    h[4]["index"] = 3
    assert validate_report(d, judge_report(d, hooks=h)) == ["judge scored hooks [0, 1, 2, 3, 3], expected 0..4"]


@pytest.mark.parametrize("pos", range(4))
@pytest.mark.parametrize("bad", [0, 11, -1])
def test_hook_score_out_of_range(d: Draft, pos: int, bad: int) -> None:
    score = [9, 9, 9, 9]
    score[pos] = bad
    h = hooks(ALL9, ALL9, tuple(score), ALL9, ALL9)  # type: ignore[arg-type]
    r = judge_report(d, hooks=h)
    assert validate_report(d, r) == ["hook 2 has a score outside 1–10"]
    assert decide(d, r) == GateResult(False, None, ["malformed judge report: hook 2 has a score outside 1–10"])


@pytest.mark.parametrize("edge", [1, 10])
def test_hook_score_edges_valid(d: Draft, edge: int) -> None:
    h = hooks((edge, edge, edge, edge), ALL9, ALL9, ALL9, ALL9)
    assert validate_report(d, judge_report(d, hooks=h)) == []


@pytest.mark.parametrize(("novelty", "ok"), [(-1, False), (0, True), (100, True), (101, False)])
def test_novelty_range(d: Draft, novelty: int, ok: bool) -> None:
    r = judge_report(d, novelty_pct=novelty)
    assert validate_report(d, r) == ([] if ok else [f"novelty {novelty} outside 0–100"])


@pytest.mark.parametrize(("acc", "ok"), [(-1, False), (0, True), (10, True), (11, False)])
def test_accessibility_range(d: Draft, acc: int, ok: bool) -> None:
    r = judge_report(d, accessibility=acc)
    assert validate_report(d, r) == ([] if ok else [f"accessibility {acc} outside 0–10"])


def test_several_malformations_all_reported(d: Draft) -> None:
    r = judge_report(d, claims=[], hooks=hooks((0, 9, 9, 9)), novelty_pct=101, accessibility=11)
    assert decide(d, r) == GateResult(
        False,
        None,
        [
            "malformed judge report: judge covered claims [], expected 0..1",
            "malformed judge report: judge scored hooks [0], expected 0..4",
            "malformed judge report: hook 0 has a score outside 1–10",
            "malformed judge report: novelty 101 outside 0–100",
            "malformed judge report: accessibility 11 outside 0–10",
        ],
    )


def test_malformed_short_circuits_other_reasons(d: Draft) -> None:
    r = judge_report(d, novelty_pct=101, tone_ok=False, contested_framing=True)
    assert decide(d, r).reasons == ["malformed judge report: novelty 101 outside 0–100"]


# ── verdicts ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("verdict", ["unsupported", "overstated"])
def test_any_bad_claim_fails(d: Draft, verdict: str) -> None:
    claims = [{"index": 0, "verdict": "supported"}, {"index": 1, "verdict": verdict, "note": "tidak ada di ayat"}]
    res = decide(d, judge_report(d, claims=claims))
    assert res == GateResult(False, None, [f"claim 1 {verdict}: 'Ayat ini memerintahkan utang yang bertempo untuk ditulis' tidak ada di ayat"])


def test_bad_claim_text_truncated_and_note_stripped() -> None:
    long = "x" * 100
    d = load_draft(claims=[{"text": long, "source": {"type": "fact", "ref": "quran:totals"}}])
    claims = [{"index": 0, "verdict": "unsupported", "note": ""}]
    res = decide(d, judge_report(d, claims=claims))
    assert res.reasons == [f"claim 0 unsupported: {'x' * 80!r}"]


def test_contested_framing_fails(d: Draft) -> None:
    res = decide(d, judge_report(d, contested_framing=True, contested_note="khilafiyah"))
    assert res == GateResult(False, None, ["contested framing: khilafiyah"])


@pytest.mark.parametrize(("novelty", "ok"), [(59, False), (60, True), (61, True)])
def test_novelty_threshold(d: Draft, novelty: int, ok: bool) -> None:
    res = decide(d, judge_report(d, novelty_pct=novelty))
    assert res == (GateResult(True, 0, []) if ok else GateResult(False, None, [f"novelty {novelty}% < 60% (too easy)"]))


@pytest.mark.parametrize(("acc", "ok"), [(7, False), (8, True), (9, True)])
def test_accessibility_threshold(d: Draft, acc: int, ok: bool) -> None:
    res = decide(d, judge_report(d, accessibility=acc))
    assert res == (GateResult(True, 0, []) if ok else GateResult(False, None, [f"accessibility {acc}/10 < 8"]))


def test_payoff_by_slide_3(d: Draft) -> None:
    assert decide(d, judge_report(d, payoff_by_slide_3=False)) == GateResult(False, None, ["hook is not paid off by slide 3"])


def test_tone(d: Draft) -> None:
    assert decide(d, judge_report(d, tone_ok=False, notes="menggurui")) == GateResult(False, None, ["tone: menggurui"])


def test_all_failures_listed_in_order(d: Draft) -> None:
    r = judge_report(
        d,
        claims=[{"index": 0, "verdict": "overstated"}, {"index": 1, "verdict": "supported"}],
        contested_framing=True,
        contested_note="n",
        novelty_pct=10,
        accessibility=2,
        payoff_by_slide_3=False,
        tone_ok=False,
        notes="t",
        hooks=hooks(*[(7, 9, 9, 9)] * 5),
    )
    assert decide(d, r).reasons == [
        'claim 0 overstated: "Ayat terpanjang di Al-Qur\'an adalah Al-Baqarah ayat 282"',
        "contested framing: n",
        "novelty 10% < 60% (too easy)",
        "accessibility 2/10 < 8",
        "hook is not paid off by slide 3",
        "tone: t",
        "no hook scores ≥ 8 on curiosity, specificity and payoff honesty (and ≥ 7 thumb-stop)",
    ]


# ── hooks ─────────────────────────────────────────────────────────────────────


# Spec §3.2: curiosity gap, specificity and payoff honesty need ≥ 8; thumb-stop has its own floor of 7.
@pytest.mark.parametrize(("pos", "score"), [(0, 7), (1, 7), (2, 7), (3, 6)])
def test_a_score_below_its_floor_fails_that_hook(d: Draft, pos: int, score: int) -> None:
    weak = [10, 10, 10, 10]
    weak[pos] = score
    h = hooks(tuple(weak), (8, 8, 8, 8), (8, 8, 8, 8), (8, 8, 8, 8), (8, 8, 8, 8))  # type: ignore[arg-type]
    # Hook 0 has the highest total but one score under its floor, so hook 1 (first all-8 hook) is chosen.
    assert decide(d, judge_report(d, hooks=h)) == GateResult(True, 1, [])


def test_thumb_stop_seven_passes(d: Draft) -> None:
    h = hooks((10, 10, 10, 7), (8, 8, 8, 8), (8, 8, 8, 8), (8, 8, 8, 8), (8, 8, 8, 8))
    assert decide(d, judge_report(d, hooks=h)) == GateResult(True, 0, [])
    only = hooks((8, 8, 8, 7), (8, 8, 8, 6), (7, 9, 9, 9), (9, 7, 9, 9), (9, 9, 7, 9))
    assert decide(d, judge_report(d, hooks=only)) == GateResult(True, 0, [])


@pytest.mark.parametrize(("pos", "score"), [(0, 7), (1, 7), (2, 7), (3, 6)])
def test_no_passing_hook_fails(d: Draft, pos: int, score: int) -> None:
    weak = [9, 9, 9, 9]
    weak[pos] = score
    h = hooks(*[tuple(weak)] * 5)  # type: ignore[arg-type]
    assert decide(d, judge_report(d, hooks=h)) == GateResult(
        False, None, ["no hook scores ≥ 8 on curiosity, specificity and payoff honesty (and ≥ 7 thumb-stop)"]
    )


def test_hook_ok_boundary() -> None:
    assert G._hook_ok(HookScore(index=0, curiosity_gap=8, specificity=8, payoff_honesty=8, thumb_stop=8)) is True
    assert G._hook_ok(HookScore(index=0, curiosity_gap=8, specificity=8, payoff_honesty=8, thumb_stop=7)) is True
    assert G._hook_ok(HookScore(index=0, curiosity_gap=8, specificity=8, payoff_honesty=8, thumb_stop=6)) is False
    assert G._hook_ok(HookScore(index=0, curiosity_gap=7, specificity=8, payoff_honesty=8, thumb_stop=10)) is False
    assert G._hook_ok(HookScore(index=0, curiosity_gap=8, specificity=7, payoff_honesty=8, thumb_stop=10)) is False
    assert G._hook_ok(HookScore(index=0, curiosity_gap=8, specificity=8, payoff_honesty=7, thumb_stop=10)) is False


def test_best_hook_by_total(d: Draft) -> None:
    h = hooks(ALL9, ALL9, (10, 10, 10, 9), ALL9, (9, 10, 9, 9))
    assert decide(d, judge_report(d, hooks=h)) == GateResult(True, 2, [])


@pytest.mark.parametrize("pos", range(4))
def test_every_score_counts_in_total(d: Draft, pos: int) -> None:
    better = [9, 9, 9, 9]
    better[pos] = 10
    h = hooks(ALL9, ALL9, ALL9, tuple(better), ALL9)  # type: ignore[arg-type]
    assert decide(d, judge_report(d, hooks=h)).hook_index == 3


def test_tie_on_total_broken_by_thumb_stop(d: Draft) -> None:
    h = hooks((10, 9, 9, 8), (9, 9, 9, 9), (8, 9, 9, 10), ALL9, ALL9)
    assert decide(d, judge_report(d, hooks=h)).hook_index == 2


def test_full_tie_picks_lowest_index(d: Draft) -> None:
    h = hooks((8, 8, 8, 8), ALL9, ALL9, ALL9, ALL9)
    assert decide(d, judge_report(d, hooks=h)).hook_index == 1
    h = [{**x, "index": 4 - x["index"]} for x in hooks(*[ALL9] * 5)]  # reported in reverse order
    assert decide(d, judge_report(d, hooks=h)).hook_index == 0


def test_hook_rank_tuple() -> None:
    h = HookScore(index=3, curiosity_gap=1, specificity=2, payoff_honesty=3, thumb_stop=4)
    assert G._hook_rank(h) == (10, 4, -3)


def test_hook_index_none_when_other_reasons(d: Draft) -> None:
    res = decide(d, judge_report(d, tone_ok=False))
    assert res.hook_index is None
    assert res.passed is False


def test_excluded_hooks_are_never_chosen() -> None:
    from conftest import judge_report, load_draft
    from samawi_bot.verify.gate import decide

    d = load_draft()
    r = judge_report(d)
    best = decide(d, r).hook_index
    assert best is not None
    other = decide(d, r, {best}).hook_index
    assert other is not None and other != best
    assert not decide(d, r, set(range(5))).passed
