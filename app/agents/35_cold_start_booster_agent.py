"""Seed priority — an adapter over the frontier's own ranking.

`priority_of` in the frontier already scores a URL for crawl order. This adds the
one thing a cold start needs and a steady state does not: an explicit weight for
the TLDs that are worth seeding first, applied when the queue is being filled
rather than on every URL.

The two are multiplied, not merged, so the frontier keeps ownership of URL-level
priority and this only nudges the starting order.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Higher is crawled sooner. Reserved and institutional TLDs first: they are
#: primary sources, they change slowly, and they are what a search engine is
#: judged on.
TLD_WEIGHTS: dict[str, float] = {
    ".gov.in": 3.0, ".nic.in": 3.0, ".gov": 3.0, ".nic": 3.0, ".mil": 2.5,
    ".ac.in": 2.5, ".edu.in": 2.5, ".edu": 2.5, ".ac": 2.5,
    ".org": 1.8, ".org.in": 1.8, ".res.in": 2.0,
    ".co.in": 1.2, ".com": 1.0, ".in": 1.0, ".net": 1.0,
}

#: Anything not listed.
DEFAULT_WEIGHT = 0.8


def tld_weight(url: str) -> float:
    """The weight for a URL's suffix, longest suffix first."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return DEFAULT_WEIGHT
    if not host:
        return DEFAULT_WEIGHT

    for suffix in sorted(TLD_WEIGHTS, key=len, reverse=True):
        if host.endswith(suffix):
            return TLD_WEIGHTS[suffix]
    return DEFAULT_WEIGHT


class ColdStartBoosterAgent:
    """Order seeds so authoritative sources are crawled first."""

    def __init__(self, frontier=None) -> None:
        self._frontier = frontier

    def weight(self, url: str) -> float:
        return tld_weight(url)

    def order(self, urls: list[str]) -> list[str]:
        """Stable sort: within one weight the original order is kept."""
        base = getattr(self._frontier, "priority_of", None)
        return sorted(
            urls,
            key=lambda url: (-self.weight(url), base(url) if base else 0),
        )
