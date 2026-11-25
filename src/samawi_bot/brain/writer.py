"""Builds the writer and judge requests and runs write → check → judge (spec §6.3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date

from ..checks.draft_checks import CheckContext, check_draft, resolve_source
from ..checks.rules import CheckReport, load_too_easy
from ..sources import Sources
from ..sources.duas import DuaUnavailable
from ..sources.hadith import HadithRejected
from ..sources.quran import InvalidReference
from ..verify.facts import Fact, UnknownFact, fact_pack
from ..verify.gate import GateResult, decide
from .llm import LLM, LLMError
from .models import Draft, HookReport, HookSet, JudgeReport
from .prompts import HOOK_JUDGE_SYSTEM, HOOK_SYSTEM, JUDGE_SYSTEM, WRITER_SYSTEM


@dataclass
class Brief:
    day: date
    slot: str
    pillar: str
    format: str
    hook_formula: str
    bridge_options: list[str]
    sky_moment: str
    series: dict[str, object] | None = None
    avoid_topics: list[str] = field(default_factory=list)
    hook_examples: list[str] = field(default_factory=list)
    seasonal: str = ""


@dataclass
class Attempt:
    draft: Draft | None
    checks: CheckReport | None
    judge: JudgeReport | None
    gate: GateResult | None
    error: str | None = None

    @property
    def passed(self) -> bool:
        return bool(self.gate and self.gate.passed and self.checks and self.checks.ok)

    def notes(self) -> str:
        parts: list[str] = []
        if self.error:
            parts.append(self.error)
        if self.checks and not self.checks.ok:
            parts += self.checks.errors
        if self.gate and not self.gate.passed:
            parts += self.gate.reasons
        return "\n".join(f"- {p}" for p in parts)

    def report(self) -> dict[str, object]:
        return {
            "error": self.error,
            "checks": self.checks.errors if self.checks else None,
            "gate": self.gate.reasons if self.gate else None,
            "hook_index": self.gate.hook_index if self.gate else None,
        }


def writer_prompt(sources: Sources, brief: Brief, facts: list[Fact], retry_notes: str = "") -> str:
    hadith = sources.hadith.candidates(limit=60)
    duas = sources.duas.all()
    bridges = []
    for key in brief.bridge_options:
        # check_bridge, not just `shipped`: a shipped feature with no public name has no label to show.
        if sources.features.check_bridge(key) is None:
            f = sources.features.features[key]
            bridges.append(f'- {key}: "{sources.features.display_name(key)}" ({"berbayar" if f.paid else "gratis"})')
    too_easy = "\n".join(f"- {e.text}" for e in load_too_easy())
    lines = [
        f"Tanggal post: {brief.day.isoformat()} · slot {brief.slot} · nuansa langit {brief.sky_moment}",
        f"Pilar: {brief.pillar} · format: {brief.format} · formula hook utama: {brief.hook_formula}",
    ]
    if brief.seasonal:
        lines.append(f"Konteks musim/kalender: {brief.seasonal}")
    if brief.series:
        lines.append(f"Seri: {json.dumps(brief.series, ensure_ascii=False)} — isi `series` dengan nilai ini.")
    lines += [
        "",
        "## Fakta terhitung yang boleh kamu pakai (key → nilai). Angka lain DILARANG:",
        *(f.as_prompt() for f in facts),
        "",
        "## Fitur untuk bridge (pilih satu key):",
        *bridges,
        "",
        "## Kandidat hadis (id — judul). Hanya ini yang boleh dipakai:",
        *(f"- {h.id} — {h.title} ({h.reference})" for h in hadith),
        "",
        "## Doa/dzikir yang tersedia (id — bab — referensi):",
        *(f"- {d.id} — {d.chapter} — {d.reference}" for d in duas),
        "",
        "## Terlalu mudah (jangan jadikan fakta kunci):",
        too_easy,
    ]
    if brief.avoid_topics:
        lines += ["", "## Topik 60 hari terakhir (jangan ulang):", *(f"- {t}" for t in brief.avoid_topics)]
    if brief.hook_examples:
        lines += ["", "## Hook terbaik akun ini sejauh ini (contoh gaya):", *(f"- {h}" for h in brief.hook_examples)]
    if retry_notes:
        lines += ["", "## Draft sebelumnya GAGAL. Perbaiki semua masalah ini:", retry_notes]
    return "\n".join(lines)


def judge_prompt(sources: Sources, draft: Draft, day: date) -> str:
    excerpts = []
    for i, c in enumerate(draft.claims):
        try:
            text, _ = resolve_source(sources, c.source, day)
        except (InvalidReference, HadithRejected, DuaUnavailable, UnknownFact) as exc:
            text = f"[TIDAK DAPAT DIRESOLUSI: {exc}]"
        excerpts.append(f"[{i}] KLAIM: {c.text}\n    SUMBER ({c.source.type} {c.source.ref}): {text}")
    payload = draft.model_dump(exclude={"claims"})
    return (
        "## Klaim dan kutipan sumbernya\n"
        + "\n".join(excerpts)
        + "\n\n## Draft lengkap (hooks index 0–4; slides dimulai dari slide 2)\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
    )


HOOK_REASON = "no hook scores"
HOOK_ROUNDS = 2


def hooks_only_failure(gate: GateResult) -> bool:
    """True when the content passed and only the hooks fell short: worth a hook rewrite, not a redraft."""
    return not gate.passed and len(gate.reasons) == 1 and gate.reasons[0].startswith(HOOK_REASON)


def hook_prompt(draft: Draft, report: JudgeReport | HookReport) -> str:
    scores = "\n".join(
        f"- {draft.hooks[h.index]!r}: curiosity {h.curiosity_gap}, specificity {h.specificity}, payoff {h.payoff_honesty}, thumb-stop {h.thumb_stop}"
        for h in report.hooks
        if 0 <= h.index < len(draft.hooks)
    )
    return (
        f"Fakta kunci: {draft.key_fact}\nFormula utama: {draft.hook_formula.value}\n\n"
        "## Slide 2–3 (payoff yang harus dibayar hook)\n"
        + json.dumps([s.model_dump(exclude_defaults=True) for s in draft.slides[:2]], ensure_ascii=False, indent=1)
        + f"\n\n## Hook sebelumnya dan nilainya (semua < 8)\n{scores}\n\nCatatan juri: {report.notes}"
    )


def hook_judge_prompt(draft: Draft) -> str:
    return (
        "## Hook (index 0–4)\n"
        + "\n".join(f"[{i}] {h}" for i, h in enumerate(draft.hooks))
        + ("\n\n## Draft (slides dimulai dari slide 2)\n" + json.dumps(draft.model_dump(exclude={"claims", "hooks"}), ensure_ascii=False, indent=1))
    )


def improve_hooks(llm: LLM, sources: Sources, first: Attempt, ctx: CheckContext) -> Attempt:
    """Spec §3.2: the writer produces 5 alternatives and the judge picks the best, up to HOOK_ROUNDS."""
    assert first.draft is not None and first.judge is not None
    draft, report, last = first.draft, first.judge, first
    for _ in range(HOOK_ROUNDS):
        try:
            hooks = llm.structured(HOOK_SYSTEM, hook_prompt(draft, report), HookSet, effort="high").hooks
        except LLMError as exc:
            return Attempt(last.draft, last.checks, last.judge, last.gate, error=f"hook writer: {exc}")
        candidate = draft.model_copy(update={"hooks": hooks})
        checks = check_draft(candidate, sources, ctx)
        if not checks.ok:
            last = Attempt(candidate, checks, None, None)
            continue
        try:
            scored = llm.structured(HOOK_JUDGE_SYSTEM, hook_judge_prompt(candidate), HookReport, effort="high")
        except LLMError as exc:
            return Attempt(candidate, checks, None, None, error=f"hook judge: {exc}")
        merged = report.model_copy(update={"hooks": scored.hooks, "notes": scored.notes or report.notes})
        last = Attempt(candidate, checks, merged, decide(candidate, merged, checks.invalid_hooks))
        if last.passed:
            return last
        draft, report = candidate, merged
    return last


def attempt(llm: LLM, sources: Sources, brief: Brief, ctx: CheckContext, retry_notes: str = "", previous: Draft | None = None) -> Attempt:
    facts = fact_pack(sources, brief.pillar, brief.day)
    prompt = writer_prompt(sources, brief, facts, retry_notes)
    if previous is not None:
        prompt += (
            "\n\n## Draft sebelumnya — REVISI draft ini, perbaiki hanya masalah di atas; jangan menulis ulang dari nol "
            "dan jangan menambah klaim baru tanpa sumber:\n" + previous.model_dump_json()
        )
    try:
        draft = llm.structured(WRITER_SYSTEM, prompt, Draft, effort="high")
    except LLMError as exc:
        return Attempt(None, None, None, None, error=f"writer: {exc}")
    checks = check_draft(draft, sources, ctx)
    if not checks.ok:
        return Attempt(draft, checks, None, None)
    try:
        report = llm.structured(JUDGE_SYSTEM, judge_prompt(sources, draft, brief.day), JudgeReport, effort="high")
    except LLMError as exc:
        return Attempt(draft, checks, None, None, error=f"judge: {exc}")
    result = Attempt(draft, checks, report, decide(draft, report, checks.invalid_hooks))
    if result.gate is not None and hooks_only_failure(result.gate):
        return improve_hooks(llm, sources, result, ctx)
    return result


def write_verified(llm: LLM, sources: Sources, brief: Brief, ctx: CheckContext) -> list[Attempt]:
    """At most two attempts; the second revises the first against its failure notes. Caller falls back on failure."""
    first = attempt(llm, sources, brief, ctx)
    if first.passed:
        return [first]
    return [first, attempt(llm, sources, brief, ctx, retry_notes=first.notes(), previous=first.draft)]
