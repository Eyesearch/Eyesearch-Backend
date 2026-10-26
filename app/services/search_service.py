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

from app import database
from app.config import Settings, get_settings
from app.engine.config import get_engine_settings
from app.engine.store import get_store
from app.errors import DatabaseError
from app.schemas import AIContextResult, RetrieveResponse, SearchResponse, SearchResult
from app.services import formatter, live_retrieval
from app.services.live_retrieval import RawResult, RetrievalOutcome

logger = logging.getLogger(__name__)

# Strong references so fire-and-forget cache writes are not garbage collected
# mid-flight; the done-callback releases each one.
_background_tasks: set[asyncio.Task[Any]] = set()


def _schedule_write(coro: Any) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _persist(query: str, records: Sequence[dict[str, Any]]) -> None:
    try:
        written = await database.upsert_records(query, records)
        if written:
            logger.debug("cached %d records for %r", written, query)
    except DatabaseError as exc:
        # A failed cache write must never reach the client.
        logger.warning("cache write skipped for %r: %s", query, exc)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive
        logger.exception("unexpected cache write error for %r", query)


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

    @property
    def degraded(self) -> bool:
        return bool(self.failed)

    @property
    def failed_providers(self) -> list[str]:
        return [item.get("provider", "unknown") for item in self.failed]


def _cached_to_raw(rows: Sequence[dict[str, Any]]) -> list[RawResult]:
    raws: list[RawResult] = []
    for position, row in enumerate(rows):
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        raws.append(
            RawResult(
                title=str(row.get("title") or url),
                url=url,
                snippet=str(row.get("snippet") or ""),
                source=str(row.get("source") or "cache"),
                position=position,
                image_url=row.get("poster_url") or None,
            )
        )
    return raws


def _raw_to_records(raws: Sequence[RawResult], settings: Settings) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for raw in raws:
        poster, thumbnail = formatter.derive_media(raw.url)
        records.append(
            {
                "title": formatter.clean_snippet(raw.title, 200),
                "url": raw.url,
                "snippet": formatter.clean_snippet(raw.snippet, settings.snippet_max_chars),
                "poster_url": raw.image_url or poster or thumbnail,
                "source": raw.source,
            }
        )
    return records


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
        """Index first, then the Turso cache, then the live chain.

        Our own index is the preferred source: if it returns at least
        `INDEX_THRESHOLD` documents the query is answered entirely from it. A
        sparse miss falls through to the cache and the external providers, and
        the unanswered query is queued for the crawler so the next identical
        query can be served locally.
        """
        normalized = " ".join(query.split())
        acquisition = Acquisition(query=normalized)
        engine = get_engine_settings()

        # 0. proprietary index
        local_hits = 0
        try:
            local = await get_store().search_local(normalized, limit)
        except Exception as exc:  # a broken index must never fail a request
            local = []
            logger.warning("index lookup failed for %r: %s", normalized, exc)

        local_hits = len(local)
        acquisition.index_hits = local_hits
        if local_hits >= engine.index_threshold:
            acquisition.results = [
                RawResult(
                    title=doc.title,
                    url=doc.url,
                    snippet=doc.snippet,
                    source="eyesearch_index",
                    position=index,
                )
                for index, doc in enumerate(local)
            ]
            acquisition.providers = {"eyesearch_index": local_hits}
            acquisition.source = "index"
            acquisition.total_estimate = local_hits
            return acquisition

        # 1. cache
        try:
            cached = await database.fetch_cached(normalized, limit)
        except DatabaseError as exc:
            cached = []
            acquisition.failed.append({"provider": "turso_cache", "error": type(exc).__name__})
            logger.warning("cache read failed for %r: %s", normalized, exc)

        if len(cached) >= self._settings.cache_min_results:
            acquisition.results = _cached_to_raw(cached)
            acquisition.cache_hit = True
            acquisition.providers = {"turso_cache": len(acquisition.results)}
            acquisition.total_estimate = len(acquisition.results)
            try:
                acquisition.total_estimate = max(
                    acquisition.total_estimate,
                    await database.count_cached(normalized),
                )
            except DatabaseError:
                pass
            acquisition.source = "cache"
            return acquisition

        # 2. The request path never calls an external provider. A local miss is
        # queued for the crawler and answered immediately, which is what keeps
        # the fast path at single-digit milliseconds.
        if not engine.live_search_enabled:
            _schedule_write(get_store().enqueue_backlog(normalized, local_hits))
            acquisition.source = "queued"
            acquisition.total_estimate = 0
            return acquisition

        # 2b. Emergency escape hatch only (LIVE_SEARCH_ENABLED=true).
        outcome: RetrievalOutcome = await live_retrieval.retrieve(
            normalized,
            limit,
            client=self._client,
            settings=self._settings,
        )
        acquisition.results = outcome.results
        acquisition.providers = dict(outcome.providers)
        acquisition.failed.extend(outcome.failed)
        acquisition.total_estimate = len(outcome.results)

        # 3. persist without blocking the response
        if outcome.results:
            _schedule_write(_persist(normalized, _raw_to_records(outcome.results, self._settings)))

        # 4. the index could not answer this query, so queue it for the crawler —
        # whether or not the external providers happened to return anything.
        if local_hits < engine.index_threshold:
            _schedule_write(get_store().enqueue_backlog(normalized, local_hits))

        acquisition.source = "live"
        return acquisition

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
            providers=acquisition.providers,
            results=cards,
        )

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
        return RetrieveResponse(
            query=acquisition.query,
            format="json",
            degraded=acquisition.degraded,
            source=acquisition.source,
            index_hits=acquisition.index_hits,
            providers=acquisition.providers,
            failed_providers=acquisition.failed_providers,
            results=results,
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
