"""The crawler itself: fetch politely, extract, follow same-host links once.

Politeness rules that are not optional: robots.txt is honoured, one request at a
time per host with a delay, a byte cap per page, and a page budget per seed.
Anything that fails is skipped — a broken page never stops a crawl.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from app.crawler.seeds import BLOCKED_HOSTS, Seed
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.text import ExtractedDocument, extract_document, extract_links

logger = logging.getLogger(__name__)

ALLOWED_CONTENT_TYPES = ("text/html", "application/xhtml+xml")


@dataclass(slots=True)
class CrawlResult:
    seed: str
    documents: list[ExtractedDocument] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    fetched: int = 0


class RobotsCache:
    """Per-host robots.txt, fetched once per crawl and cached in memory."""

    def __init__(self, client: httpx.AsyncClient, user_agent: str) -> None:
        self._client = client
        self._user_agent = user_agent
        self._parsers: dict[str, RobotFileParser | None] = {}

    async def allowed(self, url: str) -> bool:
        host = urlparse(url).netloc
        if host not in self._parsers:
            parser = RobotFileParser()
            robots_url = f"{urlparse(url).scheme}://{host}/robots.txt"
            try:
                response = await self._client.get(robots_url, timeout=5.0)
                if response.status_code == 200 and response.text:
                    parser.parse(response.text.splitlines())
                    self._parsers[host] = parser
                else:
                    # No robots.txt (or unreadable) means no restriction stated.
                    self._parsers[host] = None
            except Exception as exc:
                logger.debug("robots.txt unavailable for %s: %s", host, exc)
                self._parsers[host] = None
        parser = self._parsers[host]
        return True if parser is None else parser.can_fetch(self._user_agent, url)


def _is_blocked(url: str) -> bool:
    host = urlparse(url).netloc.split(":")[0]
    return any(host == blocked or host.endswith(f".{blocked}") for blocked in BLOCKED_HOSTS)


def crawl_client(settings: EngineSettings) -> httpx.AsyncClient:
    """The HTTP client for crawling, with TLS configured once.

    `CRAWL_VERIFY_TLS=never` (the default) skips certificate checks entirely so
    hosts with incomplete chains still index; `auto` verifies and falls back per
    request; `always` verifies strictly.
    """
    policy = (settings.crawl_verify_tls or "never").lower()
    return httpx.AsyncClient(
        follow_redirects=True,
        verify=policy != "never",
        timeout=settings.crawl_timeout_seconds,
    )


async def _stream_page(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    settings: EngineSettings,
) -> tuple[str, int] | None:
    """One streamed GET, byte-capped. TLS policy is the client's, not the call's."""
    async with client.stream(
        "GET",
        url,
        headers=headers,
        timeout=settings.crawl_timeout_seconds,
        follow_redirects=True,
    ) as response:
        if response.status_code == 304:
            return None
        content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
        if response.status_code >= 400:
            return None
        if content_type and content_type not in ALLOWED_CONTENT_TYPES:
            return None
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= settings.crawl_max_bytes:
                break
        return b"".join(chunks).decode(response.encoding or "utf-8", errors="ignore"), response.status_code


async def fetch_page(
    client: httpx.AsyncClient,
    url: str,
    settings: EngineSettings,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> tuple[str, int] | None:
    """Fetch one page, capped by bytes and content type. None means skip.

    Pass `etag` / `last_modified` to make the request conditional; a 304 comes
    back as `None` here, which the revalidation worker treats as "unchanged" and
    answers with zero database writes.

    TLS follows `CRAWL_VERIFY_TLS`: "auto" verifies and, when a host presents a
    broken certificate chain (common on NIC portals), retries that one request
    unverified with a warning; "never" skips verification entirely; "always"
    refuses the fallback.
    """
    if _is_blocked(url):
        return None

    headers = {
        "User-Agent": settings.crawl_user_agent,
        "Accept": "text/html,application/xhtml+xml",
    }
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    policy = (settings.crawl_verify_tls or "never").lower()
    try:
        try:
            return await _stream_page(client, url, headers, settings)
        except (httpx.ConnectError, httpx.ReadError) as exc:
            if policy != "auto" or "certificate" not in str(exc).lower():
                raise
            logger.warning(
                "TLS verification failed for %s (%s) — retrying this request unverified",
                url,
                exc,
            )
            async with httpx.AsyncClient(
                verify=False,
                follow_redirects=True,
                timeout=settings.crawl_timeout_seconds,
            ) as insecure:
                return await _stream_page(insecure, url, headers, settings)
    except (httpx.HTTPError, asyncio.TimeoutError) as exc:
        logger.debug("fetch failed %s: %s", url, exc)
        return None


async def crawl_seed(
    seed: Seed,
    *,
    settings: EngineSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> CrawlResult:
    """Crawl one seed: its allowed paths, then same-host links up to the depth cap."""
    cfg = settings or get_engine_settings()
    result = CrawlResult(seed=seed.url)
    if not cfg.crawl_enabled:
        return result

    owned_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=True)
    robots = RobotsCache(http, cfg.crawl_user_agent)

    try:
        queue: list[tuple[str, int]] = [(seed.url, 0)]
        visited: set[str] = set()

        while queue and len(result.documents) < cfg.crawl_max_pages_per_seed:
            url, depth = queue.pop(0)
            if url in visited or len(visited) >= cfg.crawl_max_pages_per_seed:
                continue
            if seed.paths and depth > 0 and not any(urlparse(url).path.startswith(p) for p in seed.paths):
                continue
            if cfg.respect_robots and not await robots.allowed(url):
                result.skipped.append(url)
                continue

            visited.add(url)
            fetched = await fetch_page(http, url, cfg)
            result.fetched += 1
            if fetched is None:
                result.skipped.append(url)
                continue

            html, status = fetched
            document = extract_document(url, html, http_status=status)
            if document is None:
                result.skipped.append(url)
            else:
                result.documents.append(document)

            if depth < cfg.crawl_max_depth:
                for link in list(extract_links(html, url))[:20]:
                    if link not in visited:
                        queue.append((link, depth + 1))

            await asyncio.sleep(cfg.crawl_delay_seconds)
    finally:
        if owned_client:
            await http.aclose()

    logger.info(
        "crawl %s: %d documents, %d skipped, %d fetched",
        seed.url,
        len(result.documents),
        len(result.skipped),
        result.fetched,
    )
    return result


async def crawl_seeds(
    seeds: list[Seed],
    *,
    settings: EngineSettings | None = None,
) -> list[CrawlResult]:
    """Crawl several seeds with bounded concurrency (never unbounded fan-out)."""
    cfg = settings or get_engine_settings()
    semaphore = asyncio.Semaphore(max(1, cfg.crawl_concurrency))
    results: list[CrawlResult] = []

    async with httpx.AsyncClient(follow_redirects=True) as client:
        async def run(seed: Seed) -> CrawlResult:
            async with semaphore:
                return await crawl_seed(seed, settings=cfg, client=client)

        for outcome in await asyncio.gather(*(run(seed) for seed in seeds), return_exceptions=True):
            if isinstance(outcome, CrawlResult):
                results.append(outcome)
            else:
                logger.warning("seed crawl failed: %s", outcome)
    return results


__all__ = [
    "crawl_client","CrawlResult", "crawl_seed", "crawl_seeds", "fetch_page", "RobotsCache"]
