"""Blended ranking for local hits.

SQLite's `bm25()` returns a negative score where more negative is better, so it
is flipped to "higher is better" before blending. Freshness decays exponentially
with the document's age, and a small host-trust term breaks ties in favour of
official and regional sources.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

TRUSTED_SUFFIXES = (".gov.in", ".nic.in", ".ac.in", ".edu.in", ".org.in")
PREFERRED_SUFFIXES = (".in", ".co.in", ".org", ".news")


@dataclass(slots=True)
class RankedDocument:
    url: str
    title: str
    snippet: str
    language: str
    updated_at: str | None
    bm25: float
    favicon_url: str | None = None
    sections: list[dict[str, str]] = field(default_factory=list)
    score: float = 0.0


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[: len(fmt) + 2].strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def freshness(updated_at: str | None, *, half_life_days: float = 30.0) -> float:
    """1.0 for a document updated now, decaying by half every `half_life_days`."""
    stamp = _parse_timestamp(updated_at)
    if stamp is None:
        return 0.5
    age_days = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400)
    return math.pow(0.5, age_days / max(1.0, half_life_days))


def host_trust(url: str) -> float:
    host = url.split("/")[2] if "://" in url and len(url.split("/")) > 2 else ""
    host = host.removeprefix("www.")
    if host.endswith(TRUSTED_SUFFIXES):
        return 1.0
    if host.endswith(PREFERRED_SUFFIXES):
        return 0.75
    return 0.5


def rank_documents(documents: list[RankedDocument], *, freshness_weight: float = 0.25) -> list[RankedDocument]:
    """Blend BM25, freshness and host trust, then sort best-first."""
    if not documents:
        return []

    bm25_scores = [max(0.0, -doc.bm25) for doc in documents]
    top = max(bm25_scores) or 1.0

    for doc, raw in zip(documents, bm25_scores):
        relevance = raw / top
        doc.score = round(
            (1.0 - freshness_weight) * relevance
            + freshness_weight * freshness(doc.updated_at)
            + 0.05 * host_trust(doc.url),
            4,
        )

    return sorted(documents, key=lambda item: item.score, reverse=True)


__all__ = ["RankedDocument", "rank_documents", "freshness", "host_trust"]
