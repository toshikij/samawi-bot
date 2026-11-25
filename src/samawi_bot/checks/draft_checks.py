"""Every deterministic check a draft must pass before the judge sees it (spec §6.3 step 2).

A failing check blocks the draft. None of these can be relaxed by the learner (§9).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from ..brain.models import Draft, SourceRef
from ..config import LaunchMode
from ..sources import Sources
from ..sources.duas import DuaUnavailable
from ..sources.hadith import HadithRejected
from ..sources.quran import InvalidReference, parse_reference
from ..verify.facts import Fact, UnknownFact, compute
from .rules import CheckReport, TooEasy, blocked_topic_hits, find, load_rules, load_too_easy, topic_hash
from .text import canonical_number, count_chars, emoji_count, is_shouting, numbers, utf8_len, variants, word_count

FOLLOW_HANDLE = "@samawi.indonesia"
ARABIC_SCRIPT = re.compile("[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff\ufb50-\ufdff\ufe70-\ufefc]")
DATED_FACTS = frozenset({"prayer", "compare", "extremes", "hijri"})
re_dated = re.compile(r"\b(hari ini|pekan ini|minggu ini|bulan ini|tahun ini|kemarin)\b")
# Install CTAs that may only run once the app is live (§8.4). Matched on the raw lowercase text and on
# every normalised form, so "Play\u200bStore", "PlayStore" or "link  di  bio" are caught too.
re_install_cta = re.compile(r"link di bio|komentar samawi|play ?store|download")


def _forms(text: str) -> list[str]:
    return [text.lower(), *variants(text)]


@dataclass
class CheckContext:
    today: date
    launch_mode: LaunchMode
    topic_history: dict[str, date] = field(default_factory=dict)  # topic_hash → last used
    recent_bridges: list[str] = field(default_factory=list)  # bridge keys of the previous days, newest first
    extra_too_easy: list[TooEasy] = field(default_factory=list)
    evergreen: bool = False  # evergreen posts may cite nothing tied to a date
    extra_sources: list[SourceRef] = field(default_factory=list)  # e.g. the ayat of the day for Threads


def resolve_source(sources: Sources, ref: SourceRef, today: date) -> tuple[str, Fact | None]:
    """The verbatim source text a claim rests on (and the Fact, for computed claims).

    Raises InvalidReference, HadithRejected, DuaUnavailable or UnknownFact when it does not resolve.
    """
    if ref.type == "ayat":
        rng = parse_reference(ref.ref, sources.quran)
        text = " ".join(sources.quran.translation(r) for r in rng.refs)
        return f"{sources.quran.citation(rng)} {text}", None
    if ref.type == "hadith":
        h = sources.hadith.get(ref.ref)
        return f"({h.reference}) {h.text_id}", None
    if ref.type == "dua":
        d = sources.duas.get(ref.ref)
        return f"({d.reference}) {d.indonesian}", None
    fact = compute(sources, ref.ref, today)
    return f"{fact.label}: {', '.join(fact.values)} {fact.detail}", fact


def all_copy(draft: Draft) -> list[tuple[str, str]]:
    """(where, text) for every piece of copy that will be published."""
    out: list[tuple[str, str]] = [(f"hook {i + 1}", h) for i, h in enumerate(draft.hooks)]
    for i, s in enumerate(draft.slides):
        for part in (s.heading, s.body, *s.items, s.left_label, s.left_value, s.right_label, s.right_value):
            if part:
                out.append((f"slide {i + 2}", part))
    out += [
        ("takeaway", draft.takeaway),
        ("bridge", draft.bridge_line),
        ("teaser", draft.next_teaser),
        ("share prompt", draft.share_prompt),
        ("caption", draft.caption),
        ("threads subuh", draft.threads_subuh),
        ("threads malam", draft.threads_malam),
        *((f"thread {i + 1}", t) for i, t in enumerate(draft.thread)),
        *((f"claim {i + 1}", c.text) for i, c in enumerate(draft.claims)),
    ]
    return [(w, t) for w, t in out if t]


def _check_hook(report: CheckReport, hook: str, where: str) -> None:
    if word_count(hook) > load_rules().limits["cover_max_words"]:
        report.fail(f"{where}: more than {load_rules().limits['cover_max_words']} words")
    if not any(ch.isdigit() for ch in hook) and not _has_specific(hook):
        report.fail(f"{where}: needs a number, a proper noun, or a surah/ayat reference")


def _has_specific(text: str) -> bool:
    words = [w for w in text.split()[1:] if w[:1].isalpha()]  # skip the sentence-initial capital
    return any(w[0].isupper() for w in words) or "QS" in text or "surah" in text.lower()


def check_draft(draft: Draft, sources: Sources, ctx: CheckContext) -> CheckReport:
    report = CheckReport()
    rules = load_rules()
    limits = rules.limits
    copy = all_copy(draft)

    # ── Sources: every claim resolves; every slide source resolves; collect allowed numbers ──
    allowed: set[str] = set()
    cited_facts: list[Fact] = []
    refs: list[SourceRef] = [c.source for c in draft.claims] + [s.source for s in draft.slides if s.source] + ctx.extra_sources
    if not draft.claims:
        report.fail("draft has no claims; every factual statement needs a source")
    for ref in refs:
        try:
            text, fact = resolve_source(sources, ref, ctx.today)
        except (InvalidReference, HadithRejected, DuaUnavailable, UnknownFact) as exc:
            report.fail(f"source {ref.type}:{ref.ref} does not resolve: {exc}")
            continue
        allowed.update(numbers(text))
        if fact:
            cited_facts.append(fact)
            allowed.update(canonical_number(v) for v in fact.values)
        if ref.type == "ayat":
            rng = parse_reference(ref.ref)
            allowed.update({str(rng.surah), str(rng.start), str(rng.end)})
    for s in draft.slides:
        if s.template in ("ayat", "hadith", "dua") and (s.source is None or s.source.type != s.template):
            report.fail(f"{s.template} slide must carry a {s.template} source; the renderer pulls the text from it")
    if draft.series:
        allowed.update({str(draft.series.part), str(draft.series.total)})
        if not 1 <= draft.series.part <= draft.series.total:
            report.fail(f"series part {draft.series.part}/{draft.series.total} is out of range")
    # The post's own date may be named ("Kamis, 26 November 2026").
    allowed.update({str(ctx.today.day), str(ctx.today.year)})
    report.allowed_numbers = allowed

    # ── Numbers: every number in the copy must come from a cited source or computation ──
    for where, text in copy:
        for n in numbers(text):
            if n not in allowed:
                report.fail(f"{where}: number {n} is not backed by any cited source or computation")

    # ── Notes that must accompany a fact (Ramadan/Syawal dates pending isbat) ──
    whole = "\n".join(t for _, t in copy).lower()
    for fact in cited_facts:
        for note in fact.notes:
            # The exact note, or both of its parts in the writer's own words; never "sidang isbat" alone.
            if note.lower() not in whole and not ("perkiraan" in whole and "sidang isbat" in whole):
                report.fail(f"{fact.key} requires the wording '{note}'")

    # ── Bridge: shipped features only, no feature 3 days running ──
    reason = sources.features.check_bridge(draft.bridge_feature)
    if reason:
        report.fail(reason)
    elif ctx.recent_bridges[:2] == [draft.bridge_feature] * 2:
        report.fail(f"bridge {draft.bridge_feature!r} would appear 3 days in a row")
    if reason is None and sources.features.display_name(draft.bridge_feature).lower() not in draft.bridge_line.lower():
        report.fail("bridge line must name the feature as the app names it")
    if "tanpa iklan" not in draft.bridge_line.lower():
        report.fail("bridge line must say 'tanpa iklan'")

    # ── No Arabic typed by the model (spec §6.1, §6.5): scripture only ever comes from sources ──
    for where, text in copy:
        if ARABIC_SCRIPT.search(text):
            report.fail(f"{where}: contains Arabic script; reference the source instead of typing it")

    # ── Banned patterns, competitors, blocked topics ──
    for where, text in copy:
        for hit in find(rules.banned_cta, text):
            report.fail(f"{where}: banned CTA phrase {hit!r}")
        for hit in find(rules.competitors, text):
            report.fail(f"{where}: competitor name {hit!r}")
        for topic, hits in blocked_topic_hits(rules, text).items():
            report.fail(f"{where}: blocked topic {topic} ({', '.join(hits)})")
        if is_shouting(text):
            report.fail(f"{where}: all-caps shouting")
        if emoji_count(text) > 3:
            report.fail(f"{where}: emoji spam")
    hook_texts = [*((f"hook {i + 1}", h) for i, h in enumerate(draft.hooks)), ("thread 1", draft.thread[0] if draft.thread else "")]
    for where, text in hook_texts:
        for hit in find(rules.banned_hook, text):
            report.fail(f"{where}: banned hook pattern {hit!r}")

    # ── Hooks ──
    if len(draft.hooks) != 5:
        report.fail(f"expected 5 hook candidates, got {len(draft.hooks)}")
    for i, h in enumerate(draft.hooks):
        _check_hook(report, h, f"hook {i + 1}")
    if draft.thread:
        first_line = draft.thread[0].split("\n", 1)[0]
        if count_chars(first_line) > limits["threads_hook_max_chars"]:
            report.fail(f"thread 1: first line over {limits['threads_hook_max_chars']} characters")

    # ── Limits ──
    n_slides = len(draft.slides) + 2  # + cover + CTA added by code
    if draft.format.value == "carousel" and not limits["carousel_slides_min"] <= n_slides <= limits["carousel_slides_max"]:
        report.fail(f"carousel has {n_slides} slides; needs {limits['carousel_slides_min']}–{limits['carousel_slides_max']}")
    if count_chars(draft.caption) > limits["caption_max_chars"]:
        report.fail(f"caption is {count_chars(draft.caption)} characters; max {limits['caption_max_chars']}")
    if len(draft.hashtags) > limits["hashtags_max"]:
        report.fail(f"{len(draft.hashtags)} hashtags; max {limits['hashtags_max']}")
    if any(not h.startswith("#") or " " in h for h in draft.hashtags):
        report.fail("hashtags must be single #words")
    for i, t in enumerate([*draft.thread, draft.threads_subuh, draft.threads_malam]):
        if count_chars(t) > limits["threads_post_max_chars"] or utf8_len(t) > 2000:
            report.fail(f"threads post {i + 1} is {count_chars(t)} characters; max {limits['threads_post_max_chars']}")
    if not 2 <= len(draft.thread) <= 20:
        report.fail("thread chain needs 2–20 posts")
    if FOLLOW_HANDLE not in draft.next_teaser and FOLLOW_HANDLE not in draft.caption:
        report.fail(f"the follow hook must name {FOLLOW_HANDLE}")
    if not draft.next_teaser.lower().startswith("besok"):
        report.fail("teaser must start with 'Besok'")
    if "sumber" in draft.caption.lower() or "terjemahan kemenag" in draft.caption.lower():
        report.fail("caption must carry no sources list or translation credit")

    # ── Too easy, topic reuse ──
    for entry in [*load_too_easy(), *ctx.extra_too_easy]:
        if entry.matches(draft.key_fact) or entry.matches(draft.topic):
            report.fail(f"too easy: matches '{entry.text}'")
    th = topic_hash(draft.topic)
    last = ctx.topic_history.get(th)
    if last is not None and ctx.today - last < timedelta(days=limits["topic_cooldown_days"]):
        report.fail(f"topic reused within {limits['topic_cooldown_days']} days (last {last})")

    # ── Evergreen: nothing that dates ──
    if ctx.evergreen:
        for ref in refs:
            if ref.type == "fact" and ref.ref.split(":")[0] in DATED_FACTS:
                report.fail(f"evergreen post cites dated fact {ref.ref}")
        if draft.series is not None:
            report.fail("evergreen posts are not part of a series")
        for where, text in copy:
            if any(re_dated.search(f) for f in _forms(text)) and where not in ("teaser", "threads subuh", "threads malam"):
                report.fail(f"{where}: evergreen copy refers to a specific day")

    # ── Launch mode ──
    if ctx.launch_mode is LaunchMode.PRELAUNCH:
        for where, text in copy:
            if any(re_install_cta.search(f) for f in _forms(text)):
                report.fail(f"{where}: install CTA before launch")
    return report


def uses(draft: Draft) -> Iterable[SourceRef]:
    yield from (c.source for c in draft.claims)
    yield from (s.source for s in draft.slides if s.source)
