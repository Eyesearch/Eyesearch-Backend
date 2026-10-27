"""Agent 1 — the frontier: what to crawl next, and how often per domain.

Wraps the persistent queues (`crawl_backlog` for discovered domains, the site
table for coverage) and applies the domain rules: authoritative Indian suffixes
first, at most `max_sitelinks_per_domain` per host.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import FrontierConfig
from .sitemap import (
    MAX_SITEMAPS_PER_HOST,
    MAX_URLS_PER_HOST,
    STANDARD_SITEMAP_PATHS,
    harvest_urls,
    is_content_url,
    iter_sitemap_index,
    looks_like_sitemap,
    sitemap_urls_from_robots,
)

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

        if not hosts:
            # nothing in the domain backlog: fall back to the URL queue, which is
            # where sitemap harvesting puts its work
            try:
                hosts = await self.store.pending_queue_hosts(size)
            except Exception as exc:
                logger.warning("frontier queue read failed: %s", exc)
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
            # enqueue_domain owns the re-arm/cooldown decision. Check its result
            # before mirroring so an existing site can become eligible again.
            queued = await self.store.enqueue_domain(key, reason=reason)
            if not queued:
                return False
            await self.store.queue_url(f"https://{key}/", domain=key, reason=reason)
            return True
        except Exception as exc:
            logger.warning("frontier enqueue failed for %s: %s", host, exc)
            return False

    async def mark_done(self, host: str, *, status: str = "processed") -> None:
        try:
            await self.store.mark_domain_processed(host, status=status)
            await self.store.close_queue_url(f"https://{(host or '').lower().removeprefix('www.')}/")
        except Exception as exc:
            logger.warning("frontier mark failed for %s: %s", host, exc)

    async def harvest(self, host: str, *, fetcher: Any | None = None) -> list[str]:
        """Content URLs published by a domain's robots.txt or sitemap.

        Never raises: a missing, blocked or malformed sitemap simply yields an
        empty list and the caller falls back to sitelink discovery.
        """
        host = (host or "").lower().removeprefix("www.").strip()
        if not host:
            return []

        agent = fetcher
        owned = False
        if agent is None:
            from app.agents import FetcherAgent

            agent = FetcherAgent()
            owned = True

        base = f"https://{host}/"
        try:
            robots = await agent.fetch(base + "robots.txt")
            sitemaps: list[str] = []
            if robots.ok and robots.html:
                sitemaps = sitemap_urls_from_robots(robots.html, base)

            documents: list[tuple[str, str]] = []
            queue: list[str] = sitemaps[:MAX_SITEMAPS_PER_HOST] or [
                base + path.lstrip("/") for path in STANDARD_SITEMAP_PATHS
            ]

            seen_sitemaps: set[str] = set()
            budget = MAX_SITEMAPS_PER_HOST
            while queue and budget > 0:
                url = queue.pop(0)
                if url in seen_sitemaps:
                    continue
                seen_sitemaps.add(url)
                budget -= 1

                payload: bytes | str | None = None
                if hasattr(agent, "fetch_raw"):
                    raw, status = await agent.fetch_raw(url)
                    if raw and status and status < 400:
                        payload = raw
                if payload is None:
                    fetched = await agent.fetch(url)
                    if not fetched.ok or not fetched.html:
                        continue
                    payload = fetched.html
                if not looks_like_sitemap(payload):
                    continue

                documents.append((url, payload))
                # a sitemap index points at more sitemaps, one level deep
                for child in iter_sitemap_index(payload, url=url):
                    if child not in seen_sitemaps and len(queue) < MAX_SITEMAPS_PER_HOST:
                        queue.append(child)

            urls = harvest_urls(documents, host=host, limit=MAX_URLS_PER_HOST)
            if urls:
                logger.info("harvested %d sitemap urls from %s", len(urls), host)
            return urls
        except Exception as exc:
            logger.debug("sitemap harvest failed for %s: %s", host, exc)
            return []
        finally:
            if owned:
                await agent.aclose()

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
