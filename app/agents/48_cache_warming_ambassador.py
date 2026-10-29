"""Keep federated answers for a little while.

A federated lookup costs a network round trip and someone else's capacity. The
same query arriving again a minute later should not repeat it. A short TTL is
enough — a definition does not change between two searches.

It wraps Agent 19's cache rather than adding a second one: two caches mean two
TTLs and a stale answer from whichever is checked first.
"""

from __future__ import annotations

import logging

from app.agents import load_agent

logger = logging.getLogger(__name__)

FastCacheAgent = load_agent("19_fast_cache_agent").FastCacheAgent

#: Long enough to matter, short enough that a changed page is noticed.
DEFAULT_TTL_SECONDS = 900.0


class CacheWarmingAmbassadorAgent:
    """Cache external results under their own namespace."""

    def __init__(self, cache=None, *, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self.cache = cache if cache is not None else FastCacheAgent()
        self.ttl_seconds = float(ttl_seconds)

    @staticmethod
    def key(query: str, source: str = "") -> tuple:
        # Namespaced so an external answer can never be mistaken for an indexed one.
        return ("federated", source, (query or "").strip().lower())

    def get(self, query: str, source: str = ""):
        return self.cache.get(self.key(query, source))

    def set(self, query: str, payload, source: str = "") -> None:
        self.cache.set(self.key(query, source), payload)

    def stats(self) -> dict[str, object]:
        return {"ttl_seconds": self.ttl_seconds, **self.cache.stats()}
