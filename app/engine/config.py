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
    """Crawler and index policy."""

    # --- index routing ---------------------------------------------------
    # Minimum number of local hits before the index answers on its own.
    index_threshold: int = field(default_factory=lambda: _int("INDEX_THRESHOLD", 3))
    index_limit: int = field(default_factory=lambda: _int("INDEX_LIMIT", 20))
    # Weight of freshness in the blended score (0 = pure BM25).
    freshness_weight: float = field(default_factory=lambda: _float("INDEX_FRESHNESS_WEIGHT", 0.25))

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

    # --- content limits --------------------------------------------------
    max_content_chars: int = field(default_factory=lambda: _int("INDEX_MAX_CONTENT_CHARS", 20_000))
    max_snippet_chars: int = field(default_factory=lambda: _int("INDEX_MAX_SNIPPET_CHARS", 240))

    # --- backlog ---------------------------------------------------------
    backlog_max_per_query: int = field(default_factory=lambda: _int("BACKLOG_MAX_PER_QUERY", 50))


@lru_cache(maxsize=1)
def get_engine_settings() -> EngineSettings:
    return EngineSettings()
