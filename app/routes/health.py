"""GET /health and GET /healthz — fast liveness probe, never touches the database."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app import database
from app.config import get_settings
from app.schemas import HealthResponse
from app.services.live_retrieval import provider_states

router = APIRouter(tags=["health"])
_STARTED_AT = time.monotonic()


def _health_response(request: Request) -> HealthResponse:
    """Return only cached process state; do not await Turso or worker tasks."""
    cfg = get_settings()
    state = request.app.state
    db_up = bool(getattr(state, "db_ready", False))
    try:
        providers = provider_states()
    except Exception:
        providers = {}
    index_stats = getattr(state, "index_stats", None) or {"documents": 0, "backlog": 0}
    target = database.describe_target(cfg)
    return HealthResponse(
        ok=db_up,
        db="up" if db_up else "down",
        db_target=f"{target['kind']}:{target['url']}",
        version=cfg.app_version,
        environment=cfg.environment,
        uptime_s=int(time.monotonic() - _STARTED_AT),
        providers=providers,
        index=index_stats,
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health(request: Request) -> HealthResponse:
    return _health_response(request)


@router.get("/v1/stats", summary="Live index statistics")
async def stats() -> dict[str, object]:
    """Counts read straight from the database on every call — never cached."""
    from app.engine.store import get_store

    from app.main import app

    store = get_store()
    try:
        live = await store.stats()
    except Exception:
        live = {"documents": 0, "sites": 0, "pages": 0, "backlog": 0, "max_payload_bytes": 0}
    try:
        pending_domains = await store.pending_domain_count()
    except Exception:
        pending_domains = 0
    try:
        from app.indexer.guard import storage_state

        storage = await storage_state(store)
    except Exception:
        storage = {"paused": False}
    return {
        "writable": getattr(app.state, "db_writable", None),
        "sites": live.get("sites", live.get("documents", 0)),
        "pages": live.get("pages", 0),
        "indexed": live.get("pages", 0),
        "query_backlog": live.get("backlog", 0),
        "discovered_backlog": pending_domains,
        "max_payload_bytes": live.get("max_payload_bytes", 0),
        "storage": {
            "pages": storage.get("pages", 0),
            "limit": storage.get("limit", 0),
            "over_limit": storage.get("over_limit", False),
            "quota_tripped": storage.get("quota_tripped", False),
            "paused": storage.get("paused", False),
        },
        "source": "database",
    }


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe (alias)")
async def healthz(request: Request) -> HealthResponse:
    return _health_response(request)
