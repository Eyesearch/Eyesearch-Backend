"""GET /v1/search — structured cards for the EyeSearch web UI."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query

from app.config import get_settings
from app.errors import BadRequestError
from app.schemas import ErrorEnvelope, MetaSearchResponse, SearchResponse
from app.services.search_service import get_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["search"])


@router.get(
    "/v1/search",
    response_model=SearchResponse | MetaSearchResponse,
    summary="Search the index and the live web",
    responses={
        400: {"model": ErrorEnvelope, "description": "Malformed query"},
        503: {"model": ErrorEnvelope, "description": "Every provider and the cache failed"},
    },
)
async def search(
    q: str = Query(..., min_length=1, max_length=512, description="Search query"),
    limit: int | None = Query(
        default=None, ge=1, le=50, description="Results per page (server cap: MAX_LIMIT)"
    ),
    page: int = Query(default=1, ge=1, le=20, description="1-based page number"),
    format: str = Query(
        default="cards",
        pattern="^(cards|json)$",
        description="`cards` for the web UI, `json` for external aggregators",
    ),
):
    """Cache-first search. On a miss, live DuckDuckGo retrieval (region in-en)
    fills the result set and is persisted to Turso in the background.

    Response: `query`, `page`, `limit`, `total_estimate`, `took_ms`, `degraded`,
    `cache_hit`, `providers`, `results[]` (id, title, url, snippet, source, kind,
    poster_url, thumbnail_url, published_at, score).
    """
    cleaned = " ".join(q.split())
    if not cleaned:
        raise BadRequestError("query must not be blank")
    settings = get_settings()
    effective = min(limit or settings.default_limit, settings.max_limit)

    if format == "json":
        # The meta-search contract: flat, favicon-bearing, no presentation fields.
        return await get_service().meta_search(cleaned, limit=effective)

    return await get_service().search(cleaned, limit=effective, page=page)
