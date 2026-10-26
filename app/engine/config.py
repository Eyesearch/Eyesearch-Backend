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
    # Per page, and per domain across its root and sitelinks (the brief's 10 KB).
    max_payload_bytes: int = field(default_factory=lambda: _int("INDEX_MAX_PAYLOAD_BYTES", 4096))
    max_site_payload_bytes: int = field(default_factory=lambda: _int("INDEX_MAX_SITE_PAYLOAD_BYTES", 10240))
    title_max_chars: int = field(default_factory=lambda: _int("INDEX_TITLE_MAX_CHARS", 100))
    snippet_max_chars: int = field(default_factory=lambda: _int("INDEX_SNIPPET_MAX_CHARS", 200))
    # The FTS5 `content` column only needs enough text to match on.
    # 3–4 sentences of dense summary, hard-capped at 1.5 KB.
    content_max_chars: int = field(default_factory=lambda: _int("INDEX_CONTENT_MAX_CHARS", 1500))
    summary_max_sentences: int = field(default_factory=lambda: _int("INDEX_SUMMARY_SENTENCES", 4))

    # --- category seed crawler -------------------------------------------
    seed_crawl_enabled: bool = field(default_factory=lambda: _bool("SEED_CRAWL_ENABLED", True))
    # The self-healing refresh: every 24 hours by default.
    seed_crawl_interval_seconds: int = field(default_factory=lambda: _int("SEED_CRAWL_INTERVAL_SECONDS", 86400))
    # Below this many sites the index counts as uncovered and boot re-seeds it.
    seed_coverage_min_sites: int = field(default_factory=lambda: _int("SEED_COVERAGE_MIN_SITES", 5))
    # What "covered" means: the share of *registry seeds* present in `sites`.
    # 1.0 means every seed in seed_registry.py must be indexed.
    seed_coverage_min_ratio: float = field(default_factory=lambda: _float("SEED_COVERAGE_MIN_RATIO", 1.0))
    # Continuous discovery: batches of ten, a pause between batches, and a hard
    # ceiling on indexed sites (well inside the free Turso allowance).
    discovery_batch_size: int = field(default_factory=lambda: _int("DISCOVERY_BATCH_SIZE", 10))
    discovery_batch_pause_seconds: float = field(default_factory=lambda: _float("DISCOVERY_BATCH_PAUSE_SECONDS", 0.5))
    # How often the discovery worker drains a batch, independently of the seeds.
    discovery_interval_seconds: int = field(default_factory=lambda: _int("DISCOVERY_INTERVAL_SECONDS", 300))
    max_indexed_sites: int = field(default_factory=lambda: _int("MAX_INDEXED_SITES", 50000))
    seed_crawl_pages_per_domain: int = field(default_factory=lambda: _int("SEED_CRAWL_PAGES_PER_DOMAIN", 5))
    # Domains crawled at once: bounded so the target sites and the single Turso
    # connection both stay stable.
    seed_crawl_concurrency: int = field(default_factory=lambda: _int("SEED_CRAWL_CONCURRENCY", 5))

    # --- relevance -------------------------------------------------------
    # A single-word query must appear in the title, URL or snippet — never only
    # in the body — or the hit is rejected and the caller falls back.
    relevance_require_strong_field: bool = field(
        default_factory=lambda: _bool("RELEVANCE_REQUIRE_STRONG_FIELD", True)
    )
    # Fraction of a query's words that must appear in title/URL/snippet.
    relevance_min_token_coverage: float = field(
        default_factory=lambda: _float("RELEVANCE_MIN_TOKEN_COVERAGE", 0.6)
    )

    # --- on-demand extraction pipeline ------------------------------------
    on_demand_max_candidates: int = field(default_factory=lambda: _int("ON_DEMAND_MAX_CANDIDATES", 8))
    on_demand_max_fetches: int = field(default_factory=lambda: _int("ON_DEMAND_MAX_FETCHES", 5))
    on_demand_concurrency: int = field(default_factory=lambda: _int("ON_DEMAND_CONCURRENCY", 3))
    on_demand_budget_seconds: float = field(default_factory=lambda: _float("ON_DEMAND_BUDGET_SECONDS", 12.0))

    # --- dedupe and domain policy ----------------------------------------
    max_urls_per_domain: int = field(default_factory=lambda: _int("INDEX_MAX_URLS_PER_DOMAIN", 12))
    # Sitelinks kept per domain root.
    sections_max: int = field(default_factory=lambda: _int("INDEX_SECTIONS_MAX", 5))
    # Root and level-1 pages only (Wikipedia articles are the documented exception).
    max_path_depth: int = field(default_factory=lambda: _int("INDEX_MAX_PATH_DEPTH", 1))

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
    crawl_timeout_seconds: float = field(default_factory=lambda: _float("CRAWL_TIMEOUT_SECONDS", 5.0))
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
