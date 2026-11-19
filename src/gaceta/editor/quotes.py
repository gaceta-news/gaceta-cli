"""Verbatim-quote gate for story cards (roadmap 11b, from The Nostr Observer).

A quoted span in a card (title, summary, why_it_matters) claims to be somebody's exact
words. If it is not found in the text of the story's own items, the quotation marks are
removed: the sentence survives as paraphrase, but it no longer pretends to be a quote.
Short spans (< MIN_WORDS words: scare quotes, names, titles) are left alone.
"""
from __future__ import annotations

import re
import unicodedata

MIN_WORDS = 4
# opening -> closing mark
_PAIRS = {"\u201c": "\u201d", "\u00ab": "\u00bb", '"': '"', "\u2018": "\u2019"}
_SPAN = re.compile("|".join(
    re.escape(o) + r"([^" + re.escape(o + c) + r"]{3,600}?)" + re.escape(c) for o, c in _PAIRS.items()
))


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "").casefold()
    t = re.sub(r"[\u2018\u2019\u201a\u201b'`\u00b4]", "'", t)
    t = re.sub(r"[\u201c\u201d\u201e\u00ab\u00bb\"]", '"', t)
    t = t.replace("\u2026", "...")
    t = re.sub(r"[^\w'\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _found(span: str, corpus: str) -> bool:
    """Every fragment (split on an ellipsis) appears, in order, in the corpus."""
    pos = 0
    for frag in re.split(r"\.\.\.|\u2026|\[\u2026\]", span):
        f = _norm(frag)
        if not f:
            continue
        i = corpus.find(f, pos)
        if i < 0:
            return False
        pos = i + len(f)
    return True


def enforce(text: str | None, sources: list[str]) -> tuple[str | None, int]:
    """Return (text with unverifiable quotes unquoted, number of quotes removed)."""
    if not text:
        return text, 0
    corpus = _norm(" \n ".join(s for s in sources if s))
    removed = 0

    def fix(m: re.Match) -> str:
        nonlocal removed
        inner = next(g for g in m.groups() if g is not None)
        if len(inner.split()) < MIN_WORDS or _found(inner, corpus):
            return m.group(0)
        removed += 1
        return inner

    return _SPAN.sub(fix, text), removed


def enforce_card(card: dict, items: list[dict]) -> int:
    """Apply the gate to a parsed card in place; returns quotes removed."""
    sources = [f"{it.get('title') or ''} {it.get('body') or ''}" for it in items]
    total = 0
    for key in ("title", "summary", "why_it_matters"):
        card[key], n = enforce(card.get(key), sources)
        total += n
    return total
