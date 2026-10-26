"""GET /health and GET /healthz — liveness and dependency status. Never raises."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter

from app import database
from app.config import get_settings
from app.schemas import HealthResponse
from app.engine.store import get_store
from app.services.live_retrieval import provider_states

logger = logging.getLogger(__name__)
router = APIRouter(tags=["health"])

_STARTED_AT = time.monotonic()


async def _health() -> HealthResponse:
    cfg = get_settings()
    try:
        db_up = await database.ping()
    except Exception as exc:  # ping already swallows, this is belt and braces
        logger.warning("health ping error: %s", exc)
        db_up = False
    try:
        providers = provider_states()
    except Exception:  # pragma: no cover - defensive
        providers = {}
    try:
        index_stats = await get_store().stats() if db_up else {"documents": 0, "backlog": 0}
    except Exception:  # pragma: no cover - defensive
        index_stats = {"documents": 0, "backlog": 0}
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
async def health() -> HealthResponse:
    return await _health()


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe (alias)")
async def healthz() -> HealthResponse:
    return await _health()
