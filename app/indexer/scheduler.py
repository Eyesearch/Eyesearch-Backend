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
from app.indexer.crawler import run_autonomous_seed_crawl, run_discovery_pass, run_url_pass
from app.indexer.revalidate import revalidate_batch

logger = logging.getLogger(__name__)
URL_BATCH_THROTTLE_SECONDS = 2.0

_task: asyncio.Task[None] | None = None
_backlog_task: asyncio.Task[None] | None = None
_seed_task: asyncio.Task[None] | None = None
_discovery_task: asyncio.Task[None] | None = None
_url_queue_task: asyncio.Task[None] | None = None
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
        checked = 0
        try:
            report = await run_once(settings.revalidate_batch_size)
            checked = int(report.get("checked", 0) or 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bad pass must not kill the worker
            logger.warning("revalidation pass failed: %s", exc)

        if checked:
            # Jitter keeps a fleet of instances from waking at the same moment.
            await _sleep_logged(
                max(30, settings.revalidate_interval_seconds) + random.uniform(0, 60),
                "revalidation worker", "next pass",
            )
        else:
            await _sleep_logged(
                _idle_seconds(settings), "revalidation worker", "nothing stale"
            )


def start(settings: EngineSettings | None = None) -> asyncio.Task[None] | None:
    """Start the loop in the running event loop (idempotent)."""
    global _task
    cfg = settings or get_engine_settings()
    if not cfg.revalidate_enabled or _task is not None:
        return _task
    _task = asyncio.create_task(_loop(cfg), name="eyesearch-revalidate")
    logger.info("revalidation worker started (every ~%ss)", cfg.revalidate_interval_seconds)
    return _task


def _idle_seconds(settings: EngineSettings) -> float:
    """The minimum gap between two database polls when a worker has no work."""
    return max(15.0, float(settings.worker_idle_backoff_seconds))


async def _sleep_logged(seconds: float, worker: str, reason: str) -> None:
    """Sleep, saying so — a silent worker looks like a hung one."""
    logger.info("%s sleeping %.0fs (%s)", worker, seconds, reason)
    await asyncio.sleep(seconds)
    logger.info("%s awake", worker)


async def _refill_if_low(settings: EngineSettings) -> None:
    """Top the queue up when it runs dry, and say how much it gained."""
    from app.indexer.crawler import refill_frontier

    try:
        frontier_depth = await _queue_depth()
        if frontier_depth >= settings.frontier_low_water:
            return
        logger.info(
            "frontier low (%d < %d): refilling before the next pass",
            frontier_depth, settings.frontier_low_water,
        )
        result = await refill_frontier(settings=settings)
        logger.info(
            "INFO: Frontier refill completed: added %d URLs to crawl_queue. Depth is now %d",
            int(result.get("after", 0)) - int(result.get("before", 0)),
            int(result.get("after", 0)),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("frontier refill failed: %s", exc)


async def _queue_depth() -> int:
    from app.engine.store import get_store

    try:
        return await get_store().queue_depth()
    except Exception:
        return 0


def _backlog_sleep_seconds(report: dict, settings: EngineSettings) -> float:
    """How long the backlog worker waits before its next pass.

    A pass that filled its batch means work is still queued: retry soon. Only an
    empty or partial batch is worth the full interval.
    """
    processed = int(report.get("queries", 0) or 0)
    if processed >= max(1, settings.backlog_batch_queries):
        return 30.0
    if processed:
        return 60.0
    return float(max(60, settings.backlog_interval_seconds)) + random.uniform(0, 30)


async def _backlog_loop(settings: EngineSettings) -> None:
    """Resolve missed queries into indexed documents, off the request path."""
    while True:
        processed = 0
        try:
            report = await run_backlog_once(settings.backlog_batch_queries)
            processed = int(report.get("queries", 0) or 0)
            if processed:
                logger.info("backlog worker: %s", dict(report))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a bad pass must not kill the worker
            logger.warning("backlog pass failed: %s", exc)

        if processed:
            await _sleep_logged(
                _backlog_sleep_seconds({"queries": processed}, settings),
                "backlog worker",
                "work remains" if processed >= max(1, settings.backlog_batch_queries) else "next pass",
            )
        else:
            await _sleep_logged(_idle_seconds(settings), "backlog worker", "nothing pending")


def start_backlog_worker(settings: EngineSettings | None = None) -> asyncio.Task[None] | None:
    global _backlog_task
    cfg = settings or get_engine_settings()
    if not cfg.backlog_worker_enabled or _backlog_task is not None:
        return _backlog_task
    _backlog_task = asyncio.create_task(_backlog_loop(cfg), name="eyesearch-backlog")
    logger.info("backlog worker started (every ~%ss)", cfg.backlog_interval_seconds)
    return _backlog_task


async def _seed_loop(settings: EngineSettings) -> None:
    """Keep the foundational categories warm, one pass at a time."""
    while True:
        try:
            # The same entry point as boot: it checks coverage, crawls when the
            # index has drifted, and never raises out of the loop.
            await run_autonomous_seed_crawl()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("seed pass failed: %s", exc)
        from app.indexer.guard import storage_paused

        if await storage_paused():
            # Paused for storage: wake on the pause interval, not the crawl one.
            await asyncio.sleep(max(600, settings.storage_pause_seconds))
            continue
        await asyncio.sleep(max(600, settings.seed_crawl_interval_seconds) + random.uniform(0, 120))


def start_seed_crawler(settings: EngineSettings | None = None, *, initial_pass: bool = False) -> asyncio.Task[None] | None:
    """Start the category crawler. `initial_pass` populates an empty index at boot."""
    global _seed_task
    cfg = settings or get_engine_settings()
    if not cfg.seed_crawl_enabled or _seed_task is not None:
        return _seed_task

    async def runner() -> None:
        if initial_pass:
            try:
                logger.info("initial autonomous seed crawl starting")
                await run_autonomous_seed_crawl()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("initial autonomous crawl failed: %s", exc)
        await _seed_loop(cfg)

    _seed_task = asyncio.create_task(runner(), name="eyesearch-seed-crawler")
    logger.info("seed crawler started (every ~%ss)", cfg.seed_crawl_interval_seconds)
    return _seed_task


async def _url_queue_loop(settings: EngineSettings) -> None:
    """Continuously drain sitemap/frontier URLs with bounded polling frequency."""
    while True:
        try:
            from app.indexer.guard import storage_paused

            if await storage_paused():
                await _sleep_logged(
                    max(300, settings.storage_pause_seconds),
                    "URL queue worker",
                    "storage paused",
                )
            else:
                report = await run_url_pass(settings=settings)
                attempted = int(report.get("attempted", 0) or 0)
                processed = int(report.get("processed", 0) or 0)
                failed = int(report.get("failed", 0) or 0)
                if attempted or failed or report.get("error"):
                    logger.info("URL queue worker: %s", report)
                if report.get("error") and not attempted:
                    # Poll failures need a longer cooldown before another Turso request.
                    await asyncio.sleep(15.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Include traceback and exact exception; never retry in a tight loop.
            logger.exception("URL queue worker iteration failed")
            await asyncio.sleep(15.0)
        finally:
            # Mandatory throttle for every iteration, including empty and failed batches.
            await asyncio.sleep(URL_BATCH_THROTTLE_SECONDS)


def start_url_queue_worker(settings: EngineSettings | None = None) -> asyncio.Task[None] | None:
    """Start the URL queue drainer independently of discovery/seed feature flags."""
    global _url_queue_task
    cfg = settings or get_engine_settings()
    if _url_queue_task is not None:
        return _url_queue_task
    _url_queue_task = asyncio.create_task(_url_queue_loop(cfg), name="eyesearch-url-queue")
    logger.info("URL queue worker started (batch size 10; idle poll 15s)")
    return _url_queue_task


async def stop_url_queue_worker() -> None:
    global _url_queue_task
    if _url_queue_task is None:
        return
    _url_queue_task.cancel()
    try:
        await _url_queue_task
    except asyncio.CancelledError:
        pass
    _url_queue_task = None
    logger.info("URL queue worker stopped")


async def _discovery_loop(settings: EngineSettings) -> None:
    """Drain the discovered-domain backlog continuously, independently of seeds.

    Without this the queue only moved after a full seed pass, which on a free
    instance could take longer than the instance lived.
    """
    while True:
        await _refill_if_low(settings)
        report: dict = {}
        try:
            report = await run_discovery_pass(settings=settings)
            if report.get("attempted"):
                logger.info("discovery worker: %s", report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("discovery worker failed: %s", exc)

        from app.indexer.guard import storage_paused

        if await storage_paused():
            await _sleep_logged(
                max(300, settings.storage_pause_seconds), "discovery worker", "storage paused"
            )
            continue

        # A pass that did nothing must not be followed immediately by another.
        idle = not report.get("attempted")
        await _sleep_logged(
            _idle_seconds(settings) if idle else max(30, settings.discovery_interval_seconds),
            "discovery worker",
            "queue empty" if idle else "next pass",
        )


def start_discovery_worker(settings: EngineSettings | None = None) -> asyncio.Task[None] | None:
    global _discovery_task
    cfg = settings or get_engine_settings()
    if not cfg.seed_crawl_enabled or _discovery_task is not None:
        return _discovery_task
    _discovery_task = asyncio.create_task(_discovery_loop(cfg), name="eyesearch-discovery")
    logger.info("discovery worker started (every ~%ss)", cfg.discovery_interval_seconds)
    return _discovery_task


async def stop_discovery_worker() -> None:
    global _discovery_task
    if _discovery_task is None:
        return
    _discovery_task.cancel()
    try:
        await _discovery_task
    except asyncio.CancelledError:
        pass
    _discovery_task = None
    logger.info("discovery worker stopped")


async def stop_seed_crawler() -> None:
    global _seed_task
    if _seed_task is None:
        return
    _seed_task.cancel()
    try:
        await _seed_task
    except asyncio.CancelledError:
        pass
    _seed_task = None
    logger.info("seed crawler stopped")


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


__all__ = [
    "start_discovery_worker",
    "stop_discovery_worker",
    "start_url_queue_worker",
    "stop_url_queue_worker",
    "start",
    "stop",
    "run_once",
    "start_backlog_worker",
    "stop_backlog_worker",
    "start_seed_crawler",
    "stop_seed_crawler",
]
