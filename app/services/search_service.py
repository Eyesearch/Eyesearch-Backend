"""Orchestration: cache (Turso) -> live retrieval chain -> async persistence.

The service owns the request budget. A database outage degrades the response
instead of failing it, and cache writes happen after the response is shaped so
they never sit on the critical path.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

import httpx

from app.config import Settings, get_settings
from app.engine.config import get_engine_settings
from app.engine.relevance import filter_candidates, is_relevant
from app.engine.store import get_store
from app.indexer.extractor import extract_candidates
from app.errors import DatabaseError
from app.schemas import (
    AIContextResult,
    MetaSearchResponse,
    MetaSearchResult,
    RagContextItem,
    RetrieveResponse,
    SearchResponse,
    SearchResult,
)
from app.services import formatter, live_retrieval
from app.services.live_retrieval import RawResult, RetrievalOutcome

logger = logging.getLogger(__name__)

# Upper bound on the inline index write that follows a first-time live fetch.
AUTO_INGEST_TIMEOUT_S = 2.0

# Strong references so fire-and-forget cache writes are not garbage collected
# mid-flight; the done-callback releases each one.
_background_tasks: set[asyncio.Task[Any]] = set()


def _schedule_write(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


@dataclass(slots=True)
class Acquisition:
    """Results plus provenance, shared by both endpoints."""

    query: str
    results: list[RawResult] = field(default_factory=list)
    providers: dict[str, int] = field(default_factory=dict)
    failed: list[dict[str, str]] = field(default_factory=list)
    cache_hit: bool = False
    total_estimate: int = 0
    # "index" = answered by our own FTS5 index, "cache" = Turso result cache,
    # "live" = external providers.
    source: str = "live"
    index_hits: int = 0
    auto_ingested: int = 0

    @property
    def degraded(self) -> bool:
        return bool(self.failed)

    @property
    def failed_providers(self) -> list[str]:
        return [item.get("provider", "unknown") for item in self.failed]


class SearchService:
    """Single entry point used by every route."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._client = client
        self._settings = settings or get_settings()

    @property
    def settings(self) -> Settings:
        return self._settings

    def bind_client(self, client: httpx.AsyncClient) -> None:
        self._client = client

    # -- acquisition -----------------------------------------------------
    async def acquire(self, query: str, limit: int) -> Acquisition:
        """Index first; on a miss, verify and extract on demand.

        1. `documents_fts` with BM25 ranking (title > URL > snippet) and the
           universal relevance gate. Any hit is served from our own index.
        2. On a miss, ask the providers only for *candidate URLs*. Candidates
           are gated on their own title/URL/snippet, the survivors are fetched
           and parsed by the native extractor, gated again on the extracted
           document, and upserted into `documents`/`documents_fts`.
        3. The response carries our extracts — never a provider's raw text — and
           the next query for these words is a step-1 hit.

        Nothing is written to `search_records`: that table held unverified
        provider dumps and is no longer part of the serving path.
        """
        normalized = " ".join(query.split())
        acquisition = Acquisition(query=normalized)
        engine = get_engine_settings()
        store = get_store()

        # 1. our own index
        try:
            local = await store.search_local(normalized, limit)
        except Exception as exc:  # a broken index must never fail a request
            local = []
            logger.warning("index lookup failed for %r: %s", normalized, exc)

        acquisition.index_hits = len(local)
        if local:
            acquisition.results = [
                RawResult(
                    title=doc.title,
                    url=doc.url,
                    snippet=doc.snippet,
                    source="eyesearch_index",
                    position=index,
                    image_url=doc.favicon_url,
                )
                for index, doc in enumerate(local)
            ]
            acquisition.providers = {"eyesearch_index": len(local)}
            acquisition.source = "index"
            acquisition.total_estimate = len(local)
            return acquisition

        # 2. miss: nothing outbound unless discovery is enabled at all
        if not engine.live_search_enabled:
            _schedule_write(store.enqueue_backlog(normalized, 0))
            acquisition.source = "queued"
            return acquisition

        # 2a. do not hammer the providers for a query we already gave up on
        try:
            if await store.recently_queued(normalized):
                acquisition.source = "queued"
                return acquisition
        except Exception as exc:
            logger.debug("backlog guard skipped for %r: %s", normalized, exc)

        verified = await self._extract_on_demand(normalized, engine)
        acquisition.failed.extend(verified.get("failed", []))
        acquisition.auto_ingested = verified["stored"]
        acquisition.providers = {"eyesearch_index": verified["stored"]}
        acquisition.index_hits = verified["stored"]

        if verified["documents"]:
            acquisition.results = [
                RawResult(
                    title=document.title,
                    url=document.url,
                    snippet=document.snippet,
                    source="eyesearch_index",
                    position=index,
                    image_url=document.favicon_url,
                )
                for index, document in enumerate(verified["documents"])
            ]
            acquisition.source = "index"
            acquisition.total_estimate = len(acquisition.results)
            return acquisition

        # 3. nothing verified: say so, and leave the query for the crawler
        _schedule_write(store.enqueue_backlog(normalized, 0))
        acquisition.source = "queued"
        return acquisition

    async def _extract_on_demand(self, query: str, engine: Any) -> dict[str, Any]:
        """Discover → gate → extract → gate → upsert. Returns what was verified."""
        outcome: dict[str, Any] = {
            "documents": [], "stored": 0, "candidates": 0, "dropped": 0, "failed": []
        }

        try:
            async with asyncio.timeout(engine.on_demand_budget_seconds):
                discovery = await live_retrieval.retrieve(
                    query,
                    engine.on_demand_max_candidates,
                    client=self._client,
                    settings=self._settings,
                )
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("on-demand discovery timed out for %r", query)
            return outcome
        except Exception as exc:
            logger.warning("on-demand discovery failed for %r: %s", query, exc)
            return outcome

        outcome["failed"] = list(getattr(discovery, "failed", []) or [])
        candidates = [
            {"title": result.title, "url": result.url, "snippet": result.snippet}
            for result in discovery.results
        ]
        outcome["candidates"] = len(candidates)

        # Gate before we spend a single fetch on a candidate.
        gated = filter_candidates(
            query, candidates, min_coverage=engine.relevance_min_token_coverage
        )
        outcome["dropped"] = len(candidates) - len(gated)
        if not gated:
            logger.info(
                "on-demand: %d candidates for %r all failed the relevance gate",
                len(candidates),
                query,
            )
            return outcome

        urls = [candidate["url"] for candidate in gated][: engine.on_demand_max_fetches]
        try:
            documents = await extract_candidates(
                urls, client=self._client, settings=engine, limit=engine.on_demand_max_fetches
            )
        except Exception as exc:
            logger.warning("on-demand extraction failed for %r: %s", query, exc)
            return outcome

        # Gate again, this time on our own extract rather than the provider's text.
        verified = [
            document
            for document in documents
            if is_relevant(
                query,
                title=document.title,
                url=document.url,
                snippet=document.snippet,
                min_coverage=engine.relevance_min_token_coverage,
            )
        ]
        outcome["dropped"] += len(documents) - len(verified)
        if not verified:
            return outcome

        try:
            written = await get_store().ingest_documents(
                [document.as_row() for document in verified], mode="upsert"
            )
            outcome["stored"] = int(written.get("inserted", 0)) + int(written.get("replaced", 0))
            logger.info(
                "on-demand: verified %d documents for %r and stored them (%s)",
                len(verified),
                query,
                written,
            )
        except Exception as exc:
            logger.warning("on-demand ingest failed for %r: %s", query, exc)

        outcome["documents"] = verified
        return outcome

    async def suggest(self, query: str, limit: int | None = None) -> list[str]:
        """Autocomplete from the index. Never touches the network."""
        cleaned = " ".join((query or "").split())
        if len(cleaned) < 2:
            return []
        return await get_store().suggest(cleaned, limit or get_engine_settings().suggest_limit)

    # -- contracts -------------------------------------------------------
    async def search(
        self,
        query: str,
        *,
        limit: int | None = None,
        page: int = 1,
    ) -> SearchResponse:
        started = time.perf_counter()
        cfg = self._settings
        effective_limit = min(max(1, limit or cfg.default_limit), cfg.max_limit)
        safe_page = max(1, page)
        offset = (safe_page - 1) * effective_limit
        needed = min(offset + effective_limit, cfg.max_limit)

        acquisition = await self.acquire(query, needed)
        window = acquisition.results[offset : offset + effective_limit]
        cards: list[SearchResult] = formatter.build_ui_results(
            window, settings=cfg, limit=effective_limit
        )
        return SearchResponse(
            query=acquisition.query,
            page=safe_page,
            limit=effective_limit,
            total_estimate=max(acquisition.total_estimate, len(acquisition.results)),
            took_ms=int((time.perf_counter() - started) * 1000),
            degraded=acquisition.degraded,
            cache_hit=acquisition.cache_hit,
            source=acquisition.source,
            index_hits=acquisition.index_hits,
            auto_ingested=acquisition.auto_ingested,
            providers=acquisition.providers,
            results=cards,
        )

    async def meta_search(self, query: str, *, limit: int | None = None) -> MetaSearchResponse:
        """The flat, aggregator-friendly JSON contract (`format=json`)."""
        started = time.perf_counter()
        cfg = self._settings
        effective = min(max(1, limit or cfg.default_limit), cfg.max_limit)
        acquisition = await self.acquire(query, effective)

        results = [
            MetaSearchResult(
                title=formatter.clean_snippet(raw.title or raw.url, 160),
                url=formatter.canonical_url(raw.url),
                snippet=formatter.clean_snippet(raw.snippet, cfg.snippet_max_chars),
                favicon=raw.image_url,
            )
            for raw in formatter.dedupe(acquisition.results)[:effective]
        ]
        source = "eyesearch_index" if acquisition.source == "index" else f"eyesearch_{acquisition.source}"
        return MetaSearchResponse(
            query=acquisition.query,
            total_hits=max(acquisition.index_hits, len(results), acquisition.total_estimate),
            took_ms=int((time.perf_counter() - started) * 1000),
            source=source,
            auto_ingested=acquisition.auto_ingested,
            results=results,
        )

    async def rag_context(self, query: str, *, limit: int | None = None) -> tuple[list[RagContextItem], Acquisition]:
        """Token-optimised context blocks for LLM clients."""
        cfg = self._settings
        effective = min(max(1, limit or cfg.default_limit), cfg.max_limit)
        acquisition = await self.acquire(query, effective)
        context = [
            RagContextItem(
                title=formatter.clean_snippet(raw.title or raw.url, 160),
                url=formatter.canonical_url(raw.url),
                text=formatter.clean_snippet(raw.snippet, cfg.ai_snippet_max_chars),
            )
            for raw in formatter.dedupe(acquisition.results)[:effective]
        ]
        return context, acquisition

    async def retrieve(
        self,
        query: str,
        *,
        limit: int | None = None,
    ) -> tuple[list[AIContextResult], Acquisition]:
        cfg = self._settings
        effective_limit = min(max(1, limit or cfg.default_limit), cfg.max_limit)
        acquisition = await self.acquire(query, effective_limit)
        results = formatter.build_ai_results(
            acquisition.results, settings=cfg, limit=effective_limit
        )
        return results, acquisition

    async def retrieve_json(
        self, query: str, *, limit: int | None = None
    ) -> RetrieveResponse:
        results, acquisition = await self.retrieve(query, limit=limit)
        context = [RagContextItem(title=item.title, url=item.url, text=item.snippet) for item in results]
        return RetrieveResponse(
            query=acquisition.query,
            format="json",
            degraded=acquisition.degraded,
            source=acquisition.source,
            index_hits=acquisition.index_hits,
            providers=acquisition.providers,
            failed_providers=acquisition.failed_providers,
            results=results,
            context=context,
        )


_service: SearchService | None = None


def get_service() -> SearchService:
    global _service
    if _service is None:
        _service = SearchService()
    return _service


def configure(client: httpx.AsyncClient | None, settings: Settings | None = None) -> SearchService:
    """Wire the shared HTTP client in from ``lifespan``."""
    global _service
    _service = SearchService(client=client, settings=settings)
    return _service


async def drain_background_writes(timeout_s: float = 2.0) -> None:
    """Best-effort flush of pending cache writes during shutdown."""
    if not _background_tasks:
        return
    pending = list(_background_tasks)
    try:
        async with asyncio.timeout(timeout_s):
            await asyncio.gather(*pending, return_exceptions=True)
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning("shutdown: %d cache writes still pending", len(pending))
