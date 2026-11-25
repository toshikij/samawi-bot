"""The writer must never type Arabic: scripture comes only from sources (spec §6.1, §6.5)."""

from __future__ import annotations

import pytest

from conftest import load_draft
from samawi_bot.checks.draft_checks import CheckContext, check_draft
from samawi_bot.sources import Sources


@pytest.mark.parametrize(
    "text",
    ["Bismillah: بِسْمِ ٱللَّهِ", "ﷺ", "Allah ﷻ", "قُلْ", "ٱلْحَمْدُ"],
)
def test_arabic_script_in_copy_is_rejected(sources: Sources, ctx: CheckContext, text: str) -> None:
    report = check_draft(load_draft(caption=f"Pembuka. {text}"), sources, ctx)
    assert "caption: contains Arabic script; reference the source instead of typing it" in report.errors


def test_transliteration_is_fine(sources: Sources, ctx: CheckContext) -> None:
    report = check_draft(load_draft(caption="Bismillahirrahmanirrahim, Al-Baqarah ayat 282."), sources, ctx)
    assert not any("Arabic script" in e for e in report.errors)


def test_the_posts_own_date_is_an_allowed_number(sources: Sources, ctx: CheckContext) -> None:
    caption = f"Kamis, {ctx.today.day} November {ctx.today.year}: ayat terpanjang."
    report = check_draft(load_draft(caption=caption), sources, ctx)
    assert not any("number" in e for e in report.errors)


def test_a_byte_order_mark_is_not_arabic(sources: Sources, ctx: CheckContext) -> None:
    report = check_draft(load_draft(caption="﻿Ayat terpanjang."), sources, ctx)
    assert not any("Arabic script" in e for e in report.errors)
