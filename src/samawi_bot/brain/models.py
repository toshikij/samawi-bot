"""The structured draft the writer produces and everything downstream consumes (spec §6.3)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Pillar(StrEnum):
    LANGIT = "langit_waktu_salat"
    QURAN = "fakta_quran"
    SUNNAH = "sunnah_terlupa"
    DOA = "doa_dzikir"
    MUSAFIR = "musafir"
    HITUNG = "hitung_ibadah"
    KALENDER = "kalender_hijriah"
    SEJARAH = "sejarah_kisah"


class HookFormula(StrEnum):
    TERNYATA = "ternyata"
    SATU_SATUNYA = "satu_satunya"
    NUMBER_SHOCK = "number_shock"
    KENAPA = "kenapa"
    PASTI_PERNAH = "pasti_pernah"
    VERSUS = "versus"
    HIDDEN = "hidden_in_plain_sight"
    SERIES = "series"


class Format(StrEnum):
    CAROUSEL = "carousel"
    REEL = "reel"


SlideTemplate = Literal["cover", "ayat", "fact", "list", "steps", "hadith", "dua", "comparison", "map", "takeaway"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(_Strict):
    """Where a fact comes from. The renderer pulls scripture text from these; the LLM never types it."""

    type: Literal["ayat", "hadith", "dua", "fact"]
    ref: str = Field(description="ayat: '2:282' or '18:1-5'; hadith: HadeethEnc id; dua: dua id; fact: fact key")


class Claim(_Strict):
    text: str = Field(description="The factual claim exactly as the post states it, in Indonesian")
    source: SourceRef


class Slide(_Strict):
    template: SlideTemplate
    heading: str = ""
    body: str = ""
    items: list[str] = Field(default_factory=list)
    source: SourceRef | None = None
    left_label: str = ""
    left_value: str = ""
    right_label: str = ""
    right_value: str = ""


class Series(_Strict):
    id: str
    title: str
    part: int
    total: int


class Draft(_Strict):
    topic: str = Field(description="A short, specific topic label, used for 60-day dedupe")
    key_fact: str = Field(description="The single fact most of the audience did not know")
    pillar: Pillar
    format: Format
    hook_formula: HookFormula
    hooks: list[str] = Field(description="Exactly 5 cover-hook candidates, each ≤ 10 words")
    slides: list[Slide] = Field(description="Slides 2..n-1 after the cover; the cover and CTA slides are added by code")
    takeaway: str = Field(description="One practical thing to do today")
    bridge_feature: str = Field(description="Key of a shipped feature from the list given")
    bridge_line: str = Field(description="One sentence tying the topic to the feature; must say 'tanpa iklan'")
    next_teaser: str = Field(description="'Besok: …' cliffhanger for the next post in the series or plan")
    share_prompt: str = Field(default="", description="'Kirim ke teman yang …' when natural, else empty")
    caption: str
    hashtags: list[str]
    thread: list[str] = Field(description="Threads chain: post 1 is the hook; then one idea per reply")
    threads_subuh: str = Field(description="Short Ba'da Subuh reflection on the ayat of the day for Threads")
    threads_malam: str = Field(description="An evening discussion question for Threads")
    claims: list[Claim]
    series: Series | None = None


class HookScore(_Strict):
    index: int
    curiosity_gap: int
    specificity: int
    payoff_honesty: int
    thumb_stop: int


class ClaimVerdict(_Strict):
    index: int
    verdict: Literal["supported", "unsupported", "overstated"]
    note: str = ""


class JudgeReport(_Strict):
    claims: list[ClaimVerdict]
    contested_framing: bool
    contested_note: str = ""
    hooks: list[HookScore]
    novelty_pct: int = Field(description="% of the audience who would NOT already know the key fact")
    accessibility: int = Field(description="0-10: a 17-year-old fully understands without googling")
    payoff_by_slide_3: bool
    tone_ok: bool
    notes: str = ""


class HookSet(_Strict):
    hooks: list[str] = Field(description="Exactly 5 new cover hooks, each ≤ 10 words, each paid off by slides 2–3")


class HookReport(_Strict):
    hooks: list[HookScore]
    notes: str = ""
