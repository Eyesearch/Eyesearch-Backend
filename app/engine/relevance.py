"""The universal relevance gate.

One rule, applied everywhere a candidate is judged — index hits and freshly
discovered candidates alike: a page is relevant to a query when enough of the
query's words appear in the fields that *describe* the page (title, URL,
snippet). A word buried in the body does not count, which is what stops a page
that merely mentioned the term once from being served as an answer.

Nothing here knows about any particular query, domain or language. The threshold
is a single number (`RELEVANCE_MIN_TOKEN_COVERAGE`, 0.6 by default) and the
tokeniser handles Latin, Devanagari and Kannada scripts.
"""

from __future__ import annotations

import re
from typing import Iterable

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9\u0900-\u097F\u0C80-\u0CFF]+")

# Words that carry no discriminating power in a query.
STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at", "is",
        "are", "was", "were", "be", "by", "with", "from", "as", "that", "this",
        "it", "its", "into", "about", "how", "what", "when", "where", "who",
        "ಮತ್ತು", "ಒಂದು", "ಇದು", "ಆದರೆ", "के", "का", "की", "है", "में", "और",
    }
)

DEFAULT_MIN_COVERAGE = 0.6


def tokens(query: str, *, drop_stopwords: bool = True) -> list[str]:
    """Distinct, lower-cased words of a query, in order, without stopwords."""
    seen: set[str] = set()
    out: list[str] = []
    for raw in TOKEN_PATTERN.findall(query or ""):
        token = raw.lower()
        if len(token) < 2 and not token.isdigit():
            continue
        if drop_stopwords and token in STOPWORDS:
            continue
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
    return out


def _field_contains(field: str, token: str) -> bool:
    """True when the token appears in the field, tolerating simple stemming."""
    if not field:
        return False
    if token in field:
        return True
    # "bengal" should still match "bengaluru" in a title, and vice versa.
    if len(token) >= 4:
        for word in TOKEN_PATTERN.findall(field):
            word = word.lower()
            if word.startswith(token) or (len(word) >= 4 and token.startswith(word)):
                return True
    return False


def coverage(query: str, *, title: str = "", url: str = "", snippet: str = "") -> float:
    """Fraction of the query's words present in title/URL/snippet, 0.0–1.0."""
    wanted = tokens(query)
    if not wanted:
        return 0.0

    haystacks = ((title or "").lower(), (url or "").lower(), (snippet or "").lower())
    matched = sum(
        1
        for token in wanted
        if any(_field_contains(field, token) for field in haystacks)
    )
    return matched / len(wanted)


def is_relevant(
    query: str,
    *,
    title: str = "",
    url: str = "",
    snippet: str = "",
    min_coverage: float = DEFAULT_MIN_COVERAGE,
) -> bool:
    """The gate itself: is this candidate about the query at all?"""
    wanted = tokens(query)
    if not wanted:
        return False
    return coverage(query, title=title, url=url, snippet=snippet) >= min_coverage


def filter_candidates(query: str, candidates: Iterable[dict], min_coverage: float = DEFAULT_MIN_COVERAGE) -> list[dict]:
    """Drop candidates that fail the gate, preserving the incoming order."""
    kept: list[dict] = []
    for candidate in candidates:
        if is_relevant(
            query,
            title=str(candidate.get("title") or ""),
            url=str(candidate.get("url") or ""),
            snippet=str(candidate.get("snippet") or ""),
            min_coverage=min_coverage,
        ):
            kept.append(candidate)
    return kept


__all__ = ["tokens", "coverage", "is_relevant", "filter_candidates", "DEFAULT_MIN_COVERAGE"]
