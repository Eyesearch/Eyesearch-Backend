"""The FastAPI application: lifespan, CORS, and the frontend-facing API.

The endpoint set is the contract `web/config/api-config.js` and
`web/results/ai-chat/chat-panel.js` already speak:

| method | path             | consumer                                  |
| ------ | ---------------- | ----------------------------------------- |
| GET    | `/v1/search`     | result cards, or `format=json` flat hits   |
| GET    | `/v1/suggest`    | address-bar autocomplete                   |
| GET    | `/v1/retrieve`   | AI clients, markdown or json               |
| POST   | `/v1/retrieve`   | AI clients, JSON body                      |
| POST   | `/v1/chat`       | the AI tab, server-sent events             |
| GET    | `/v1/stats`      | index counters                             |
| GET    | `/health`        | Render health probe, never raises          |
| GET    | `/opensearch.xml`| browser search-engine install              |

One rule for errors: every failure leaves through the same envelope,
`{"error": {"code", "message", "retryable"}}`, because the frontend maps `code`
onto user-facing text and treats a missing `retryable` as true.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from app.database import initialize_database, verify_writable
from app.engine.config import Settings, describe_database_target, get_settings
from app.engine.store import IndexStore
from app.indexer.scheduler import WorkerHandles, start_workers, stop_workers
from app.libsql_http import LibsqlClient, LibsqlError, build_client
from app.orchestrator import Orchestrator
from app.services.search_service import SearchService

logger = logging.getLogger(__name__)

SERVICE_NAME = "eyesearch"
SERVICE_TITLE = "EyeSearch Backend"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AppError(Exception):
    """A failure with a contract-shaped answer."""

    def __init__(self, code: str, message: str, status: int = 500, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable


def error_response(code: str, message: str, status: int, retryable: bool) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "retryable": retryable}},
    )


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class RetrieveRequest(BaseModel):
    query: str = Field(default="", description="what to look up")
    format: str = Field(default="json", description="json or markdown")
    limit: int = Field(default=10, ge=1, le=50)


class ChatRequest(BaseModel):
    query: str = Field(default="")
    messages: list[dict[str, Any]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info("starting %s (%s)", SERVICE_TITLE, settings.environment)
    logger.info("database target: %s", describe_database_target(settings))
    logger.info(
        "effective crawl settings: enabled=%s batch_size=%d concurrency=%d "
        "timeout=%.1fs batch_sleep=%.3fs db_min_interval=%.3fs "
        "domain_page_cap=%d storage_page_limit=%d allow_global_domains=%s",
        settings.crawler_enabled,
        settings.crawl_batch_size,
        settings.crawl_concurrency,
        settings.crawl_timeout_seconds,
        settings.crawl_batch_sleep_seconds,
        settings.libsql_min_interval_seconds,
        settings.max_pages_per_domain,
        settings.storage_page_limit,
        settings.allow_global_domains,
    )

    client: LibsqlClient = build_client(settings)
    await initialize_database(client, allow_legacy_drop=settings.migrate_legacy_schema)
    writable = await verify_writable(client)
    if not writable:
        logger.error("database rejected the write probe — crawling will stay paused")

    orchestrator = Orchestrator(client, settings)
    app.state.settings = settings
    app.state.client = client
    app.state.orchestrator = orchestrator
    app.state.service = SearchService(orchestrator.store, settings)
    app.state.workers = WorkerHandles()

    # Workers are scheduled from a bootstrap task, not inline, so the port is
    # bound and /health answers before the first crawl begins.
    bootstrap = asyncio.create_task(_start_workers_when_serving(app, orchestrator, settings))
    app.state.bootstrap = bootstrap

    try:
        yield
    finally:
        bootstrap.cancel()
        await stop_workers(app.state.workers)
        await orchestrator.close()
        await client.close()
        logger.info("shutdown complete")


async def _start_workers_when_serving(
    app: FastAPI,
    orchestrator: Orchestrator,
    settings: Settings,
) -> None:
    """Give the event loop a turn, then start the crawl workers."""
    await asyncio.sleep(0)
    app.state.workers = start_workers(orchestrator, settings)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

settings_at_import = get_settings()

app = FastAPI(
    title=SERVICE_TITLE,
    version="2.0.0",
    description=(
        "Async search gateway over an own Turso (libSQL) index. "
        "One index, four consumer contracts."
    ),
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings_at_import.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(AppError)
async def handle_app_error(_request: Request, error: AppError) -> JSONResponse:
    return error_response(error.code, error.message, error.status, error.retryable)


@app.exception_handler(LibsqlError)
async def handle_db_error(_request: Request, error: LibsqlError) -> JSONResponse:
    logger.error("database error: %s", error)
    return error_response("database_error", "The search index is unavailable.", 503, True)


def get_service(request: Request) -> SearchService:
    service = getattr(request.app.state, "service", None)
    if service is None:
        raise AppError("internal", "The service is still starting.", 503, True)
    return service


def require_query(raw: str) -> str:
    query = " ".join((raw or "").split())
    if not query:
        raise AppError("bad_request", "A non-empty q parameter is required.", 400, False)
    if len(query) > 400:
        raise AppError("bad_request", "The query is too long.", 400, False)
    return query


# ---------------------------------------------------------------------------
# Service metadata and health
# ---------------------------------------------------------------------------


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": SERVICE_NAME,
        "title": SERVICE_TITLE,
        "version": app.version,
        "endpoints": [
            "/v1/search?q=&category=&lang=",
            "/v1/suggest",
            "/v1/retrieve",
            "/v1/chat",
            "/v1/stats",
            "/health",
            "/opensearch.xml",
            "/docs",
        ],
    }


async def health_payload(request: Request) -> dict[str, Any]:
    """Never raises: a probe that 500s is a probe that cannot report a fault."""
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    payload: dict[str, Any] = {
        "status": "ok",
        "service": SERVICE_NAME,
        "db": "unknown",
        "db_target": describe_database_target(settings),
        "sites": 0,
        "pages": 0,
        "indexed": 0,
        "queued": 0,
        "storage": {},
        "workers": [],
    }

    try:
        client = getattr(request.app.state, "client", None)
        if client is None:
            raise AppError("database_error", "database client is not ready", 503, True)

        stats = await IndexStore(client, settings).stats()
        payload.update(
            {
                "db": "up",
                "sites": stats.sites,
                "pages": stats.pages,
                "indexed": stats.indexed,
                "queued": stats.queued,
            }
        )

        orchestrator = getattr(request.app.state, "orchestrator", None)
        if orchestrator is not None:
            state = await orchestrator.guard.storage_state()
            payload["storage"] = state.as_dict()

        workers = getattr(request.app.state, "workers", None)
        if workers is not None:
            payload["workers"] = workers.running()
            payload["worker_status"] = workers.status()
            restarts = sum(workers.restarts.values())
            if restarts:
                payload["worker_restarts"] = restarts
    except Exception as error:  # health must answer, always
        payload["status"] = "degraded"
        payload["db"] = "down"
        payload["error"] = str(error)

    return payload


@app.get("/health")
@app.get("/healthz")
async def health(request: Request) -> JSONResponse:
    payload = await health_payload(request)
    status = 200 if payload["status"] == "ok" else 503
    return JSONResponse(status_code=status, content=payload)


# ---------------------------------------------------------------------------
# Search, suggest, retrieve
# ---------------------------------------------------------------------------


@app.get("/v1/search")
async def search(
    request: Request,
    q: str = Query(default="", description="the query"),
    limit: int = Query(default=10, ge=1, le=50),
    page: int = Query(default=1, ge=1, le=20),
    format: str = Query(default="cards", description="cards or json"),
    category: str | None = Query(
        default=None,
        description="restrict to one category, e.g. government, education_exams",
    ),
    lang: str | None = Query(default=None, description="restrict to en, hi or kn"),
    highlight: bool = Query(
        default=False,
        description=(
            "wrap query terms in <b> tags inside snippets; off by default because "
            "both shipped clients render snippets as plain text and highlight locally"
        ),
    ),
) -> JSONResponse:
    query = require_query(q)
    service = get_service(request)

    try:
        category_filter = service.normalise_category(category)
        language_filter = service.normalise_language(lang)
    except ValueError as error:
        raise AppError("bad_request", str(error), 400, False) from error

    try:
        if format.lower() == "json":
            payload = await service.search_flat(
                query,
                limit=limit,
                category=category_filter,
                language=language_filter,
                highlight=highlight,
            )
        else:
            payload = await service.search(
                query,
                limit=limit,
                page=page,
                category=category_filter,
                language=language_filter,
                highlight=highlight,
            )
    except LibsqlError:
        raise
    except Exception as error:
        logger.exception("search failed for %r", query)
        raise AppError("internal", f"The search failed: {error}", 500, True) from error
    return JSONResponse(content=payload)


@app.get("/v1/suggest")
async def suggest(
    request: Request,
    q: str = Query(default=""),
    limit: int = Query(default=6, ge=1, le=20),
) -> JSONResponse:
    query = " ".join((q or "").split())
    if not query:
        return JSONResponse(content=["", []])
    service = get_service(request)
    return JSONResponse(content=await service.suggest(query, limit=limit))


async def _retrieve_payload(
    request: Request,
    raw_query: str,
    output_format: str,
    limit: int,
):
    query = require_query(raw_query)
    service = get_service(request)
    payload = await service.retrieve(query, limit=limit, format=output_format)
    if isinstance(payload, str):
        return PlainTextResponse(content=payload, media_type="text/markdown; charset=utf-8")
    return JSONResponse(content=payload)


@app.get("/v1/retrieve")
async def retrieve_get(
    request: Request,
    q: str = Query(default=""),
    format: str = Query(default="json"),
    limit: int = Query(default=10, ge=1, le=50),
    max_results: int | None = Query(default=None, ge=1, le=50),
):
    return await _retrieve_payload(request, q, format, max_results or limit)


@app.post("/v1/retrieve")
async def retrieve_post(request: Request, body: RetrieveRequest):
    return await _retrieve_payload(request, body.query, body.format, body.limit)


# ---------------------------------------------------------------------------
# Chat (server-sent events)
# ---------------------------------------------------------------------------


def compose_answer(query: str, results: list[dict[str, Any]]) -> str:
    """An extractive answer assembled only from indexed text.

    There is no model behind this endpoint, so it does not invent prose: it
    reports what the index holds and attributes each sentence to its source.
    """
    if not results:
        return (
            f"Nothing is indexed for **{query}** yet. The query has been left to the "
            "crawler; ask again once it has been fetched."
        )

    lines = [f"Here is what the index holds for **{query}**.", ""]
    for result in results[:3]:
        title = result.get("title") or result.get("url", "")
        snippet = (result.get("snippet") or "").strip()
        lines.append(f"**{title}**")
        if snippet:
            lines.append(snippet)
        lines.append(f"Source: {result.get('url', '')}")
        lines.append("")
    return "\n".join(lines).strip()


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/v1/chat")
async def chat(request: Request, body: ChatRequest) -> StreamingResponse:
    query = require_query(body.query)
    service = get_service(request)
    payload = await service.search(query, limit=5)
    results = payload.get("results", [])
    answer = compose_answer(query, results)
    sources = [{"title": item["title"], "url": item["url"]} for item in results[:5]]

    async def stream() -> AsyncIterator[str]:
        buffer: list[str] = []
        for token in answer.split(" "):
            buffer.append(token)
            if len(buffer) >= 6:
                yield sse({"delta": " ".join(buffer) + " "})
                buffer = []
                await asyncio.sleep(0)
        if buffer:
            yield sse({"delta": " ".join(buffer)})
        yield sse({"done": True, "sources": sources})

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


# ---------------------------------------------------------------------------
# Stats and OpenSearch
# ---------------------------------------------------------------------------


@app.get("/v1/stats")
async def stats(request: Request) -> JSONResponse:
    service = get_service(request)
    orchestrator = getattr(request.app.state, "orchestrator", None)
    payload: dict[str, Any] = await service.stats()
    if orchestrator is not None:
        state = await orchestrator.guard.storage_state()
        payload["storage"] = state.as_dict()
    payload["db_target"] = describe_database_target(getattr(request.app.state, "settings", get_settings()))
    return JSONResponse(content=payload)


def base_url_for(request: Request) -> str:
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    forwarded = request.headers.get("x-forwarded-proto")
    scheme = forwarded.split(",")[0].strip() if forwarded else request.url.scheme
    return f"{scheme}://{request.headers.get('host', request.url.netloc)}"


@app.get("/opensearch.xml")
async def opensearch(request: Request) -> Response:
    base = base_url_for(request)
    template = base + "/v1/search?q={searchTerms}"
    suggestions = base + "/v1/suggest?q={searchTerms}"
    document = f"""<?xml version="1.0" encoding="UTF-8"?>
<OpenSearchDescription xmlns="http://a9.com/-/spec/opensearch/1.1/">
  <ShortName>eyesearch</ShortName>
  <Description>Search the EyeSearch index</Description>
  <InputEncoding>UTF-8</InputEncoding>
  <Url type="text/html" method="GET" template="{template}"/>
  <Url type="application/x-suggestions+json" method="GET" template="{suggestions}"/>
</OpenSearchDescription>
"""
    return Response(
        content=document,
        media_type="application/opensearchdescription+xml; charset=utf-8",
    )


__all__ = ["app", "compose_answer", "lifespan", "sse"]
