"""Agent 1 — the frontier: what to crawl next, and how often per domain.

Wraps the persistent queues (`crawl_backlog` for discovered domains, the site
table for coverage) and applies the domain rules: authoritative Indian suffixes
first, at most `max_sitelinks_per_domain` per host.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import FrontierConfig

logger = logging.getLogger(__name__)


class FrontierAgent:
    """Async entry point: `next_batch()`, `enqueue()`, `coverage()`."""

    name = "frontier"

    def __init__(self, store: Any | None = None, config: FrontierConfig | None = None) -> None:
        self._store = store
        self.config = config or FrontierConfig()

    @property
    def store(self) -> Any:
        if self._store is None:
            from app.engine.store import get_store

            self._store = get_store()
        return self._store

    async def next_batch(self, limit: int | None = None) -> list[str]:
        """The next domains to crawl, authoritative hosts first."""
        size = limit or self.config.batch_size
        try:
            hosts = await self.store.pending_domains(size)
        except Exception as exc:  # the frontier must never take the pipeline down
            logger.warning("frontier read failed: %s", exc)
            return []
        authoritative = [h for h in hosts if self.config.is_authoritative(h)]
        rest = [h for h in hosts if h not in authoritative and not self.config.is_excluded(h)]
        return (authoritative + rest)[:size]

    async def enqueue(self, host: str, *, reason: str = "discovered") -> bool:
        """Queue one domain, applying the authority and exclusion rules.

        The domain lands in `crawl_backlog` (kind='domain') for the pipeline and
        is mirrored into `crawl_queue`, which is the table an operator watches.
        """
        if self.config.is_excluded(host):
            return False
        key = (host or "").lower().removeprefix("www.").strip()
        if not key:
            return False
        try:
            if await self.store.site_exists(key):
                return False
            queued = await self.store.enqueue_domain(key, reason=reason)
            await self.store.queue_url(f"https://{key}/", domain=key, reason=reason)
            return bool(queued)
        except Exception as exc:
            logger.warning("frontier enqueue failed for %s: %s", host, exc)
            return False

    async def mark_done(self, host: str, *, status: str = "processed") -> None:
        try:
            await self.store.mark_domain_processed(host, status=status)
            await self.store.close_queue_url(f"https://{(host or '').lower().removeprefix('www.')}/")
        except Exception as exc:
            logger.warning("frontier mark failed for %s: %s", host, exc)

    async def queue_depth(self) -> int:
        """How many URLs sit in `crawl_queue`, i.e. how full the buffer is."""
        try:
            return await self.store.queue_depth()
        except Exception:
            return 0

    async def pending(self) -> int:
        try:
            return await self.store.pending_domain_count()
        except Exception:
            return 0

    async def coverage(self) -> dict[str, Any]:
        """How much of the seed registry the index already holds."""
        from app.indexer import crawler as seed_crawler

        try:
            return await seed_crawler.coverage(self.store)
        except Exception as exc:
            logger.warning("frontier coverage failed: %s", exc)
            return {"sufficient": False, "uncovered": [], "sites": 0}


__all__ = ["FrontierAgent"]
