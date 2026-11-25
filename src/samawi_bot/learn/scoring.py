"""Post scoring and Thompson-sampling re-weighting (spec §9).

score = 3·share_rate + 2·save_rate + 2·follows_per_1k_reach + 1·comment_rate + 0.5·profile_visit_rate

The learner tunes *what* and *when* only. `ALLOWED_DIMENSIONS` is the whole of what it may write;
anything else (blocked topics, accuracy rules, CTA rules) is rejected by `validate_changes`.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

ALLOWED_DIMENSIONS = frozenset({"pillar", "hook_formula", "format", "slot", "bridge_feature"})
EXPLORATION = 0.20
DRAWS = 4000


@dataclass(frozen=True)
class Observation:
    dimension: str
    arm: str
    score: float


def score(reach: int, shares: int, saves: int, follows: int, comments: int, profile_visits: int) -> float:
    """The spec §9 formula, literally. 0 for a post with no reach (no division by zero)."""
    if reach <= 0:
        return 0.0
    return 3 * (shares / reach) + 2 * (saves / reach) + 2 * (follows / reach * 1000) + 1 * (comments / reach) + 0.5 * (profile_visits / reach)


def thompson_weights(arms: Iterable[str], observations: list[Observation], rng: random.Random) -> dict[str, float]:
    """P(arm is best) under Beta posteriors over normalised scores, mixed with 20% uniform exploration.

    Every arm keeps at least EXPLORATION / n weight, so no arm ever goes to zero.
    """
    arms = sorted(set(arms))
    if not arms:
        return {}
    top = max((o.score for o in observations), default=0.0)
    alpha: dict[str, float] = defaultdict(lambda: 1.0)
    beta: dict[str, float] = defaultdict(lambda: 1.0)
    for o in observations:
        if o.arm not in arms:
            continue
        s = o.score / top if top > 0 else 0.0
        alpha[o.arm] += s
        beta[o.arm] += 1 - s
    wins = dict.fromkeys(arms, 0)
    for _ in range(DRAWS):
        best = max(arms, key=lambda a: rng.betavariate(alpha[a], beta[a]))
        wins[best] += 1
    n = len(arms)
    return {a: (1 - EXPLORATION) * wins[a] / DRAWS + EXPLORATION / n for a in arms}


class LearnerViolation(ValueError):
    pass


def validate_changes(changes: dict[str, dict[str, float]]) -> None:
    for dim, weights in changes.items():
        if dim not in ALLOWED_DIMENSIONS:
            raise LearnerViolation(f"the learner may not change {dim!r}")
        if any(w <= 0 for w in weights.values()):
            raise LearnerViolation(f"{dim}: weights must stay above zero")


def deciles(values: list[tuple[str, float]]) -> tuple[list[str], list[str]]:
    """(top-decile ids, bottom-decile ids); at least one each when there are ≥ 2 values."""
    if len(values) < 2:
        return [], []
    ordered = sorted(values, key=lambda v: v[1], reverse=True)
    k = max(1, len(ordered) // 10)
    return [v[0] for v in ordered[:k]], [v[0] for v in ordered[-k:]]
