"""Choosing the sentences worth keeping, the way a search engine does.

A page is stored so that a later question can be answered from it. Keeping its
opening characters keeps the navigation and drops the paragraph that answers the
question — "karnataka scheme status" sits in the third paragraph, and a body cut
to its first 256 characters never contains it, so the card comes back with a
snippet that says nothing.

This keeps sentences instead: the ones that carry a fact, chosen from anywhere
in the page, within a character budget. It is the difference between a page that
can be quoted and a page that can only be linked.
"""

from __future__ import annotations

import re

#: A sentence shorter than this is a menu item or a heading fragment.
MIN_SENTENCE_CHARS = 40
#: Longer than this and it is a paragraph that should have been split.
MAX_SENTENCE_CHARS = 400
#: Where the split happens. A full stop, question or exclamation, followed by
#: space and a capital — the ordinary shape of written prose.
SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
#: Lines that are furniture rather than content.
BOILERPLATE = re.compile(
    r"(?i)(copyright|all rights reserved|privacy policy|terms of use|"
    r"skip to main|screen reader|javascript|browser support|"
    r"follow us|subscribe|newsletter|sign in|log in)"
)
#: A sentence with numbers in it usually carries the fact someone searched for:
#: a rate, a date, a deadline, an amount.
DIGITS = re.compile(r"\d")


def sentences(text: str) -> list[str]:
    """The sentence-shaped pieces of a body, in page order."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    parts = SENTENCE_SPLIT.split(cleaned)
    return [part.strip() for part in parts if part.strip()]


def score(sentence: str, position: int) -> float:
    """How much a sentence is worth keeping.

    Position matters, but only as a tie-breaker: the paragraph that answers a
    question is as often third as first, and preferring the first 256 characters
    is exactly the mistake this replaces.
    """
    length = len(sentence)
    value = 0.0
    if MIN_SENTENCE_CHARS <= length <= MAX_SENTENCE_CHARS:
        value += 2.0
    if DIGITS.search(sentence):
        value += 1.0
    if BOILERPLATE.search(sentence):
        value -= 5.0
    # A gentle decay with position: earlier is a little better, never decisive.
    value += max(0.0, 1.0 - position / 60.0)
    return value


def extract(text: str, budget: int) -> str:
    """The best sentences of `text`, in page order, within `budget` characters.

    Returned in page order rather than by score, because a body that reads
    out of order is unquotable even when every sentence in it is good.
    """
    if budget <= 0:
        return ""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= budget:
        return cleaned

    ranked = sorted(
        ((score(sentence, index), index, sentence) for index, sentence in enumerate(sentences(cleaned))),
        key=lambda item: (-item[0], item[1]),
    )

    chosen: list[tuple[int, str]] = []
    used = 0
    for value, index, sentence in ranked:
        if value <= 0:
            break
        cost = len(sentence) + 1
        if used + cost > budget:
            continue
        chosen.append((index, sentence))
        used += cost
        if used >= budget - MIN_SENTENCE_CHARS:
            break

    if not chosen:
        # Nothing scored: fall back to the opening, which is what the old rule
        # did and is better than an empty body.
        return cleaned[:budget].rstrip()

    chosen.sort()
    return " ".join(sentence for _index, sentence in chosen)
