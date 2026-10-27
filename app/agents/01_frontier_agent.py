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
from urllib.parse import parse_qsl, urlparse, urlunparse

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
    ".in",
)

#: Indian portals that are not on a public suffix but are worth crawling.
#: News, legal reporting, startup and developer media, jobs and open data.
WHITELISTED_DOMAINS: tuple[str, ...] = (
    "thehindu.com",
    "indianexpress.com",
    "prajavani.net",
    "vijaykarnataka.com",
    "indiankanoon.org",
    "livelaw.in",
    "barandbench.com",
    "inc42.com",
    "hasgeek.com",
    "data.gov.in",
    "freejobalert.com",
    "ncs.gov.in",
)

#: Extensions that are never a page to index.
JUNK_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif", ".tiff",
        ".css", ".js", ".mjs", ".json", ".xml", ".rss", ".atom", ".txt",
        ".zip", ".gz", ".tar", ".rar", ".7z", ".bz2", ".xz",
        ".mp3", ".mp4", ".avi", ".mov", ".wmv", ".flv", ".webm", ".mkv",
        ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
        ".woff", ".woff2", ".ttf", ".otf", ".eot",
        ".exe", ".dmg", ".apk", ".msi", ".deb", ".rpm", ".iso",
        ".map", ".manifest",
    }
)

#: Paths that are a form or a session, not a page.
BLOCKED_PATH_MARKERS: tuple[str, ...] = (
    "/login", "/signin", "/sign-in", "/signup", "/sign-up", "/logout", "/signout",
    "/cart", "/checkout", "/basket", "/account", "/wp-admin", "/wp-login",
    "/search", "/print", "/share", "/rss", "/feed",
)

#: Query keys that identify a campaign, never a different page.
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gclid", "fbclid", "mc_cid", "mc_eid", "ref", "referrer", "source",
        "campaign", "spm", "share", "fbclid", "igshid", "s_kwcid",
    }
)

#: How deep below a host root the crawler follows links. Indian portals nest
#: real content several levels down (`/en/departments/health/services`), so a
#: limit of 2 threw away valid pages; 6 keeps the useful depth and still
#: bounds the crawl.
MAX_PATH_DEPTH = 6

#: Stop queueing new links once this many are waiting, so discovery cannot
#: grow the frontier faster than the workers drain it.
MAX_PENDING_QUEUE = 10000

# Domains the crawler starts from when the queue is empty.
SEED_URLS: tuple[str, ...] = (
    "https://www.india.gov.in/",
    "https://www.karnataka.gov.in/",
    "https://bengaluruurban.nic.in/",
    "https://www.rbi.org.in/",
    "https://www.irctc.co.in/",
    "https://www.iisc.ac.in/",
    "https://www.ugc.gov.in/",
    "https://www.isro.gov.in/",
    "https://dst.gov.in/",
    "https://www.digitalindia.gov.in/",
    "https://data.gov.in/",
    "https://www.meity.gov.in/",
    "https://www.mohfw.gov.in/",
    "https://agricoop.nic.in/",
    "https://www.ncs.gov.in/",
    "https://www.thehindu.com/news/",
    "https://www.prajavani.net/",
    "https://indiankanoon.org/",
    "https://inc42.com/",
    "https://hasgeek.com/",
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


def normalise_url(url: str) -> str | None:
    """One canonical form per page, or `None` when it is not a page.

    Lower-case host, no `www.`, no fragment, no trailing slash, sorted query
    keys, tracking parameters removed. Two links that address the same page
    therefore collapse to one queue row, which is what stops the frontier
    filling with `?utm_source=` variants of the same URL.
    """
    if not url:
        return None

    parts = urlparse(url.strip())
    if parts.scheme not in ("http", "https"):
        return None

    host = (parts.hostname or "").lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    query = "&".join(
        f"{key}={value}"
        for key, value in sorted(parse_qsl(parts.query, keep_blank_values=True))
        if key.lower() not in TRACKING_PARAMS
    )

    port = f":{parts.port}" if parts.port and parts.port not in (80, 443) else ""
    return urlunparse((parts.scheme, f"{host}{port}", path, "", query, ""))


def is_crawlable(url: str) -> bool:
    """Crawl-scope rules: depth, extension, and form/session paths."""
    parts = urlparse(url)
    path = (parts.path or "/").lower()

    if path.endswith(tuple(JUNK_EXTENSIONS)):
        return False

    depth = len([segment for segment in path.split("/") if segment])
    if depth > MAX_PATH_DEPTH:
        return False

    if any(marker in path for marker in BLOCKED_PATH_MARKERS):
        return False

    # A single unbroken key longer than a URL segment is a session id.
    if any(len(segment) > 80 for segment in path.split("/")):
        return False

    return True


def in_crawl_scope(url: str, source_host: str) -> bool:
    """Same host, or an authoritative Indian public domain."""
    host = domain_of(url)
    if not host:
        return False
    if host == source_host:
        return True
    return is_authoritative(host)


def is_authoritative(host: str) -> bool:
    """An Indian public suffix, or one of the whitelisted portals."""
    if any(host.endswith(suffix) for suffix in PRIORITY_SUFFIXES):
        return True
    return any(host == domain or host.endswith(f".{domain}") for domain in WHITELISTED_DOMAINS)


#: Domains the frontier must never let go quiet: every seed root plus the
#: whitelisted portals.
AUTHORITATIVE_DOMAINS: tuple[str, ...] = tuple(
    dict.fromkeys([domain_of(url) for url in SEED_URLS] + list(WHITELISTED_DOMAINS))
)


def priority_of(url: str) -> int:
    """Lower sorts first: official public domains, then everything else."""
    return 0 if is_authoritative(domain_of(url)) else 1


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

        pending = await self.depth()
        if pending >= MAX_PENDING_QUEUE:
            logger.debug(
                "frontier full (%d pending >= %d), dropping %d new URLs",
                pending,
                MAX_PENDING_QUEUE,
                len(candidates),
            )
            return 0

        # One call must not overshoot the cap: trim to the room that is left.
        capacity = MAX_PENDING_QUEUE - pending
        if len(candidates) > capacity:
            logger.debug(
                "frontier near capacity (%d left), keeping %d of %d URLs",
                capacity,
                capacity,
                len(candidates),
            )
            candidates = candidates[:capacity]

        candidates.sort(key=lambda item: (item.priority, len(item.url), item.url))
        known = await self._store.known_urls([item.url for item in candidates])
        fresh = [item for item in candidates if item.url not in known]
        if not fresh:
            return 0

        added = await self._store.enqueue_urls([(item.url, item.domain) for item in fresh])
        if added:
            logger.info("frontier: queued %d new URLs (depth now %d)", added, await self.depth())
        return added

    def filter_links(self, links: list[str] | tuple[str, ...], source_url: str) -> list[str]:
        """The subset of a page's links that belongs in the frontier.

        Pure and synchronous: no database, no network. The orchestrator calls
        this per page and enqueues the aggregate once per batch.
        """
        source_host = domain_of(source_url)
        accepted: list[str] = []
        seen: set[str] = set()

        for link in links:
            normalised = normalise_url(link)
            if not normalised or normalised in seen:
                continue
            if normalised == normalise_url(source_url):
                continue
            if not is_crawlable(normalised):
                continue
            if not in_crawl_scope(normalised, source_host):
                continue
            seen.add(normalised)
            accepted.append(normalised)

        return accepted

    async def discover(self, links: list[str] | tuple[str, ...], source_url: str) -> int:
        """Filter a page's links and queue the survivors."""
        candidates = self.filter_links(links, source_url)
        if not candidates:
            return 0
        return await self.enqueue(candidates)

    async def next_batch(self, limit: int | None = None) -> list[QueuedUrl]:
        """The next batch of pending URLs, in the order the plan specifies."""
        size = limit or self._settings.crawl_batch_size
        return await self._store.pending_batch(size)

    async def depth(self) -> int:
        return await self._store.queue_depth()

    async def refill(self, minimum: int = 1) -> int:
        """Keep the frontier alive, and keep it diverse.

        Three stages, in order:

        1. queue any seed that has not been seen yet;
        2. if every seed is already indexed, return the stalest crawled URLs to
           `pending` once their cooldown has elapsed;
        3. if the queue has work but none of it comes from an authoritative
           domain, re-arm the stalest crawled authoritative URLs.

        Stage 2 is what stops the stall: with only stage 1, an exhausted seed
        list means `enqueue()` rejects every URL as already known, the queue
        stays empty, and the worker idles for good. Stage 3 is what stops the
        frontier drifting into nothing but one niche domain.

        Every stage respects the cooldown — re-enqueueing a seed that is
        already `processed` does nothing, because `enqueue()` rejects known
        URLs, so "force" has to mean "re-arm", not "insert again".
        """
        if await self.depth() >= minimum:
            if await self.has_authoritative_pending():
                return 0
            return await self._rearm_authoritative()

        added = await self.enqueue(SEED_URLS)
        if added:
            return added

        rearmed = await self._store.rearm_processed(
            limit=self._settings.rearm_batch_size,
            cooldown_seconds=self._settings.rearm_cooldown_seconds,
        )
        if rearmed:
            logger.info(
                "frontier re-armed: %d previously crawled URLs returned to pending "
                "after a %ds cooldown",
                rearmed,
                self._settings.rearm_cooldown_seconds,
            )
        return rearmed

    async def has_authoritative_pending(self) -> bool:
        """Is any authoritative domain waiting to be crawled?"""
        domains = await self._store.pending_domains()
        return any(is_authoritative(domain) for domain in domains)

    async def _rearm_authoritative(self) -> int:
        rearmed = await self._store.rearm_processed(
            limit=self._settings.rearm_batch_size,
            cooldown_seconds=self._settings.rearm_cooldown_seconds,
            domains=AUTHORITATIVE_DOMAINS,
        )
        if rearmed:
            logger.info(
                "frontier re-armed %d authoritative URLs (no seed or whitelisted "
                "domain had work waiting)",
                rearmed,
            )
        return rearmed


__all__ = [
    "AUTHORITATIVE_DOMAINS",
    "BLOCKED_PATH_MARKERS",
    "FrontierAgent",
    "FrontierItem",
    "JUNK_EXTENSIONS",
    "MAX_PATH_DEPTH",
    "MAX_PENDING_QUEUE",
    "PRIORITY_SUFFIXES",
    "SEED_URLS",
    "TRACKING_PARAMS",
    "WHITELISTED_DOMAINS",
    "domain_of",
    "in_crawl_scope",
    "is_authoritative",
    "is_crawlable",
    "normalise_url",
    "priority_of",
]
