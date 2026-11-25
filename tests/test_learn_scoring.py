"""Post score (spec §9), Thompson weights, learner guard rails, deciles."""

from __future__ import annotations

import random

import pytest

from samawi_bot.learn.scoring import (
    ALLOWED_DIMENSIONS,
    DRAWS,
    EXPLORATION,
    LearnerViolation,
    Observation,
    deciles,
    score,
    thompson_weights,
    validate_changes,
)


def test_score_formula_is_the_spec_literally() -> None:
    reach, shares, saves, follows, comments, visits = 2000, 40, 60, 10, 20, 100
    expected = 3 * (40 / 2000) + 2 * (60 / 2000) + 2 * (10 / 2000 * 1000) + 1 * (20 / 2000) + 0.5 * (100 / 2000)
    assert score(reach, shares, saves, follows, comments, visits) == pytest.approx(expected)
    assert expected == pytest.approx(0.06 + 0.06 + 10 + 0.01 + 0.025)


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((1000, 1, 0, 0, 0, 0), 3 / 1000),
        ((1000, 0, 1, 0, 0, 0), 2 / 1000),
        ((1000, 0, 0, 1, 0, 0), 2.0),  # follows are per 1k reach
        ((1000, 0, 0, 0, 1, 0), 1 / 1000),
        ((1000, 0, 0, 0, 0, 1), 0.5 / 1000),
        ((1000, 0, 0, 0, 0, 0), 0.0),
    ],
)
def test_each_term_weight(args: tuple[int, ...], expected: float) -> None:
    assert score(*args) == pytest.approx(expected)


@pytest.mark.parametrize("reach", [0, -5])
def test_zero_reach_scores_zero_without_division_error(reach: int) -> None:
    assert score(reach, 10, 10, 10, 10, 10) == 0.0


def test_cold_start_is_uniform_ish_and_sums_to_one() -> None:
    arms = ["a", "b", "c", "d"]
    w = thompson_weights(arms, [], random.Random(1))
    assert set(w) == set(arms)
    assert sum(w.values()) == pytest.approx(1.0)
    for v in w.values():
        assert v == pytest.approx(0.25, abs=0.05)
        assert v >= EXPLORATION / len(arms)


def test_one_post_per_arm() -> None:
    obs = [Observation("pillar", "a", 2.0), Observation("pillar", "b", 1.0), Observation("pillar", "c", 0.0)]
    w = thompson_weights(["a", "b", "c"], obs, random.Random(7))
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["a"] > w["b"] > w["c"]
    assert min(w.values()) >= EXPLORATION / 3


def test_weights_never_go_to_zero() -> None:
    obs = [Observation("x", "winner", 10.0)] * 200 + [Observation("x", "loser", 0.0)] * 200
    w = thompson_weights(["winner", "loser", "untried"], obs, random.Random(3))
    floor = EXPLORATION / 3
    assert all(v >= floor - 1e-12 for v in w.values())
    assert w["loser"] == pytest.approx(floor, abs=0.01)
    assert w["winner"] > 0.5
    assert sum(w.values()) == pytest.approx(1.0)


def test_all_zero_scores_and_unknown_arms() -> None:
    obs = [Observation("x", "a", 0.0), Observation("x", "ghost", 5.0)]
    w = thompson_weights(["a", "b", "a"], obs, random.Random(0))
    assert set(w) == {"a", "b"}
    assert sum(w.values()) == pytest.approx(1.0)
    # top score 5.0 is from an arm outside the list; "a" scored 0 → treated as a loss
    assert w["b"] > w["a"]


def test_zero_top_score_counts_as_losses() -> None:
    w = thompson_weights(["a", "b"], [Observation("x", "a", 0.0)] * 50, random.Random(0))
    assert w["b"] > w["a"]


def test_no_arms() -> None:
    assert thompson_weights([], [Observation("x", "a", 1.0)], random.Random(0)) == {}


def test_deterministic_with_seeded_rng() -> None:
    obs = [Observation("x", "a", 1.0), Observation("x", "b", 0.5)]
    assert thompson_weights(["a", "b"], obs, random.Random(5)) == thompson_weights(["a", "b"], obs, random.Random(5))
    assert DRAWS >= 1000


def test_validate_changes_accepts_allowed_dimensions() -> None:
    validate_changes({d: {"k": 0.5} for d in ALLOWED_DIMENSIONS})
    validate_changes({})
    assert frozenset({"pillar", "hook_formula", "format", "slot", "bridge_feature"}) == ALLOWED_DIMENSIONS


@pytest.mark.parametrize("dimension", ["blocked_topics", "accuracy", "cta_rules", "banned_hook", "too_easy", "Pillar"])
def test_learner_may_not_touch_other_dimensions(dimension: str) -> None:
    """A learner change attempting to unblock a topic → rejected (spec §10.3)."""
    with pytest.raises(LearnerViolation, match="may not change"):
        validate_changes({"pillar": {"a": 1.0}, dimension: {"politik": 1.0}})


@pytest.mark.parametrize("weight", [0.0, -0.1])
def test_learner_may_not_zero_a_weight(weight: float) -> None:
    with pytest.raises(LearnerViolation, match="above zero"):
        validate_changes({"pillar": {"a": 0.5, "b": weight}})


def test_deciles() -> None:
    assert deciles([]) == ([], [])
    assert deciles([("a", 1.0)]) == ([], [])
    assert deciles([("a", 1.0), ("b", 3.0)]) == (["b"], ["a"])
    values = [(str(i), float(i)) for i in range(25)]
    top, bottom = deciles(values)
    assert top == ["24", "23"]
    assert bottom == ["1", "0"]
