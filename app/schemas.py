"""Pydantic v2 request/response contract.

These models are the documented API surface: if a field is not here, the API
does not return it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchResult(BaseModel):
    """One result card for the human web UI."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(description="Stable per-result id, provider-prefixed.")
    title: str
    url: str
    snippet: str = ""
    source: str = Field(description="Provider that produced this result.")
    kind: Literal["web", "video", "image", "news", "film", "document", "other"] = "web"
    poster_url: str | None = Field(default=None, description="Absolute media poster URL.")
    thumbnail_url: str | None = Field(default=None, description="Absolute thumbnail URL.")
    published_at: str | None = None
    score: float = Field(default=0.0, description="Ranking score, higher is better.")


class SearchResponse(BaseModel):
    """Response for GET /v1/search."""

    query: str
    page: int = 1
    limit: int
    total_estimate: int = Field(description="Estimate of matching records, not an exact count.")
    took_ms: int
    degraded: bool = Field(description="True when at least one provider failed.")
    cache_hit: bool = False
    source: Literal["index", "cache", "live"] = Field(
        default="live", description="Where these results came from."
    )
    index_hits: int = Field(default=0, description="Documents matched in the EyeSearch index.")
    providers: dict[str, int] = Field(default_factory=dict)
    results: list[SearchResult] = Field(default_factory=list)


class RetrieveRequest(BaseModel):
    """Body for POST /v1/retrieve."""

    query: str = Field(min_length=1, max_length=512)
    format: Literal["markdown", "json"] = "markdown"
    limit: int = Field(default=10, ge=1, le=50)

    @field_validator("query")
    @classmethod
    def _strip(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("query must not be blank")
        return cleaned


class AIContextResult(BaseModel):
    """One result in the token-optimised AI payload (no presentation fields)."""

    title: str
    url: str
    snippet: str = ""


class RetrieveResponse(BaseModel):
    """JSON mode response for /v1/retrieve."""

    query: str
    format: Literal["json", "markdown"] = "json"
    degraded: bool = False
    source: Literal["index", "cache", "live"] = "live"
    index_hits: int = 0
    providers: dict[str, int] = Field(default_factory=dict)
    failed_providers: list[str] = Field(default_factory=list)
    results: list[AIContextResult] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Response for GET /health and GET /healthz."""

    ok: bool
    db: Literal["up", "down"]
    version: str
    environment: str
    uptime_s: int
    providers: dict[str, str] = Field(default_factory=dict)
    index: dict[str, int] = Field(default_factory=dict, description="Own-index health.")


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool


class ErrorEnvelope(BaseModel):
    """The one error shape both endpoints return."""

    error: ErrorDetail
