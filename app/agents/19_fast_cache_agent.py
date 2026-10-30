"""Query cache — an adapter over the cache that already serves search.

`SearchCache` in `app/services/search_service.py` is an LRU with a TTL, wired
into the search endpoint: a repeated query is answered from memory without
touching the database. It is 60 seconds, which is the point at which a result
set can reasonably have changed.

This agent exposes that cache under the architecture's name rather than adding a
second one. Two caches would mean two TTLs, two eviction policies, and a stale
result from whichever was checked first.
"""

from __future__ import annotations

from app.services.search_service import CACHE_MAX_ENTRIES, CACHE_TTL_SECONDS, SearchCache

__all__ = ["CACHE_MAX_ENTRIES", "CACHE_TTL_SECONDS", "FastCacheAgent", "SearchCache"]


class FastCacheAgent:
    """The search cache, addressed by name."""

    def __init__(self, cache: SearchCache | None = None) -> None:
        self.cache = cache if cache is not None else SearchCache()

    def get(self, key: tuple) -> object | None:
        return self.cache.get(key)

    def invalidate_urls(self, urls) -> int:
        """Drop cached answers that mention these pages.

        The rule lives here rather than in the service so that the cache's two
        behaviours — serve fast, forget precisely — are defined in one file.
        """
        return self.cache.invalidate_urls(urls)

    def set(self, key: tuple, payload: object) -> None:
        self.cache.set(key, payload)

    def clear(self) -> None:
        self.cache.clear()

    def stats(self) -> dict[str, object]:
        return {
            "ttl_seconds": CACHE_TTL_SECONDS,
            "max_entries": CACHE_MAX_ENTRIES,
            "entries": len(self.cache),
        }
