"""Publishing quota guard (spec §4, §10.3: 79% / 80% / 100%).

Below 80% everything publishes. From 80% only the day's feed post and its Threads chain go out
(stories and extras wait), and an alert is raised. At 100% nothing publishes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

WARN_AT = 0.80


class QuotaDecision(StrEnum):
    OK = "ok"
    ESSENTIAL_ONLY = "essential_only"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class QuotaStatus:
    used: int
    total: int

    @property
    def ratio(self) -> float:
        return 1.0 if self.total <= 0 else self.used / self.total

    def decision(self) -> QuotaDecision:
        if self.ratio >= 1.0:
            return QuotaDecision.BLOCKED
        if self.ratio >= WARN_AT:
            return QuotaDecision.ESSENTIAL_ONLY
        return QuotaDecision.OK


def allowed(status: QuotaStatus, essential: bool, needed: int = 1) -> bool:
    """Whether a publish needing `needed` quota units may go out now."""
    if status.total <= 0 or status.used + needed > status.total:
        return False
    decision = status.decision()
    if decision is QuotaDecision.BLOCKED:
        return False
    if decision is QuotaDecision.ESSENTIAL_ONLY:
        return essential
    return True
