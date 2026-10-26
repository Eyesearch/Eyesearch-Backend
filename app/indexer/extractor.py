"""The native extraction pipeline: fetch a candidate, strip it, keep 1–2 KB.

This is the only way a page enters the index from an on-demand query. The
provider tells us *which* URLs might answer the query; this module decides what
we actually store, and it stores our own clean extract — never the provider's
raw title and snippet.

Every step is bounded: robots.txt is honoured, pages are byte-capped, the
extractor strips scripts, styles, navigation, footers, ads, inline images and
base64 payloads, and the result is fitted to the storage budget before it is
returned.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.crawler.crawler import RobotsCache, fetch_page
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.text import ExtractedDocument, extract_document

logger = logging.getLogger(__name__)


async def extract_candidate(
    url: str,
    *,
    client: httpx.AsyncClient,
    settings: EngineSettings | None = None,
    robots: RobotsCache | None = None,
) -> ExtractedDocument | None:
    """Fetch one URL and return our own extract, or None if it is unusable."""
    cfg = settings or get_engine_settings()

    if cfg.respect_robots and robots is not None and not await robots.allowed(url):
        logger.info("extract: robots.txt disallows %s", url)
        return None

    fetched = await fetch_page(client, url, cfg)
    if fetched is None:
        return None

    html, status = fetched
    document = extract_document(url, html, http_status=status)
    if document is None:
        logger.debug("extract: nothing indexable at %s", url)
    return document


async def extract_candidates(
    urls: list[str],
    *,
    client: httpx.AsyncClient,
    settings: EngineSettings | None = None,
    limit: int | None = None,
) -> list[ExtractedDocument]:
    """Extract up to `limit` candidates, with bounded concurrency."""
    cfg = settings or get_engine_settings()
    wanted = urls[: (limit or cfg.on_demand_max_fetches)]
    if not wanted:
        return []

    robots = RobotsCache(client, cfg.crawl_user_agent)
    semaphore = asyncio.Semaphore(max(1, cfg.on_demand_concurrency))

    async def run(url: str) -> ExtractedDocument | None:
        async with semaphore:
            try:
                return await extract_candidate(url, client=client, settings=cfg, robots=robots)
            except Exception as exc:  # one bad page must not stop the batch
                logger.info("extract failed for %s: %s", url, exc)
                return None

    results = await asyncio.gather(*(run(url) for url in wanted), return_exceptions=False)
    documents = [document for document in results if document is not None]
    logger.info("extracted %d of %d candidates", len(documents), len(wanted))
    return documents


__all__ = ["extract_candidate", "extract_candidates"]
