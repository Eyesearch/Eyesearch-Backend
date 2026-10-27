"""Agent 01 — the frontier.

Owns `crawl_queue`: what is waiting to be crawled, in what order, and which
domains are worth spending a request on first.

Queue order is the primary key (`rowid`), exactly as the pipeline reads it, so
priority has to be applied when the URL is enqueued rather than when it is
dequeued. Official Indian public domains therefore go in ahead of everything
else, and the dequeue query stays a plain `ORDER BY rowid`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from app.engine.config import Settings, get_settings
from app.engine.store import IndexStore, QueuedUrl

logger = logging.getLogger(__name__)

# Authoritative Indian public suffixes — crawled first, every time.
PRIORITY_SUFFIXES: tuple[str, ...] = (
    ".gov.in",
    ".nic.in",
    ".ac.in",
    ".edu.in",
    ".res.in",
    ".org.in",
    ".co.in",
)

# Domains the crawler starts from when the queue is empty.
SEED_URLS: tuple[str, ...] = (
    "https://www.india.gov.in/",
    "https://www.karnataka.gov.in/",
    "https://bengaluruurban.nic.in/",
    "https://www.rbi.org.in/",
    "https://www.irctc.co.in/",
    "https://www.iisc.ac.in/",
    "https://www.ugc.gov.in/",
    "https://en.wikipedia.org/wiki/Karnataka",
)


@dataclass(frozen=True)
class FrontierItem:
    """A URL the frontier accepted, with the reason it ranks where it does."""

    url: str
    domain: str
    priority: int


def domain_of(url: str) -> str:
    """The registrable host of a URL, lower-cased and without `www.`."""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def priority_of(url: str) -> int:
    """Lower sorts first: official public domains, then everything else."""
    host = domain_of(url)
    return 0 if any(host.endswith(suffix) for suffix in PRIORITY_SUFFIXES) else 1


class FrontierAgent:
    """Reads and writes the crawl frontier."""

    def __init__(self, store: IndexStore, settings: Settings | None = None) -> None:
        self._store = store
        self._settings = settings or get_settings()

    async def enqueue(self, urls: list[str] | tuple[str, ...] | set[str]) -> int:
        """Queue URLs, priority first, skipping anything already known.

        Insert order is what makes priority real, so the list is sorted before
        it reaches the store rather than relying on the dequeue query.
        """
        candidates: list[FrontierItem] = []
        seen: set[str] = set()
        for url in urls:
            cleaned = (url or "").strip()
            if not cleaned.startswith(("http://", "https://")):
                continue
            if cleaned in seen:
                continue
            domain = domain_of(cleaned)
            if not domain:
                continue
            seen.add(cleaned)
            candidates.append(FrontierItem(cleaned, domain, priority_of(cleaned)))

        if not candidates:
            return 0

        candidates.sort(key=lambda item: (item.priority, len(item.url), item.url))
        known = await self._store.known_urls([item.url for item in candidates])
        fresh = [item for item in candidates if item.url not in known]
        if not fresh:
            return 0

        added = await self._store.enqueue_urls([(item.url, item.domain) for item in fresh])
        if added:
            logger.info("frontier: queued %d new URLs (depth now %d)", added, await self.depth())
        return added

    async def next_batch(self, limit: int | None = None) -> list[QueuedUrl]:
        """The next batch of pending URLs, in the order the plan specifies."""
        size = limit or self._settings.crawl_batch_size
        return await self._store.pending_batch(size)

    async def depth(self) -> int:
        return await self._store.queue_depth()

    async def refill(self, minimum: int = 1) -> int:
        """Top the frontier back up from the seed list when it runs dry."""
        if await self.depth() >= minimum:
            return 0
        return await self.enqueue(SEED_URLS)


__all__ = [
    "FrontierAgent",
    "FrontierItem",
    "PRIORITY_SUFFIXES",
    "SEED_URLS",
    "domain_of",
    "priority_of",
]
