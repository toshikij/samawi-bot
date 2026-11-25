"""The judge's verdict turned into a publish/no-publish decision (spec §3.1, §3.2, §6.3).

Fails closed: a judge report with missing claims, out-of-range scores or duplicate indices is
treated as a failed draft, never as a pass.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..brain.models import Draft, HookScore, JudgeReport

HOOK_MIN = 8  # spec §3.2: curiosity gap, specificity and payoff honesty
THUMB_STOP_MIN = 7  # our extra scroll-stop score; ranks hooks and sets a floor, but the spec's three decide
NOVELTY_MIN = 60
ACCESSIBILITY_MIN = 8


@dataclass
class GateResult:
    passed: bool
    hook_index: int | None
    reasons: list[str] = field(default_factory=list)


def _hook_ok(h: HookScore) -> bool:
    return min(h.curiosity_gap, h.specificity, h.payoff_honesty) >= HOOK_MIN and h.thumb_stop >= THUMB_STOP_MIN


def _hook_rank(h: HookScore) -> tuple[int, int, int]:
    return (h.payoff_honesty + h.curiosity_gap + h.thumb_stop + h.specificity, h.thumb_stop, -h.index)


def validate_report(draft: Draft, report: JudgeReport) -> list[str]:
    problems: list[str] = []
    claim_idx = [c.index for c in report.claims]
    if sorted(claim_idx) != list(range(len(draft.claims))):
        problems.append(f"judge covered claims {sorted(claim_idx)}, expected 0..{len(draft.claims) - 1}")
    hook_idx = [h.index for h in report.hooks]
    if sorted(hook_idx) != list(range(len(draft.hooks))):
        problems.append(f"judge scored hooks {sorted(hook_idx)}, expected 0..{len(draft.hooks) - 1}")
    for h in report.hooks:
        if not all(1 <= v <= 10 for v in (h.curiosity_gap, h.specificity, h.payoff_honesty, h.thumb_stop)):
            problems.append(f"hook {h.index} has a score outside 1–10")
    if not 0 <= report.novelty_pct <= 100:
        problems.append(f"novelty {report.novelty_pct} outside 0–100")
    if not 0 <= report.accessibility <= 10:
        problems.append(f"accessibility {report.accessibility} outside 0–10")
    return problems


def decide(draft: Draft, report: JudgeReport, excluded: frozenset[int] | set[int] = frozenset()) -> GateResult:
    """`excluded`: hook indices the deterministic checks discarded; they can never be chosen."""
    reasons = validate_report(draft, report)
    if reasons:
        return GateResult(False, None, ["malformed judge report: " + r for r in reasons])
    for c in report.claims:
        if c.verdict != "supported":
            reasons.append(f"claim {c.index} {c.verdict}: {draft.claims[c.index].text[:80]!r} {c.note}".strip())
    if report.contested_framing:
        reasons.append(f"contested framing: {report.contested_note}")
    if report.novelty_pct < NOVELTY_MIN:
        reasons.append(f"novelty {report.novelty_pct}% < {NOVELTY_MIN}% (too easy)")
    if report.accessibility < ACCESSIBILITY_MIN:
        reasons.append(f"accessibility {report.accessibility}/10 < {ACCESSIBILITY_MIN}")
    if not report.payoff_by_slide_3:
        reasons.append("hook is not paid off by slide 3")
    if not report.tone_ok:
        reasons.append(f"tone: {report.notes}")
    passing = [h for h in report.hooks if _hook_ok(h) and h.index not in excluded]
    if not passing:
        reasons.append(f"no hook scores ≥ {HOOK_MIN} on curiosity, specificity and payoff honesty (and ≥ {THUMB_STOP_MIN} thumb-stop)")
    best = max(passing, key=_hook_rank).index if passing else None
    return GateResult(not reasons, best if not reasons else None, reasons)
