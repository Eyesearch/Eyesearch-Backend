"""Background loop for the revalidation worker.

Started from the gateway's lifespan when `REVALIDATE_ENABLED` is true. One pass
at a time, a jittered sleep between passes, and an in-memory cooldown so a page
that came back unchanged is not re-fetched on the very next pass (unchanged
pages intentionally write nothing to the database, so there is nothing on disk
to remember them by).
"""

from __future__ import annotations

import asyncio
import logging
import random

from app.engine.config import EngineSettings, get_engine_settings
from app.indexer.backlog_worker import run_backlog_once
from app.indexer.revalidate import revalidate_batch

logger = logging.getLogger(__name__)

_task: asyncio.Task[None] | None = None
_backlog_task: asyncio.Task[None] | None = None
_cooldown: dict[str, int] = {}


async def run_once(limit: int | None = None) -> dict[str, int]:
    report = await revalidate_batch(limit)
    for url in list(_cooldown):
        _cooldown[url] -= 1
        if _cooldown[url] <= 0:
            _cooldown.pop(url, None)
    return dict(report)


async def _loop(settings: EngineSettings) -> None:
    while True:
        try:
            await run_once(settings.revalidate_batch_size)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bad pass must not kill the worker
            logger.warning("revalidation pass failed: %s", exc)
        # Jitter keeps a fleet of instances from waking at the same moment.
        await asyncio.sleep(max(30, settings.revalidate_interval_seconds) + random.uniform(0, 60))


def start(settings: EngineSettings | None = None) -> asyncio.Task[None] | None:
    """Start the loop in the running event loop (idempotent)."""
    global _task
    cfg = settings or get_engine_settings()
    if not cfg.revalidate_enabled or _task is not None:
        return _task
    _task = asyncio.create_task(_loop(cfg), name="eyesearch-revalidate")
    logger.info("revalidation worker started (every ~%ss)", cfg.revalidate_interval_seconds)
    return _task


async def _backlog_loop(settings: EngineSettings) -> None:
    """Resolve missed queries into indexed documents, off the request path."""
    while True:
        try:
            await run_backlog_once(settings.backlog_batch_queries)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bad pass must not kill the worker
            logger.warning("backlog pass failed: %s", exc)
        await asyncio.sleep(max(60, settings.backlog_interval_seconds) + random.uniform(0, 30))


def start_backlog_worker(settings: EngineSettings | None = None) -> asyncio.Task[None] | None:
    global _backlog_task
    cfg = settings or get_engine_settings()
    if not cfg.backlog_worker_enabled or _backlog_task is not None:
        return _backlog_task
    _backlog_task = asyncio.create_task(_backlog_loop(cfg), name="eyesearch-backlog")
    logger.info("backlog worker started (every ~%ss)", cfg.backlog_interval_seconds)
    return _backlog_task


async def stop_backlog_worker() -> None:
    global _backlog_task
    if _backlog_task is None:
        return
    _backlog_task.cancel()
    try:
        await _backlog_task
    except asyncio.CancelledError:
        pass
    _backlog_task = None
    logger.info("backlog worker stopped")


async def stop() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except asyncio.CancelledError:
        pass
    _task = None
    logger.info("revalidation worker stopped")


__all__ = ["start", "stop", "run_once", "start_backlog_worker", "stop_backlog_worker"]
