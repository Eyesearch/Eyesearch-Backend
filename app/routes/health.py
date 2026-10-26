"""GET /health and GET /healthz — liveness and dependency status. Never raises."""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter

from app import database
from app.config import get_settings
from app.schemas import HealthResponse
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
    return HealthResponse(
        ok=db_up,
        db="up" if db_up else "down",
        version=cfg.app_version,
        environment=cfg.environment,
        uptime_s=int(time.monotonic() - _STARTED_AT),
        providers=providers,
    )


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    return await _health()


@router.get("/healthz", response_model=HealthResponse, summary="Liveness probe (alias)")
async def healthz() -> HealthResponse:
    return await _health()
