"""Storage safety: stop crawling before the database bill does.

Two independent brakes:

* a **record ceiling** — before any pass starts, the crawler asks how many pages
  the index holds and pauses when it is over `STORAGE_PAGE_LIMIT` (300,000 by
  default, roughly 3.5–4 GB at our row sizes);
* a **quota breaker** — when Turso answers a write with a storage/quota error,
  the breaker trips and every scheduled pass returns immediately until the
  cooldown expires, so nothing tight-loops against a full database.

Neither brake touches the read path: the search API keeps serving.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.engine.config import get_engine_settings

logger = logging.getLogger(__name__)

# Substrings that mean "the database will not take more data".
QUOTA_MARKERS: tuple[str, ...] = (
    "storage quota",
    "quota exceeded",
    "database full",
    "database or disk is full",
    "sqlite_full",
    "storage limit",
    "no space left",
    "too many bytes",
    "exceeded the storage",
)

STORAGE_WARNING = (
    "WARNING: Storage safety threshold reached (300k+ records). Pausing autonomous crawling."
)


def is_quota_error(error: Any) -> bool:
    """True when a driver error means the database is out of space."""
    text = str(error).lower()
    return any(marker in text for marker in QUOTA_MARKERS)


@dataclass
class _Breaker:
    tripped_at: float = 0.0
    reason: str = ""

    @property
    def tripped(self) -> bool:
        return self.tripped_at > 0.0

    def elapsed(self) -> float:
        return time.monotonic() - self.tripped_at if self.tripped else 0.0


_breaker = _Breaker()


def trip_quota(reason: str) -> None:
    """Stop scheduling fetches after a quota failure. Logged once, loudly."""
    if _breaker.tripped:
        return
    _breaker.tripped_at = time.monotonic()
    _breaker.reason = reason
    logger.error(
        "STORAGE QUOTA EXHAUSTED: %s — autonomous crawling is paused; the search API "
        "continues to serve from the existing index. Raise the Turso plan or prune rows, "
        "then restart the service to resume.",
        reason,
    )


def reset_quota() -> None:
    global _breaker
    _breaker = _Breaker()


def quota_paused() -> bool:
    """Whether the breaker is currently holding the crawler back."""
    if not _breaker.tripped:
        return False
    cooldown = max(60, get_engine_settings().storage_pause_seconds)
    if _breaker.elapsed() >= cooldown:
        logger.info("storage quota cooldown elapsed — resuming crawling")
        reset_quota()
        return False
    return True


async def storage_state(
    store: Any | None = None,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Pages, sites, the ceiling and whether crawling should be paused."""
    from app.engine.store import get_store

    index = store or get_store()
    cfg = settings or get_engine_settings()
    try:
        pages = await index.count_pages()
        sites = await index.count_documents()
    except Exception as exc:
        logger.warning("storage check failed: %s", exc)
        pages, sites = 0, 0

    over_limit = pages >= cfg.storage_page_limit
    paused = over_limit or quota_paused()
    return {
        "pages": pages,
        "sites": sites,
        "limit": cfg.storage_page_limit,
        "over_limit": over_limit,
        "quota_tripped": _breaker.tripped,
        "quota_reason": _breaker.reason,
        "paused": paused,
    }


async def storage_paused(store: Any | None = None, settings: Any | None = None) -> bool:
    """Ask once, at the start of a pass, whether to crawl at all."""
    state = await storage_state(store, settings)
    if state["over_limit"]:
        logger.warning(STORAGE_WARNING)
        return True
    if state["quota_tripped"]:
        if quota_paused():
            logger.warning(
                "storage quota breaker open (%s) — skipping this pass", state["quota_reason"]
            )
            return True
    return False


__all__ = [
    "STORAGE_WARNING",
    "QUOTA_MARKERS",
    "is_quota_error",
    "trip_quota",
    "reset_quota",
    "quota_paused",
    "storage_state",
    "storage_paused",
]
