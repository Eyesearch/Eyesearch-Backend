"""POST/GET /v1/retrieve — token-optimised context for AI and LLM clients."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Body, Query
from fastapi.responses import PlainTextResponse

from app.config import get_settings
from app.errors import BadRequestError
from app.schemas import ErrorEnvelope, RagResponse, RetrieveRequest, RetrieveResponse
from app.services import formatter
from app.services.search_service import get_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["retrieve"])

_MARKDOWN_DOC = {
    "content": {
        "text/markdown": {
            "schema": {"type": "string"},
            "example": "# espresso machine\n\n1. **Espresso - Wikipedia**\n   https://en.wikipedia.org/wiki/Espresso\n   Brewing method...\n",
        },
        "application/json": {"schema": RetrieveResponse.model_json_schema()},
    },
    "description": "Markdown by default; the JSON envelope when `format=json`.",
}


async def _respond(
    query: str, fmt: Literal["markdown", "json"], limit: int | None
) -> PlainTextResponse | RetrieveResponse:
    cleaned = " ".join(query.split())
    if not cleaned:
        raise BadRequestError("query must not be blank")
    settings = get_settings()
    effective = min(limit or settings.default_limit, settings.max_limit)
    service = get_service()

    if fmt == "json":
        return await service.retrieve_json(cleaned, limit=effective)

    results, acquisition = await service.retrieve(cleaned, limit=effective)
    markdown = formatter.to_markdown(
        acquisition.query, results, failed_providers=acquisition.failed_providers
    )
    return PlainTextResponse(markdown, media_type="text/markdown; charset=utf-8")


@router.post(
    "/v1/retrieve",
    summary="Clean Markdown or JSON context for an AI client",
    responses={
        200: _MARKDOWN_DOC,
        400: {"model": ErrorEnvelope, "description": "Malformed query"},
        503: {"model": ErrorEnvelope, "description": "Retrieval and cache both unavailable"},
    },
    response_model=None,
)
async def retrieve_post(
    payload: RetrieveRequest = Body(
        ...,
        examples=[
            {
                "query": "espresso machine buying guide",
                "format": "markdown",
                "limit": 5,
            }
        ],
    ),
) -> PlainTextResponse | RetrieveResponse:
    """Body: `{"query": "...", "format": "markdown"|"json", "limit": 10}`.

    Returns `text/markdown` by default (title, URL and the minimum prose per
    result, boilerplate stripped), or the JSON envelope when `format=json`.
    """
    return await _respond(payload.query, payload.format, payload.limit)


@router.get(
    "/v1/retrieve",
    summary="Token-optimised context for LLM agents (RAG)",
    response_model=None,
    responses={200: {"model": RagResponse}, 400: {"model": ErrorEnvelope}},
)
async def retrieve_get(
    q: str = Query(..., min_length=1, max_length=512, description="Search query"),
    max_results: int | None = Query(default=None, ge=1, le=50, description="Context blocks to return"),
    limit: int | None = Query(default=None, ge=1, le=50, description="Alias of max_results"),
    format: Literal["json", "markdown"] = Query(default="json"),
) -> RagResponse | PlainTextResponse:
    """`GET /v1/retrieve?q=...&max_results=5`

    Returns `{query, source, context: [{title, url, text}]}` — clean text, no
    HTML, no navigation, no boilerplate. `format=markdown` gives the same content
    as a markdown document for prompt assembly.
    """
    cleaned = " ".join(q.split())
    if not cleaned:
        raise BadRequestError("query must not be blank")

    settings = get_settings()
    effective = min(max_results or limit or settings.default_limit, settings.max_limit)
    service = get_service()

    if format == "markdown":
        results, acquisition = await service.retrieve(cleaned, limit=effective)
        return PlainTextResponse(
            formatter.to_markdown(acquisition.query, results, failed_providers=acquisition.failed_providers),
            media_type="text/markdown; charset=utf-8",
        )

    context, acquisition = await service.rag_context(cleaned, limit=effective)
    return RagResponse(query=acquisition.query, source=acquisition.source, context=context)
