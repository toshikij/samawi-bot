"""Loads `content/rules.yaml` and `content/too_easy.yaml` and matches text against them."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

import yaml

from .text import normalize, variants


@dataclass(frozen=True)
class Rules:
    banned_hook: tuple[re.Pattern[str], ...]
    banned_cta: tuple[re.Pattern[str], ...]
    competitors: tuple[re.Pattern[str], ...]
    blocked_topics: dict[str, tuple[re.Pattern[str], ...]]
    limits: dict[str, int]


@dataclass(frozen=True)
class TooEasy:
    id: str
    text: str
    all_of: tuple[tuple[str, ...], ...]

    def matches(self, text: str) -> bool:
        norm = f" {normalize(text)} "
        return all(any(f" {normalize(term)} " in norm for term in group) for group in self.all_of)


def _compile(patterns: list[str]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p) for p in patterns)


def _read(name: str) -> dict[str, Any]:
    text = resources.files("samawi_bot.content").joinpath(name).read_text("utf-8")
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError(f"{name} is not a mapping")
    return data


@lru_cache(maxsize=1)
def load_rules() -> Rules:
    raw = _read("rules.yaml")
    competitors = tuple(re.compile(rf"\b{re.escape(normalize(n))}\b") for n in raw["competitor_names"])
    limits = {k: int(v) for k, v in raw.items() if isinstance(v, int)}
    return Rules(
        banned_hook=_compile(raw["banned_hook_patterns"]),
        banned_cta=_compile(raw["banned_cta_phrases"]),
        competitors=competitors,
        blocked_topics={k: _compile(v) for k, v in raw["blocked_topics"].items()},
        limits=limits,
    )


def load_too_easy(extra: list[dict[str, Any]] | None = None) -> list[TooEasy]:
    raw = _read("too_easy.yaml")
    entries = list(raw.get("seed") or []) + list(raw.get("learned") or []) + list(extra or [])
    return [TooEasy(e["id"], e["text"], tuple(tuple(g) for g in e["all_of"])) for e in entries]


def find(patterns: tuple[re.Pattern[str], ...], text: str) -> list[str]:
    hits: list[str] = []
    for form in variants(text):
        for p in patterns:
            m = p.search(form)
            if m and m.group(0) not in hits:
                hits.append(m.group(0))
    return hits


def blocked_topic_hits(rules: Rules, text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for topic, patterns in rules.blocked_topics.items():
        hits = find(patterns, text)
        if hits:
            out[topic] = hits
    return out


def topic_hash(topic: str) -> str:
    """Stable across case, punctuation and word order, so a reworded topic still dedupes."""
    words = sorted(set(normalize(topic).split()) - STOPWORDS)
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:16]


STOPWORDS = frozenset(
    "yang di ke dari dan atau dalam pada untuk dengan ini itu adalah apa kenapa mengapa ternyata satu satunya the of al an".split()  # noqa: SIM905
)


@dataclass
class CheckReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    allowed_numbers: set[str] = field(default_factory=set)
    # Hook candidates that failed a check. They are discarded (never published), not fatal: the
    # draft can still pass with one of the remaining hooks (see verify/gate.decide).
    invalid_hooks: set[int] = field(default_factory=set)

    @property
    def ok(self) -> bool:
        return not self.errors

    def fail(self, msg: str) -> None:
        self.errors.append(msg)
