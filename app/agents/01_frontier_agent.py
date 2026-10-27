"""Agent 01 — the frontier.

Owns `crawl_queue`: what is waiting to be crawled, in what order, and which
domains are worth spending a request on first.

Queue order is the primary key (`rowid`), exactly as the pipeline reads it, so
priority has to be applied when the URL is enqueued rather than when it is
dequeued. Official Indian public domains therefore go in ahead of everything
else, and the dequeue query stays a plain `ORDER BY rowid`.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlparse, urlunparse

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

#: Indian portals that are not on an Indian public suffix but are worth
#: crawling: news, legal reporting, startup and developer media, jobs and open
#: data. The crawl is India-focused, so this list holds no foreign domains.
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

#: Ordinary global TLDs. With `allow_global` on, a link to any registrable
#: domain under one of these is crawlable, which is what stops the frontier
#: starving once the Indian sources are exhausted.
GLOBAL_ALLOWED_TLDS: frozenset[str] = frozenset(
    {".org", ".edu", ".net", ".com", ".io", ".dev", ".gov", ".int"}
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
#: grow the frontier faster than the workers drain it. This is a buffer, not a
#: speed control: the crawl is limited by database round trips, so a deeper
#: queue only means more work waits its turn.
MAX_PENDING_QUEUE = 50000

#: Sitemap locations tried, in order, for a newly encountered domain.
SITEMAP_PATHS: tuple[str, ...] = ("/sitemap.xml", "/sitemap_index.xml")

#: Never take more than this many URLs from one domain's sitemaps.
SITEMAP_MAX_URLS_PER_DOMAIN = 100

#: How many documents one harvest may fetch: the sitemap itself plus a couple
#: of nested sitemaps from an index.
SITEMAP_MAX_FETCHES = 3

#: Do not re-harvest a domain within this window.
SITEMAP_COOLDOWN_SECONDS = 3600

#: Harvest a sitemap only when the frontier is below this depth.
FRONTIER_LOW_WATER = 20

#: `<loc>` is the only element a sitemap or sitemap index needs here, so a
#: small regex is used rather than pulling in an XML parser for untrusted
#: documents of unbounded size.
LOC_PATTERN = re.compile(r"<loc>\s*(.*?)\s*</loc>", re.IGNORECASE | re.DOTALL)

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


#: Public suffixes that carry an extra label: the registrable domain is the
#: last three labels, so `optica.iiita.ac.in` is `iiita.ac.in`. Derived from
#: `PRIORITY_SUFFIXES` so the two can never drift apart.
TWO_PART_SUFFIXES: tuple[str, ...] = tuple(
    suffix for suffix in PRIORITY_SUFFIXES if suffix.count(".") == 2
)


def _is_ip_address(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def domain_of(url: str) -> str:
    """The registrable root domain of a URL.

    Subdomains are collapsed to the domain they belong to: `python.fossee.in`,
    `arduino.fossee.in` and `om.fossee.in` are all `fossee.in`. Without this a
    site that publishes one subdomain per project looks like dozens of separate
    domains — each with its own page quota, each taking a slot in the
    round-robin dequeue, which starves every real source behind it.

    The rule is the last two labels, or the last three when the host sits under
    a two-part Indian public suffix (`.gov.in`, `.ac.in`, …). IP addresses and
    single-label hosts are returned unchanged, because there is no registrable
    domain to extract.
    """
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]

    if _is_ip_address(host):
        return host

    labels = host.split(".")
    if len(labels) <= 2:
        return host

    if any(host.endswith(suffix) for suffix in TWO_PART_SUFFIXES):
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


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


def in_crawl_scope(url: str, source_host: str, *, allow_global: bool = False) -> bool:
    """Whether a link belongs to the crawl.

    The crawl targets the Indian web: the source host itself, any registrable
    domain under an Indian public suffix (`.in`, `.gov.in`, `.nic.in`, `.ac.in`,
    `.res.in`, `.edu.in`, `.org.in`, `.co.in`), or a whitelisted Indian portal.

    `allow_global=True` adds a ring for ordinary global TLDs. It is off by
    default — turning it on makes this a general web crawler, dilutes the index
    with foreign pages, and opens the largest trap surface there is. It is kept
    behind `ALLOW_GLOBAL_DOMAINS` so the decision stays reversible rather than
    being a one-way code change.
    """
    host = domain_of(url)
    if not host:
        return False
    if host == source_host:
        return True
    if is_authoritative(host):
        return True
    if allow_global:
        return any(host.endswith(tld) for tld in GLOBAL_ALLOWED_TLDS)
    return False


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


def parse_sitemap_locs(document: str) -> list[str]:
    """Every `<loc>` in a sitemap or sitemap index, in document order."""
    if not document:
        return []
    return [match.group(1).strip() for match in LOC_PATTERN.finditer(document)]


async def discover_sitemap_urls(
    base_url: str,
    client: Any,
    *,
    max_urls: int = SITEMAP_MAX_URLS_PER_DOMAIN,
    max_fetches: int = SITEMAP_MAX_FETCHES,
    allow_global: bool = False,
) -> list[str]:
    """Crawlable URLs from a domain's sitemap, bounded and in scope.

    A `sitemap_index.xml` does not list pages, it lists other sitemaps, so a
    `<loc>` that points at an `.xml` document is followed once (up to
    `max_fetches` in total) rather than being treated as a page.

    Everything here degrades to an empty list: a missing, blocked, gzipped or
    malformed sitemap is not an error, it just yields nothing and the frontier
    falls back to link discovery.
    """
    domain = domain_of(base_url)
    if not domain or client is None:
        return []

    candidates: list[str] = []
    fetches = 0

    for path in SITEMAP_PATHS:
        if fetches >= max_fetches:
            break
        try:
            response = await client.get(urljoin(base_url, path))
            fetches += 1
        except Exception as error:  # network shape is the fetcher's problem
            logger.debug("sitemap fetch failed for %s%s: %s", domain, path, error)
            continue

        if response.status_code != 200 or not response.text:
            continue

        for loc in parse_sitemap_locs(response.text):
            if loc.lower().endswith((".xml", ".xml.gz")):
                if fetches >= max_fetches:
                    continue
                try:
                    nested = await client.get(loc)
                    fetches += 1
                except Exception as error:
                    logger.debug("nested sitemap fetch failed for %s: %s", loc, error)
                    continue
                if nested.status_code == 200 and nested.text:
                    candidates.extend(parse_sitemap_locs(nested.text))
            else:
                candidates.append(loc)

        if candidates:
            break

    accepted: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalised = normalise_url(candidate)
        if not normalised or normalised in seen:
            continue
        if not is_crawlable(normalised):
            continue
        if not in_crawl_scope(normalised, domain, allow_global=allow_global):
            continue
        seen.add(normalised)
        accepted.append(normalised)
        if len(accepted) >= max_urls:
            break

    return accepted


def priority_of(url: str) -> int:
    """Lower sorts first: official public domains, then everything else."""
    return 0 if is_authoritative(domain_of(url)) else 1


class FrontierAgent:
    """Reads and writes the crawl frontier."""

    def __init__(
        self,
        store: IndexStore,
        settings: Settings | None = None,
        client: Any | None = None,
    ) -> None:
        self._store = store
        self._settings = settings or get_settings()
        #: Shared httpx client for sitemap harvesting. Optional so the frontier
        #: can be exercised without a network.
        self._client = client
        self._harvested_at: dict[str, float] = {}

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

        # No pre-flight `known_urls` query: `enqueue_urls` is INSERT OR IGNORE
        # and reports the rows it actually inserted, so it already answers
        # "how many of these were new" in the one request it has to make
        # anyway. The extra read was a round trip against the rate limiter on
        # every batch, for an answer we were about to compute for free.
        added = await self._store.enqueue_urls([(item.url, item.domain) for item in candidates])
        if added:
            logger.info("frontier: queued %d new URLs (depth now %d)", added, await self.depth())
        return added

    def filter_links(
        self,
        links: list[str] | tuple[str, ...],
        source_url: str,
        *,
        allow_global: bool = False,
    ) -> list[str]:
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
            if not in_crawl_scope(normalised, source_host, allow_global=allow_global):
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

        # A thin frontier is the moment to ask a domain what it publishes,
        # rather than waiting to rediscover it one link at a time.
        harvested = await self.harvest_sitemap()
        if harvested:
            return harvested

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

    async def harvest_sitemap(
        self,
        domains: tuple[str, ...] | None = None,
        *,
        force: bool = False,
    ) -> int:
        """Queue URLs from the sitemap of an authoritative domain, once an hour.

        Normally this only runs when the frontier is below `FRONTIER_LOW_WATER`,
        on the reasoning that a queue with work in it does not need more. That
        gate is wrong once the queue is large: with thousands of URLs pending,
        the depth never drops and sitemaps are skipped forever — precisely the
        domains that most need their sitemap read. `force=True` is for callers
        that know a specific domain is newly interesting, and it bypasses the
        depth check while still honouring the per-domain cooldown.

        A domain is marked as attempted before its sitemap is fetched, so a
        domain without one is not retried on every pass.
        """
        if self._client is None:
            return 0
        if not force and await self.depth() >= FRONTIER_LOW_WATER:
            return 0

        now = time.monotonic()
        for domain in domains or AUTHORITATIVE_DOMAINS:
            # Membership, not a 0.0 default: on a host whose monotonic clock
            # reads less than the cooldown, every domain would look freshly
            # harvested and no sitemap would ever be fetched.
            last = self._harvested_at.get(domain)
            if last is not None and now - last < SITEMAP_COOLDOWN_SECONDS:
                continue
            self._harvested_at[domain] = now

            urls = await discover_sitemap_urls(f"https://{domain}/", self._client)
            if not urls:
                logger.debug("sitemap harvest %s: nothing usable", domain)
                continue

            added = await self.enqueue(urls)
            if added:
                logger.info(
                    "sitemap harvest %s: %d URLs found, %d queued", domain, len(urls), added
                )
                return added
        return 0

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
    "FRONTIER_LOW_WATER",
    "GLOBAL_ALLOWED_TLDS",
    "LOC_PATTERN",
    "SITEMAP_COOLDOWN_SECONDS",
    "SITEMAP_MAX_FETCHES",
    "SITEMAP_MAX_URLS_PER_DOMAIN",
    "SITEMAP_PATHS",
    "TWO_PART_SUFFIXES",
    "discover_sitemap_urls",
    "parse_sitemap_locs",
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
