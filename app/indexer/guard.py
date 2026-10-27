"""Storage safety: stop writing before the database bill does.

`StorageGuard` is the only thing in the process allowed to answer "how many
pages do we hold". Two rules make it safe:

* **A 60-second in-memory TTL cache.** The crawl pipeline asks once per batch,
  never once per URL. `COUNT(*)` on every fetch is the difference between a
  free tier that holds and one that rate-limits itself into a stall.
* **Fail-closed.** If the count cannot be read — the database is down, the
  token expired, the response was malformed — the guard reports `paused=True`
  and blocks writes. Crawling blind past a ceiling is how a 300,000-page budget
  becomes a bill; refusing to write until the count is known costs nothing but
  a delay.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.libsql_http import LibsqlClient, LibsqlError

logger = logging.getLogger(__name__)

STORAGE_WARNING = (
    "WARNING: Storage safety threshold reached (300k+ records). "
    "Pausing autonomous crawling."
)


@dataclass(frozen=True)
class StorageState:
    """What the guard knows right now, and whether writes are allowed."""

    pages: int
    sites: int
    limit: int
    paused: bool
    reason: str = ""
    from_cache: bool = False

    @property
    def over_limit(self) -> bool:
        return self.pages >= self.limit

    def as_dict(self) -> dict[str, object]:
        return {
            "pages": self.pages,
            "sites": self.sites,
            "limit": self.limit,
            "over_limit": self.over_limit,
            "paused": self.paused,
            "reason": self.reason,
        }


class StorageGuard:
    """Counts pages and sites at most once per TTL, and fails closed."""

    def __init__(
        self,
        client: LibsqlClient,
        *,
        limit: int = 300_000,
        ttl_seconds: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        self._client = client
        self._limit = limit
        self._ttl = ttl_seconds
        self._clock = clock
        self._lock = asyncio.Lock()
        self._cached: StorageState | None = None
        self._cached_at = 0.0
        self._queries = 0

    @property
    def limit(self) -> int:
        return self._limit

    @property
    def count_queries(self) -> int:
        """How many times the database was actually asked. Tests assert on this."""
        return self._queries

    def invalidate(self) -> None:
        """Force the next call to read the database again."""
        self._cached = None
        self._cached_at = 0.0

    def note_write(self, pages: int) -> None:
        """Add pages we just committed to the cached count.

        The pipeline knows exactly how many rows it wrote, so it can keep the
        cache honest between refreshes without spending a query.
        """
        if self._cached is None or pages <= 0:
            return
        self._cached = StorageState(
            pages=self._cached.pages + pages,
            sites=self._cached.sites,
            limit=self._cached.limit,
            paused=self._cached.paused,
            reason=self._cached.reason,
            from_cache=True,
        )

    async def storage_state(self) -> StorageState:
        """The current state, from cache when fresh, fail-closed when not."""
        async with self._lock:
            now = self._clock()
            if self._cached is not None and (now - self._cached_at) < self._ttl:
                return StorageState(
                    pages=self._cached.pages,
                    sites=self._cached.sites,
                    limit=self._cached.limit,
                    paused=self._cached.paused,
                    reason=self._cached.reason,
                    from_cache=True,
                )

            try:
                results = await self._client.batch(
                    [
                        ("SELECT COUNT(*) FROM pages", ()),
                        ("SELECT COUNT(*) FROM sites", ()),
                    ]
                )
                self._queries += 1
                pages = int(results[0].scalar(0) or 0)
                sites = int(results[1].scalar(0) or 0)
            except (LibsqlError, ValueError, TypeError, IndexError) as error:
                # Fail closed: an unknown page count is not permission to write.
                state = StorageState(
                    pages=self._limit,
                    sites=0,
                    limit=self._limit,
                    paused=True,
                    reason=f"storage_state unavailable: {error}",
                    from_cache=False,
                )
                self._cached = state
                self._cached_at = now
                logger.error("storage guard failed closed: %s", error)
                return state

            over = pages >= self._limit
            state = StorageState(
                pages=pages,
                sites=sites,
                limit=self._limit,
                paused=over,
                reason=STORAGE_WARNING if over else "",
                from_cache=False,
            )
            self._cached = state
            self._cached_at = now
            if over:
                logger.warning(STORAGE_WARNING)
            return state

    async def can_write(self) -> bool:
        state = await self.storage_state()
        return not state.paused

    async def assert_can_write(self) -> StorageState:
        """Raise when writes are blocked, so a caller cannot ignore it."""
        state = await self.storage_state()
        if state.paused:
            raise StoragePaused(state.reason or "storage paused")
        return state


class StoragePaused(RuntimeError):
    """Writes are blocked because the index is at its ceiling or unreadable."""


__all__ = ["STORAGE_WARNING", "StorageGuard", "StoragePaused", "StorageState"]
