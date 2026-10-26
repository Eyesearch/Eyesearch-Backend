"""EyeSearch Gateway — FastAPI application wiring.

`lifespan` owns the two shared resources (Turso client, httpx client) so nothing
is created per request. Startup never hard-fails: if Turso is unreachable the
service still boots, `/health` reports `db: down`, and requests degrade to live
retrieval.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import database
from app.config import Settings, get_settings
from app.errors import AppError, DatabaseError, error_envelope
from app.indexer import scheduler as revalidate_scheduler
from app.routes import health, opensearch, retrieve, search
from app.services import live_retrieval, search_service

logger = logging.getLogger(__name__)


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )


def _build_http_client(settings: Settings) -> httpx.AsyncClient:
    """One shared client for the process: pooled, bounded, identifiable."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(
            connect=settings.http_connect_timeout_s,
            read=settings.http_read_timeout_s,
            write=settings.http_write_timeout_s,
            pool=settings.http_pool_timeout_s,
        ),
        limits=httpx.Limits(
            max_connections=settings.http_max_connections,
            max_keepalive_connections=settings.http_max_keepalive,
        ),
        headers={"User-Agent": live_retrieval.USER_AGENT},
        follow_redirects=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    _configure_logging(settings)
    target = database.describe_target(settings)
    logger.info(
        "starting %s v%s (%s, db=%s %s)",
        settings.app_name,
        settings.app_version,
        settings.environment,
        target["kind"],
        target["url"],
    )

    # Production must write to Turso. A silent fall back to a local file is the
    # exact failure this check exists to prevent: search keeps working from the
    # live providers while the cloud database sees no reads and no writes.
    if settings.requires_remote_db and not settings.is_remote_db:
        message = (
            f"refusing to start: environment={settings.environment} but the database is a "
            f"local file ({target['url']}). Set TURSO_DB_URL/TURSO_DATABASE_URL and "
            "TURSO_AUTH_TOKEN, or set ALLOW_LOCAL_DB=true if that is genuinely intended."
        )
        logger.critical(message)
        raise RuntimeError(message)

    http_client = _build_http_client(settings)
    app.state.http = http_client
    app.state.db_ready = False
    app.state.db_error = ""

    try:
        await database.connect(settings)
        await database.initialize_schema()
        await database.probe()
        app.state.db_ready = True
        logger.info("database ready (%s): %s", target["kind"], target["url"])
    except DatabaseError as exc:
        app.state.db_error = str(exc)
        logger.error("database unavailable at startup: %s", exc)
        if settings.requires_remote_db:
            # In production, booting without the database means every write is
            # lost. Fail instead of serving results that are never indexed.
            raise RuntimeError(f"database unavailable in production: {exc}") from exc

    if app.state.db_ready:
        try:
            from app.engine.store import get_store

            stats = await get_store().stats()
            logger.info(
                "index: %d documents, %d queued queries (%s)",
                stats.get("documents", 0),
                stats.get("backlog", 0),
                target["url"],
            )
        except Exception as exc:
            logger.warning("index stats unavailable at startup: %s", exc)

    if app.state.db_ready:
        try:
            purged = await database.purge_expired()
            if purged:
                logger.info("purged %d expired cache rows", purged)
        except DatabaseError as exc:
            logger.warning("cache purge skipped: %s", exc)

    search_service.configure(http_client, settings)

    # Both workers run off the request path, one pass at a time:
    #   - revalidation keeps indexed pages fresh
    #   - the backlog worker turns missed queries into indexed documents
    revalidate_scheduler.start()
    revalidate_scheduler.start_backlog_worker()

    try:
        yield
    finally:
        await revalidate_scheduler.stop_backlog_worker()
        await revalidate_scheduler.stop()
        await search_service.drain_background_writes()
        await http_client.aclose()
        await database.close()
        logger.info("shutdown complete")


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "Unified Search & Retrieval Gateway. `GET /v1/search` serves the "
            "EyeSearch web UI with structured cards; `POST /v1/retrieve` serves "
            "AI clients with token-optimised Markdown or JSON."
        ),
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.allow_credentials,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["X-Response-Time-Ms"],
        max_age=600,
    )

    application.include_router(health.router)
    application.include_router(opensearch.router)
    application.include_router(search.router)
    application.include_router(retrieve.router)

    @application.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("%s %s -> %s: %s", request.method, request.url.path, exc.code, exc.message)
        return JSONResponse(status_code=exc.status_code, content=exc.envelope())

    @application.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ()))}: {error.get('msg', '')}"
            for error in exc.errors()[:4]
        )
        return JSONResponse(
            status_code=400,
            content=error_envelope("bad_request", detail or "invalid request", False),
        )

    @application.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content=error_envelope("internal", "internal server error", False),
        )

    @application.get("/", tags=["meta"], summary="Service metadata")
    async def root() -> dict[str, object]:
        return {
            "service": settings.app_name,
            "version": settings.app_version,
            "environment": settings.environment,
            "endpoints": {
                "search": "GET /v1/search?q=&limit=&page=",
                "retrieve": "POST /v1/retrieve {query, format, limit}",
                "health": "GET /health",
                "docs": "GET /docs",
            },
        }

    return application


app = create_app()


if __name__ == "__main__":  # local convenience: python -m app.main
    import uvicorn

    cfg = get_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=cfg.port, log_level=cfg.log_level.lower())
