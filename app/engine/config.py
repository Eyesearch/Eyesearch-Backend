"""Engine knobs, read from the environment with working defaults.

Kept separate from the gateway's own settings so the crawler can be tuned (or
switched off entirely) without touching request-path configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class EngineSettings:
    """Storage budget, crawl policy and revalidation cadence."""

    # --- index routing ---------------------------------------------------
    index_threshold: int = field(default_factory=lambda: _int("INDEX_THRESHOLD", 1))
    index_limit: int = field(default_factory=lambda: _int("INDEX_LIMIT", 20))
    freshness_weight: float = field(default_factory=lambda: _float("INDEX_FRESHNESS_WEIGHT", 0.25))
    # Live-fetch-once: the first query for a topic calls the providers, the
    # results are ingested, and every later query is answered from the index.
    # Set false for a strictly local-only deployment.
    live_search_enabled: bool = field(default_factory=lambda: _bool("LIVE_SEARCH_ENABLED", True))
    # Write the live results into documents_fts so the next query is local.
    auto_ingest_enabled: bool = field(default_factory=lambda: _bool("AUTO_INGEST_ENABLED", True))
    auto_ingest_max_results: int = field(default_factory=lambda: _int("AUTO_INGEST_MAX_RESULTS", 5))

    # --- storage budget (1-2 KB per page) --------------------------------
    max_payload_bytes: int = field(default_factory=lambda: _int("INDEX_MAX_PAYLOAD_BYTES", 2048))
    title_max_chars: int = field(default_factory=lambda: _int("INDEX_TITLE_MAX_CHARS", 100))
    snippet_max_chars: int = field(default_factory=lambda: _int("INDEX_SNIPPET_MAX_CHARS", 200))
    # The FTS5 `content` column only needs enough text to match on.
    content_max_chars: int = field(default_factory=lambda: _int("INDEX_CONTENT_MAX_CHARS", 1200))

    # --- dedupe and domain policy ----------------------------------------
    max_urls_per_domain: int = field(default_factory=lambda: _int("INDEX_MAX_URLS_PER_DOMAIN", 12))
    max_path_depth: int = field(default_factory=lambda: _int("INDEX_MAX_PATH_DEPTH", 2))

    # --- crawling --------------------------------------------------------
    crawl_enabled: bool = field(default_factory=lambda: _bool("CRAWL_ENABLED", True))
    crawl_max_pages_per_seed: int = field(default_factory=lambda: _int("CRAWL_MAX_PAGES_PER_SEED", 20))
    crawl_max_depth: int = field(default_factory=lambda: _int("CRAWL_MAX_DEPTH", 1))
    crawl_concurrency: int = field(default_factory=lambda: _int("CRAWL_CONCURRENCY", 3))
    crawl_delay_seconds: float = field(default_factory=lambda: _float("CRAWL_DELAY_SECONDS", 1.0))
    crawl_user_agent: str = field(
        default_factory=lambda: os.environ.get(
            "CRAWL_USER_AGENT",
            "EyeSearchBot/1.0 (+https://github.com/Eyesearch/Eyesearch-Backend; indexing Indian web)",
        )
    )
    crawl_timeout_seconds: float = field(default_factory=lambda: _float("CRAWL_TIMEOUT_SECONDS", 8.0))
    crawl_max_bytes: int = field(default_factory=lambda: _int("CRAWL_MAX_BYTES", 512_000))
    respect_robots: bool = field(default_factory=lambda: _bool("CRAWL_RESPECT_ROBOTS", True))

    # --- stale-while-revalidate ------------------------------------------
    revalidate_enabled: bool = field(default_factory=lambda: _bool("REVALIDATE_ENABLED", True))
    revalidate_interval_seconds: int = field(default_factory=lambda: _int("REVALIDATE_INTERVAL_SECONDS", 3600))
    revalidate_batch_size: int = field(default_factory=lambda: _int("REVALIDATE_BATCH_SIZE", 10))
    # Domain classes, in days.
    news_ttl_days: float = field(default_factory=lambda: _float("REVALIDATE_NEWS_DAYS", 1.5))
    government_ttl_days: float = field(default_factory=lambda: _float("REVALIDATE_GOV_DAYS", 7))
    static_ttl_days: float = field(default_factory=lambda: _float("REVALIDATE_STATIC_DAYS", 21))

    # --- suggest ---------------------------------------------------------
    suggest_limit: int = field(default_factory=lambda: _int("SUGGEST_LIMIT", 8))

    # --- backlog ---------------------------------------------------------
    backlog_max_per_query: int = field(default_factory=lambda: _int("BACKLOG_MAX_PER_QUERY", 50))
    # Autonomous worker: pops pending queries, crawls for them, ingests the result.
    backlog_worker_enabled: bool = field(default_factory=lambda: _bool("BACKLOG_WORKER_ENABLED", True))
    backlog_interval_seconds: int = field(default_factory=lambda: _int("BACKLOG_INTERVAL_SECONDS", 900))
    backlog_batch_queries: int = field(default_factory=lambda: _int("BACKLOG_BATCH_QUERIES", 3))
    backlog_urls_per_query: int = field(default_factory=lambda: _int("BACKLOG_URLS_PER_QUERY", 5))
    # Discovery uses the external provider *off* the request path to find which
    # URLs to crawl for a missed query. Never called while serving a request.
    backlog_discovery_enabled: bool = field(default_factory=lambda: _bool("BACKLOG_DISCOVERY_ENABLED", True))


@lru_cache(maxsize=1)
def get_engine_settings() -> EngineSettings:
    return EngineSettings()
