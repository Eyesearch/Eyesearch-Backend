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

import asyncio
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
_STORAGE_CACHE_TTL_SECONDS = 60.0
_STORAGE_CACHE: dict[str, tuple[float, int, int]] = {}
_STORAGE_CACHE_LOCK = asyncio.Lock()


def _storage_target_key(store: Any, settings: Any) -> str:
    """Key cached counts by the actual database target, not just a wrapper object."""
    for owner in (store, getattr(store, "settings", None), settings):
        if owner is None:
            continue
        for name in ("database_url", "turso_url", "libsql_url", "db_url", "url"):
            value = getattr(owner, name, None)
            if value:
                return str(value).rstrip("/")
    try:
        from app.database import get_client

        client = get_client()
        for name in ("_base", "endpoint", "url", "database_url"):
            value = getattr(client, name, None)
            if value:
                return str(value).rstrip("/")
    except Exception:
        pass
    return f"store:{id(store)}"


def invalidate_storage_cache(store: Any | None = None, settings: Any | None = None) -> None:
    """Invalidate cached counts after a committed write."""
    if store is None:
        _STORAGE_CACHE.clear()
        return
    cfg = settings or getattr(store, "settings", None) or get_engine_settings()
    _STORAGE_CACHE.pop(_storage_target_key(store, cfg), None)


async def _cached_storage_counts(store: Any, settings: Any) -> tuple[int, int]:
    key = _storage_target_key(store, settings)
    async with _STORAGE_CACHE_LOCK:
        cached = _STORAGE_CACHE.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1], cached[2]
        pages = int(await store.count_pages())
        sites = int(await store.count_documents())
        if pages < 0 or sites < 0:
            raise ValueError("negative storage count returned")
        _STORAGE_CACHE[key] = (
            time.monotonic() + _STORAGE_CACHE_TTL_SECONDS,
            pages,
            sites,
        )
        return pages, sites


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
        pages, sites = await _cached_storage_counts(index, cfg)
    except Exception as exc:
        logger.error("storage check failed; refusing writes: %s", exc)
        return {
            "pages": None,
            "sites": None,
            "limit": cfg.storage_page_limit,
            "over_limit": True,
            "quota_tripped": _breaker.tripped,
            "quota_reason": _breaker.reason,
            "paused": True,
            "error": str(exc),
        }

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
    """Ask once, at the start of a pass, failing closed on every exception."""
    try:
        state = await storage_state(store, settings)
        if state.get("error"):
            return True
        if state["over_limit"]:
            logger.warning(STORAGE_WARNING)
            return True
        if state["quota_tripped"] and quota_paused():
            logger.warning(
                "storage quota breaker open (%s) — skipping this pass", state["quota_reason"]
            )
            return True
        return bool(state.get("paused", False))
    except Exception as exc:
        logger.error("storage pause check failed; refusing writes: %s", exc)
        return True


__all__ = [
    "STORAGE_WARNING",
    "QUOTA_MARKERS",
    "is_quota_error",
    "trip_quota",
    "reset_quota",
    "quota_paused",
    "storage_state",
    "storage_paused",
    "invalidate_storage_cache",
]
