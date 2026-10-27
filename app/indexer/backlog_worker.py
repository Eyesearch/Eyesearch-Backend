"""Autonomous backlog worker.

When a query misses the index it is written to `crawl_backlog`. This worker pops
those queries, works out which URLs would answer them, fetches and extracts
those pages under the same 2 KB budget and dedupe rules as the crawler, and
ingests them — so the *next* identical query is answered from the index.

It runs entirely off the request path. Discovery is the only part that may touch
an external provider (`BACKLOG_DISCOVERY_ENABLED`), and it happens here, in the
worker, never while a user is waiting.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.crawler.crawler import fetch_page
from app.crawler.seeds import SEED_DOMAINS
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.store import IndexStore, get_store
from app.engine.text import extract_document
from app.engine.urls import domain_class, is_indexable, normalise_url

logger = logging.getLogger(__name__)

_AUTHORITY = {"government": 0, "news": 1, "static": 2}


class BacklogReport(dict):
    """Counters for one pass."""

    def __init__(self) -> None:
        super().__init__(queries=0, resolved=0, urls=0, inserted=0, skipped=0, dropped=0)


def _authority_key(url: str) -> tuple[int, int]:
    """Government before news before everything else; shallower paths first."""
    depth = len([part for part in url.split("/")[3:] if part])
    return (_AUTHORITY.get(domain_class(url), 3), depth)


def _seed_candidates(query: str) -> list[str]:
    """Seed portals whose label or host shares a word with the query."""
    tokens = {token for token in query.lower().split() if len(token) > 2}
    if not tokens:
        return []
    candidates: list[str] = []
    for seed in SEED_DOMAINS:
        haystack = f"{seed.label} {seed.url}".lower().replace("/", " ").replace(".", " ")
        if tokens & {word for word in haystack.split() if len(word) > 2}:
            candidates.append(seed.url)
    return candidates


async def _discovered_candidates(
    query: str,
    settings: EngineSettings,
    client: httpx.AsyncClient,
) -> list[str]:
    """Ask the provider chain which URLs answer this query (worker-side only)."""
    if not settings.backlog_discovery_enabled:
        return []
    try:
        # Imported lazily: the request path must never depend on this module.
        from app.services import live_retrieval

        outcome = await live_retrieval.retrieve(
            query,
            settings.backlog_urls_per_query * 2,
            client=client,
            settings=None,
        )
        return [result.url for result in outcome.results]
    except Exception as exc:  # discovery is best-effort
        logger.info("discovery unavailable for %r: %s", query, exc)
        return []


async def discover_urls(
    query: str,
    settings: EngineSettings,
    client: httpx.AsyncClient,
) -> list[str]:
    """Candidate URLs for a missed query: seed matches first, then discovery."""
    raw = _seed_candidates(query) + await _discovered_candidates(query, settings, client)

    seen: set[str] = set()
    usable: list[str] = []
    for url in raw:
        normalised = normalise_url(url)
        if not normalised or normalised in seen or not is_indexable(normalised):
            continue
        seen.add(normalised)
        usable.append(normalised)

    usable.sort(key=_authority_key)
    return usable[: settings.backlog_urls_per_query]


async def resolve_query(
    query: str,
    *,
    store: IndexStore,
    settings: EngineSettings,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """Crawl and ingest the pages that should answer `query`."""
    urls = await discover_urls(query, settings, client)
    outcome: dict[str, Any] = {"query": query, "urls": len(urls), "inserted": 0, "skipped": 0}
    if not urls:
        return outcome

    documents = []
    for url in urls:
        fetched = await fetch_page(client, url, settings)
        if fetched is None:
            outcome["skipped"] += 1
            continue
        html, status = fetched
        document = extract_document(url, html, http_status=status)
        if document is None:
            outcome["skipped"] += 1
            continue
        documents.append(document.as_row())

    if documents:
        ingest = await store.ingest_documents(documents)
        outcome["inserted"] = int(ingest.get("inserted", 0))
        outcome["skipped"] += int(ingest.get("duplicate", 0)) + int(ingest.get("domain_cap", 0))
        outcome["ingest"] = ingest
    return outcome


async def run_backlog_once(
    limit: int | None = None,
    *,
    store: IndexStore | None = None,
    settings: EngineSettings | None = None,
    client: httpx.AsyncClient | None = None,
) -> BacklogReport:
    """Resolve one batch of pending queries. Returns the counters."""
    cfg = settings or get_engine_settings()
    index = store or get_store()
    report = BacklogReport()
    if not cfg.crawl_enabled:
        return report

    queries = await index.pending_backlog(limit or cfg.backlog_batch_queries)
    if not queries:
        return report

    owned_client = client is None
    http = client or httpx.AsyncClient(follow_redirects=True)
    try:
        for query in queries:
            report["queries"] += 1
            try:
                outcome = await resolve_query(query, store=index, settings=cfg, client=http)
            except Exception as exc:
                logger.warning("backlog resolution failed for %r: %s", query, exc)
                continue

            report["urls"] += int(outcome["urls"])
            report["inserted"] += int(outcome["inserted"])
            report["skipped"] += int(outcome["skipped"])
            if int(outcome["inserted"]) > 0:
                report["resolved"] += 1
                await index.mark_backlog_processed(query)
                logger.info("backlog resolved %r → %d documents", query, outcome["inserted"])
                continue

            # Nothing indexed this time. Count the attempt; once a query has been
            # blocked or empty too many times it goes to history instead of
            # stalling the queue on every pass.
            attempts = await index.note_backlog_attempt(
                query, reason=str(outcome.get("reason") or "unresolved")
            )
            limit = max(1, cfg.backlog_max_attempts)
            if attempts >= limit:
                await index.mark_backlog_dropped(query, reason="attempts exhausted")
                report["dropped"] = int(report.get("dropped", 0)) + 1
                logger.info(
                    "backlog query %r dropped after %d attempts (blocked or empty)", query, attempts
                )
            else:
                logger.info(
                    "backlog query %r unresolved (attempt %d of %d)", query, attempts, limit
                )
    finally:
        if owned_client:
            await http.aclose()

    logger.info("backlog pass: %s", dict(report))
    return report


__all__ = ["BacklogReport", "run_backlog_once", "resolve_query", "discover_urls"]
