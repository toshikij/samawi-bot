"""write → check → judge (spec §6.3): retries, failure notes, and what the writer and judge see."""

from __future__ import annotations

from datetime import date

import pytest

from conftest import TODAY, FakeLLM, judge_report, load_draft
from samawi_bot.brain.llm import LLMError
from samawi_bot.brain.models import Claim, Draft, SourceRef
from samawi_bot.brain.prompts import JUDGE_SYSTEM, WRITER_SYSTEM
from samawi_bot.brain.writer import Attempt, Brief, attempt, improve_hooks, judge_prompt, write_verified, writer_prompt
from samawi_bot.checks.draft_checks import CheckContext, resolve_source
from samawi_bot.checks.rules import CheckReport, load_too_easy
from samawi_bot.sources import Sources
from samawi_bot.verify.facts import fact_pack
from samawi_bot.verify.gate import GateResult, decide


def brief(**overrides: object) -> Brief:
    base: dict[str, object] = {
        "day": TODAY,
        "slot": "siang_feed",
        "pillar": "fakta_quran",
        "format": "carousel",
        "hook_formula": "ternyata",
        "bridge_options": ["tafsir", "khatamPlanner"],
        "sky_moment": "dzuhur",
    }
    base.update(overrides)
    return Brief(**base)  # type: ignore[arg-type]


def bad_draft() -> Draft:
    return load_draft(bridge_feature="customAdzanVoices")


def low_hooks(d: Draft) -> object:
    return judge_report(d, hooks=[{"index": i, "curiosity_gap": 5, "specificity": 9, "payoff_honesty": 9, "thumb_stop": 9} for i in range(len(d.hooks))])


# ── write_verified ─────────────────────────────────────────────────────────────────────────


def test_passes_first_time(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [d], "JudgeReport": [judge_report(d)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert len(attempts) == 1
    a = attempts[0]
    assert a.passed
    assert a.gate is not None and a.gate.hook_index == 0
    assert [c[0] for c in llm.calls] == ["Draft", "JudgeReport"]
    assert llm.calls[0][1] == WRITER_SYSTEM
    assert llm.calls[1][1] == JUDGE_SYSTEM
    assert a.notes() == ""
    assert a.report() == {"error": None, "checks": [], "gate": [], "hook_index": 0}


def test_writer_llm_error_then_retry_passes(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [LLMError("Claude refused the request"), d], "JudgeReport": [judge_report(d)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert len(attempts) == 2
    first, second = attempts
    assert not first.passed
    assert first.draft is None and first.checks is None and first.judge is None and first.gate is None
    assert first.error == "writer: Claude refused the request"
    assert first.notes() == "- writer: Claude refused the request"
    assert first.report() == {"error": "writer: Claude refused the request", "checks": None, "gate": None, "hook_index": None}
    assert second.passed
    retry_prompt = llm.calls[1][2]
    assert "Draft sebelumnya GAGAL" in retry_prompt
    assert "writer: Claude refused the request" in retry_prompt


def test_check_failure_skips_judge_and_retry_carries_errors(sources: Sources, ctx: CheckContext) -> None:
    good = load_draft()
    llm = FakeLLM({"Draft": [bad_draft(), good], "JudgeReport": [judge_report(good)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert [c[0] for c in llm.calls] == ["Draft", "Draft", "JudgeReport"]
    first = attempts[0]
    assert first.checks is not None and not first.checks.ok
    assert first.judge is None and first.gate is None
    assert not first.passed
    err = "bridge feature 'customAdzanVoices' has status planned, not shipped"
    assert err in first.checks.errors
    assert f"- {err}" in first.notes()
    assert err in llm.calls[1][2]
    assert "Draft sebelumnya GAGAL" not in llm.calls[0][2]
    assert attempts[1].passed


def test_judge_llm_error_is_a_failed_attempt(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [d, d], "JudgeReport": [LLMError("Claude hit max_tokens"), judge_report(d)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert len(attempts) == 2
    assert attempts[0].error == "judge: Claude hit max_tokens"
    assert attempts[0].checks is not None and attempts[0].checks.ok
    assert not attempts[0].passed
    assert attempts[1].passed


def test_malformed_judge_report_retries(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    malformed = judge_report(d, claims=[{"index": 0, "verdict": "supported"}])  # claim 1 missing
    llm = FakeLLM({"Draft": [d, d], "JudgeReport": [malformed, judge_report(d)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    first = attempts[0]
    assert first.gate is not None and not first.gate.passed
    assert any(r.startswith("malformed judge report") for r in first.gate.reasons)
    assert "malformed judge report" in llm.calls[2][2]  # the second writer call
    assert attempts[1].passed


def test_low_hook_scores_retry(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [d, d], "JudgeReport": [low_hooks(d), judge_report(d)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert len(attempts) == 2
    assert attempts[0].gate is not None and attempts[0].gate.hook_index is None
    assert "no hook scores" in attempts[0].notes()
    assert attempts[0].report()["hook_index"] is None
    assert attempts[1].passed


def test_two_failures_give_two_attempts_none_passed(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [d, bad_draft()], "JudgeReport": [low_hooks(d)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert len(attempts) == 2
    assert not any(a.passed for a in attempts)
    # The hooks-only failure tries a hook rewrite first (none queued → LLMError), then the redraft.
    assert [c[0] for c in llm.calls] == ["Draft", "JudgeReport", "HookSet", "Draft"]


def test_gate_pass_but_checks_failing_is_not_passed() -> None:
    a = Attempt(load_draft(), CheckReport(errors=["x"]), None, GateResult(True, 0))
    assert not a.passed
    assert a.notes() == "- x"


def test_attempt_notes_combine_error_checks_and_gate() -> None:
    a = Attempt(None, CheckReport(errors=["c1", "c2"]), None, GateResult(False, None, ["g1"]), error="e")
    assert a.notes() == "- e\n- c1\n- c2\n- g1"
    assert a.report() == {"error": "e", "checks": ["c1", "c2"], "gate": ["g1"], "hook_index": None}


def test_single_attempt_passes_retry_notes(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [d], "JudgeReport": [judge_report(d)]})
    a = attempt(llm, sources, brief(), ctx, retry_notes="- fix this")
    assert a.passed
    assert "## Draft sebelumnya GAGAL. Perbaiki semua masalah ini:\n- fix this" in llm.calls[0][2]


# ── writer_prompt ──────────────────────────────────────────────────────────────────────────


def test_writer_prompt_lists_only_shipped_named_bridges(sources: Sources) -> None:
    b = brief(bridge_options=["tafsir", "qibla", "customAdzanVoices", "offlineMurottal", "locationPicker", "deletedFeature"])
    p = writer_prompt(sources, b, [])
    assert '- tafsir: "Tafsir" (berbayar)' in p
    assert '- qibla: "Arah kiblat" (gratis)' in p
    for key in ("customAdzanVoices", "offlineMurottal", "locationPicker", "deletedFeature"):
        assert key not in p


def test_writer_prompt_lists_admitted_hadith_only(sources: Sources) -> None:
    p = writer_prompt(sources, brief(), [])
    admitted = [h.id for h in sources.hadith.candidates()]
    assert admitted
    for hid in admitted:
        assert f"- {hid} — " in p
    for rejected in ("4191", "5344", "66535"):
        assert f"- {rejected} — " not in p


def test_writer_prompt_contents(sources: Sources) -> None:
    facts = fact_pack(sources, "fakta_quran", TODAY)
    b = brief(
        seasonal="Bulan Rajab: menjelang Ramadan.",
        series={"id": "s-1", "title": "Seri Ayat", "part": 2, "total": 5},
        avoid_topics=["Ayat kursi", "Dua bismillah"],
        hook_examples=["128 kata. Ayat ini soal utang"],
    )
    p = writer_prompt(sources, b, facts, retry_notes="- claim 1 unsupported")
    assert "Tanggal post: 2026-11-26 · slot siang_feed · nuansa langit dzuhur" in p
    assert "Pilar: fakta_quran · format: carousel · formula hook utama: ternyata" in p
    assert "Konteks musim/kalender: Bulan Rajab: menjelang Ramadan." in p
    assert '"title": "Seri Ayat"' in p and "isi `series`" in p
    for f in facts:
        assert f.as_prompt() in p
    for d in sources.duas.all():
        assert f"- {d.id} — {d.chapter} — {d.reference}" in p
    for e in load_too_easy():
        assert f"- {e.text}" in p
    assert "## Topik 60 hari terakhir (jangan ulang):\n- Ayat kursi\n- Dua bismillah" in p
    assert "## Hook terbaik akun ini sejauh ini (contoh gaya):\n- 128 kata. Ayat ini soal utang" in p
    assert p.endswith("## Draft sebelumnya GAGAL. Perbaiki semua masalah ini:\n- claim 1 unsupported")


def test_writer_prompt_omits_optional_sections(sources: Sources) -> None:
    p = writer_prompt(sources, brief(), [])
    assert "Konteks musim" not in p
    assert "Seri:" not in p
    assert "Topik 60 hari" not in p
    assert "Hook terbaik" not in p
    assert "GAGAL" not in p


# ── judge_prompt ───────────────────────────────────────────────────────────────────────────


def test_judge_prompt_includes_resolved_source_text(sources: Sources) -> None:
    d = load_draft()
    p = judge_prompt(sources, d, TODAY)
    for i, c in enumerate(d.claims):
        text, _ = resolve_source(sources, c.source, TODAY)
        assert f"[{i}] KLAIM: {c.text}\n    SUMBER ({c.source.type} {c.source.ref}): {text}" in p
    assert "hendaklah kamu mencatatnya" in p  # the Kemenag translation of 2:282, from the snapshot
    assert "TIDAK DAPAT DIRESOLUSI" not in p
    # the draft is shown without its claims block, but with hooks and slides
    body = p.split("## Draft lengkap", 1)[1]
    assert '"claims"' not in body
    assert d.hooks[0] in body


@pytest.mark.parametrize(
    "source",
    [
        SourceRef(type="ayat", ref="115:1"),
        SourceRef(type="hadith", ref="4191"),
        SourceRef(type="hadith", ref="999999"),
        SourceRef(type="dua", ref="no.such.dua"),
        SourceRef(type="fact", ref="quran:nope"),
    ],
)
def test_judge_prompt_marks_unresolvable_sources(sources: Sources, source: SourceRef) -> None:
    d = load_draft()
    d.claims.append(Claim(text="Klaim tanpa dasar", source=source))
    p = judge_prompt(sources, d, date(2026, 11, 26))
    assert f"[2] KLAIM: Klaim tanpa dasar\n    SUMBER ({source.type} {source.ref}): [TIDAK DAPAT DIRESOLUSI:" in p
    assert p.count("TIDAK DAPAT DIRESOLUSI") == 1


# ── Hook rounds (spec §3.2: 5 alternatives, judge picks the best) ─────────────────────────────

from samawi_bot.brain.models import HookReport, HookSet  # noqa: E402
from samawi_bot.brain.writer import (  # noqa: E402
    HOOK_ROUNDS,
    KEEP_BEST,
    combine_hooks,
    hook_judge_prompt,
    hook_prompt,
    hooks_only_failure,
    near_misses,
)

NEW_HOOKS = [
    "Ternyata ayat 282 Al-Baqarah bukan soal salat",
    "128 kata dalam satu ayat, dan isinya bukan ibadah",
    "Kenapa ayat terpanjang Al-Qur'an membahas utang?",
    "Ayat terpanjang Al-Qur'an ada di Al-Baqarah 282",
    "Satu ayat di Al-Qur'an mengatur cara mencatat utang",
]


def hook_scores(good: bool) -> HookReport:
    v = 9 if good else 6
    return HookReport.model_validate(
        {"hooks": [{"index": i, "curiosity_gap": v, "specificity": 9, "payoff_honesty": 9, "thumb_stop": v} for i in range(5)], "notes": "n"}
    )


def test_hooks_only_failure_rewrites_hooks_and_passes(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [d], "JudgeReport": [low_hooks(d)], "HookSet": [HookSet(hooks=NEW_HOOKS)], "HookReport": [hook_scores(True)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert len(attempts) == 1 and attempts[0].passed
    # All five tied, so the first two are kept; three new hooks fill the rest.
    assert attempts[0].draft is not None and attempts[0].draft.hooks == d.hooks[:KEEP_BEST] + NEW_HOOKS[: 5 - KEEP_BEST]
    assert [c[0] for c in llm.calls] == ["Draft", "JudgeReport", "HookSet", "HookReport"]
    assert "curiosity 5" in llm.calls[2][2] and "Al-Baqarah" in llm.calls[2][2]  # scores + payoff slides in the prompt
    assert "kurang: curiosity 5 < 8" in llm.calls[2][2] and "[DIPERTAHANKAN]" in llm.calls[2][2]


def test_hook_rounds_stop_after_the_limit(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM(
        {
            "Draft": [d, d],
            "JudgeReport": [low_hooks(d), judge_report(d)],
            "HookSet": [HookSet(hooks=NEW_HOOKS)] * HOOK_ROUNDS,
            "HookReport": [hook_scores(False)] * HOOK_ROUNDS,
        }
    )
    attempts = write_verified(llm, sources, brief(), ctx)
    assert [c[0] for c in llm.calls].count("HookSet") == HOOK_ROUNDS
    assert not attempts[0].passed and attempts[1].passed


def test_hook_rewrite_failing_checks_tries_again(sources: Sources, ctx: CheckContext) -> None:
    """A round whose candidate fails the checks (here: too few distinct hooks) is spent, not judged."""
    d = load_draft()
    short = HookSet(hooks=[d.hooks[0], d.hooks[1]])  # only repeats the kept hooks → 2 hooks, check_draft wants 5
    llm = FakeLLM({"Draft": [d], "JudgeReport": [low_hooks(d)], "HookSet": [short, HookSet(hooks=NEW_HOOKS)], "HookReport": [hook_scores(True)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert attempts[0].passed and [c[0] for c in llm.calls].count("HookSet") == 2
    assert [c[0] for c in llm.calls].count("HookReport") == 1


def test_banned_new_hooks_are_discarded_and_never_chosen(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    banned = HookSet(hooks=[f"Dijamin viral: 99% orang tidak tahu ayat {n}" for n in (282, 283, 284, 285, 286)])
    llm = FakeLLM({"Draft": [d], "JudgeReport": [low_hooks(d)], "HookSet": [banned], "HookReport": [hook_scores(True)]})
    final = write_verified(llm, sources, brief(), ctx)[0]
    assert final.passed and final.checks is not None and final.gate is not None and final.draft is not None
    assert final.checks.invalid_hooks == {2, 3, 4}  # the three new hooks, all banned
    assert final.gate.hook_index in (0, 1) and "Dijamin" not in final.draft.hooks[final.gate.hook_index]


def test_hook_judge_error_falls_through_to_redraft(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [d, d], "JudgeReport": [low_hooks(d), judge_report(d)], "HookSet": [HookSet(hooks=NEW_HOOKS)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert attempts[0].error == "hook judge: FakeLLM has no HookReport response queued"
    assert attempts[1].passed


def test_hooks_only_failure_detection() -> None:
    assert hooks_only_failure(GateResult(False, None, ["no hook scores ≥ 8 on …"]))
    assert not hooks_only_failure(GateResult(False, None, ["no hook scores ≥ 8 on …", "novelty 50% < 60% (too easy)"]))
    assert not hooks_only_failure(GateResult(True, 0, []))


def test_retry_revises_the_previous_draft(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    llm = FakeLLM({"Draft": [bad_draft(), d], "JudgeReport": [judge_report(d)]})
    attempts = write_verified(llm, sources, brief(), ctx)
    assert attempts[1].passed
    second_prompt = llm.calls[1][2]
    assert "REVISI draft ini" in second_prompt and "customAdzanVoices" in second_prompt


def scored(rows: list[tuple[int, int, int, int]]) -> HookReport:
    return HookReport.model_validate(
        {"hooks": [{"index": i, "curiosity_gap": c, "specificity": s, "payoff_honesty": p, "thumb_stop": t} for i, (c, s, p, t) in enumerate(rows)]}
    )


def test_near_misses_rank_by_the_weakest_gated_score() -> None:
    d = load_draft()
    # Hook 0 has the highest total but a 4 on curiosity; hooks 2 and 3 are 7s across the board.
    report = scored([(4, 10, 10, 10), (6, 7, 7, 7), (7, 7, 7, 6), (7, 7, 7, 7), (5, 5, 5, 5)])
    assert near_misses(d, report) == [3, 2]
    assert near_misses(d, report, excluded={3}) == [2, 1]  # hooks the checks discarded are never kept
    assert near_misses(d, scored([(9, 9, 9, 9)] * 3 + [(1, 1, 1, 1)] * 2)) == [0, 1]  # ties: lowest index first


def test_combine_hooks_keeps_near_misses_and_skips_repeats() -> None:
    d = load_draft()
    out = combine_hooks(d, [3, 1], [d.hooks[3], "Baru A", "Baru B", "Baru C", "Baru D"])
    assert out == [d.hooks[3], d.hooks[1], "Baru A", "Baru B", "Baru C"]
    assert combine_hooks(d, [], NEW_HOOKS) == NEW_HOOKS


def test_hook_prompt_names_each_shortfall() -> None:
    d = load_draft()
    p = hook_prompt(d, scored([(6, 9, 9, 6), (9, 9, 9, 9), (8, 7, 8, 7), (8, 8, 8, 7), (1, 1, 1, 1)]), keep=[3])
    lines = [line for line in p.splitlines() if line.startswith("- ")]
    assert lines[0].endswith("kurang: curiosity 6 < 8; thumb-stop 6 < 7")
    assert lines[1].endswith("kurang: lolos") and "specificity 7 < 8" in lines[2]
    assert lines[3].startswith(f"- [DIPERTAHANKAN] {d.hooks[3]!r}") and p.count("[DIPERTAHANKAN]") == 2  # line + instruction
    assert "[DIPERTAHANKAN]" not in hook_prompt(d, scored([(5, 5, 5, 5)] * 5))  # no keep, no instruction


def test_hook_rounds_carry_the_best_hook_forward(sources: Sources, ctx: CheckContext) -> None:
    """Round 1's near-miss survives into round 2, is re-scored there, and the gate picks it on the fresh score."""
    d = load_draft()
    first = judge_report(d, hooks=[{"index": i, "curiosity_gap": 7 if i == 2 else 5, "specificity": 9, "payoff_honesty": 9, "thumb_stop": 9} for i in range(5)])
    round1 = HookSet(hooks=["R1 a", "R1 b", "R1 c", "R1 d", "R1 e"])
    round2 = HookSet(hooks=["R2 a", "R2 b", "R2 c", "R2 d", "R2 e"])
    r1_scores = scored([(7, 8, 8, 8), (6, 8, 8, 8), (5, 8, 8, 8), (5, 8, 8, 8), (5, 8, 8, 8)])  # kept: d.hooks[2], then d.hooks[0]
    r2_scores = scored([(9, 9, 9, 9), (5, 9, 9, 9), (5, 9, 9, 9), (5, 9, 9, 9), (5, 9, 9, 9)])
    llm = FakeLLM({"Draft": [d], "JudgeReport": [first], "HookSet": [round1, round2], "HookReport": [r1_scores, r2_scores]})
    attempts = write_verified(llm, sources, brief(), ctx)
    final = attempts[0]
    assert final.passed and final.draft is not None and final.gate is not None
    assert final.draft.hooks == [d.hooks[2], d.hooks[0], "R2 a", "R2 b", "R2 c"]
    assert final.gate.hook_index == 0  # the carried hook, chosen on its round-2 score
    hook_judge_prompts = [u for name, _, u in llm.calls if name == "HookReport"]
    assert f"[0] {d.hooks[2]}" in hook_judge_prompts[0] and f"[0] {d.hooks[2]}" in hook_judge_prompts[1]  # re-scored each round


def test_kept_hooks_never_include_ones_the_checks_discarded(sources: Sources, ctx: CheckContext) -> None:
    d = load_draft()
    first = judge_report(d, hooks=[{"index": i, "curiosity_gap": 7 if i == 0 else 5, "specificity": 9, "payoff_honesty": 9, "thumb_stop": 9} for i in range(5)])
    attempt0 = Attempt(d, CheckReport(invalid_hooks={0}), first, decide(d, first, {0}))
    llm = FakeLLM({"HookSet": [HookSet(hooks=NEW_HOOKS)], "HookReport": [hook_scores(True)]})
    result = improve_hooks(llm, sources, attempt0, ctx)
    assert result.draft is not None and d.hooks[0] not in result.draft.hooks
    assert result.draft.hooks[:KEEP_BEST] == [d.hooks[1], d.hooks[2]]


def test_hook_prompts_render() -> None:
    d = load_draft()
    assert "[0] " + d.hooks[0] in hook_judge_prompt(d)
    report = judge_report(
        d,
        hooks=[{"index": 9, "curiosity_gap": 1, "specificity": 1, "payoff_honesty": 1, "thumb_stop": 1}]
        + [{"index": i, "curiosity_gap": 5, "specificity": 5, "payoff_honesty": 5, "thumb_stop": 5} for i in range(1, 5)],
    )
    assert "index 9" not in hook_prompt(d, report)  # out-of-range indices are skipped, not crashed on


# ── Source texts in the prompt ─────────────────────────────────────────────────────────────────

from samawi_bot.brain.writer import HADITH_PER_PROMPT, cited_excerpts, hadith_for  # noqa: E402


def test_writer_sees_full_hadith_text_not_just_titles(sources: Sources) -> None:
    prompt = writer_prompt(sources, brief(), [])
    h = sources.hadith.get("2962")
    assert h.text_id in prompt
    assert "jangan menambah konteks riwayat dari ingatan" in prompt


def test_hadith_window_rotates_by_day(sources: Sources, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import timedelta

    fake = [object() for _ in range(HADITH_PER_PROMPT * 3 + 7)]
    monkeypatch.setattr(sources.hadith, "candidates", lambda limit=None: fake)
    a, b = hadith_for(sources, brief()), hadith_for(sources, brief(day=TODAY + timedelta(days=1)))
    assert len(a) == len(b) == HADITH_PER_PROMPT and a != b
    assert set(map(id, a)) <= set(map(id, fake))


def test_small_libraries_are_shown_whole(sources: Sources) -> None:
    assert len(hadith_for(sources, brief())) == len(sources.hadith.candidates()) <= HADITH_PER_PROMPT


def test_revision_carries_the_cited_excerpts(sources: Sources) -> None:
    d = load_draft()
    text = cited_excerpts(sources, d, TODAY)
    assert "ayat 2:282" in text and sources.hadith.get("2962").text_id in text and "fact quran:longest_ayah" in text
    broken = load_draft(claims=[{"text": "x", "source": {"type": "hadith", "ref": "4191"}}])
    assert "[TIDAK DAPAT DIRESOLUSI" in cited_excerpts(sources, broken, TODAY)
