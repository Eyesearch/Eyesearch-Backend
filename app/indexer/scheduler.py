"""Background worker lifecycle.

The workers are started from the FastAPI lifespan with `asyncio.create_task`,
which returns as soon as the task is scheduled. The port binds and `/health`
answers immediately; the crawl happens behind it.

Stopping is symmetric: every task is cancelled and awaited, so a reload or a
Render redeploy never leaves a task writing to a closed client.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.agents import load_agent
from app.engine.config import Settings, get_settings
from app.indexer.watchdog import StorageWatchdog
from app.orchestrator import Orchestrator

KingOrchestratorAgent = load_agent("14_king_orchestrator_agent").KingOrchestratorAgent
RegentAgent = load_agent("21_regent_agent").RegentAgent
MemoryJanitorAgent = load_agent("29_memory_janitor_agent").MemoryJanitorAgent
ChroniclerReporterAgent = load_agent("30_chronicler_reporter_agent").ChroniclerReporterAgent

logger = logging.getLogger(__name__)


#: A worker that dies is restarted after this long, doubling up to the cap
#: while it keeps dying and resetting once a run has lasted a while.
RESTART_DELAY_SECONDS = 2.0
MAX_RESTART_DELAY_SECONDS = 60.0
HEALTHY_RUN_SECONDS = 120.0


@dataclass
class WorkerHandles:
    """The running background tasks, so the lifespan can stop them."""

    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    restarts: dict[str, int] = field(default_factory=dict)
    last_error: dict[str, str] = field(default_factory=dict)
    king: object | None = None
    regent: object | None = None

    def add(self, name: str, task: asyncio.Task) -> None:
        self.tasks[name] = task
        self.restarts.setdefault(name, 0)

    @property
    def names(self) -> list[str]:
        return sorted(self.tasks)

    def running(self) -> list[str]:
        return [name for name, task in self.tasks.items() if not task.done()]

    def status(self) -> dict[str, dict[str, object]]:
        """Per-worker liveness, for `/health` — a dead worker must be visible."""
        return {
            name: {
                "running": not task.done(),
                "restarts": self.restarts.get(name, 0),
                "last_error": self.last_error.get(name, ""),
            }
            for name, task in self.tasks.items()
        }


async def _supervise(name: str, factory, handles: "WorkerHandles") -> None:
    """Run a worker and restart it whenever it stops, with backoff.

    A worker that ends — because it raised, or because its loop returned — used
    to stay dead until the next deploy. One transient database error was enough
    to stop crawling silently while `/health` still answered and search kept
    working from the existing index. Restarting is the difference between a
    blip and an outage.
    """
    delay = RESTART_DELAY_SECONDS
    while True:
        started = asyncio.get_running_loop().time()
        try:
            await factory()
            logger.warning("worker %s returned; restarting in %.0fs", name, delay)
        except asyncio.CancelledError:
            logger.info("worker stopped: %s", name)
            raise
        except Exception as error:
            handles.last_error[name] = f"{type(error).__name__}: {error}"
            logger.exception("worker %s died: %s", name, error)

        handles.restarts[name] = handles.restarts.get(name, 0) + 1
        ran_for = asyncio.get_running_loop().time() - started
        if ran_for >= HEALTHY_RUN_SECONDS:
            delay = RESTART_DELAY_SECONDS
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_RESTART_DELAY_SECONDS)


def start_workers(
    orchestrator: Orchestrator,
    settings: Settings | None = None,
) -> WorkerHandles:
    """Schedule the crawl and revalidation loops without blocking startup."""
    settings = settings or get_settings()
    handles = WorkerHandles()

    # The supervisor and its standby. They only start when there is something to
    # supervise: with the crawler and revalidation both off, a watchdog watching
    # nothing is just another task to leak.
    if settings.crawler_enabled or settings.revalidation_enabled:
        king = KingOrchestratorAgent()
        regent = RegentAgent(king)
        orchestrator.king = king
        handles.king = king
        handles.regent = regent

    if settings.crawler_enabled:
        handles.add(
            "crawler",
            _start(orchestrator, "crawler", orchestrator.run_forever, handles),
        )
        logger.info("crawl worker started")

    # The watchdog runs whenever the crawler does: it is the thing that tells
    # the crawler to stop taking new URLs before the disk is full.
    if settings.crawler_enabled:
        handles.add(
            "watchdog",
            asyncio.create_task(
                _supervise(
                    "watchdog",
                    lambda: _watch_storage(orchestrator, settings),
                    handles,
                )
            ),
        )
        logger.info("storage watchdog started")

    if settings.revalidation_enabled:
        handles.add(
            "revalidation",
            _start(
                orchestrator, "revalidation", orchestrator.revalidation.run_forever, handles
            ),
        )
        logger.info("revalidation worker started")

    if settings.maintenance_enabled:
        handles.add(
            "maintenance",
            asyncio.create_task(_maintain(orchestrator, settings, handles)),
        )
        logger.info("maintenance worker started")

    if handles.regent is not None:
        handles.add("regent", asyncio.create_task(handles.regent.stand_by_forever()))

    return handles


def _start(orchestrator: Orchestrator, name: str, factory, handles: "WorkerHandles"):
    """Start a worker, supervised by the King when there is one.

    The King restarts a worker that is alive but silent; `_supervise` restarts
    one that raised. Both are needed, and only one of them may own the task, so
    the King takes it when present.
    """
    king = getattr(orchestrator, "king", None)
    if king is not None:
        return king.register(name, factory)
    return asyncio.create_task(_supervise(name, factory, handles))


async def _maintain(orchestrator: Orchestrator, settings: Settings, handles: "WorkerHandles") -> None:
    """Housekeeping that watches the process rather than the crawl.

    The janitor reports the footprint and collects only when it is worth it; the
    chronicler writes a report once a day. Both are cheap, and both are useful
    whether or not anything is being crawled.
    """
    janitor = MemoryJanitorAgent()
    chronicler = ChroniclerReporterAgent(orchestrator.store.client, settings, orchestrator)
    interval = max(30.0, float(settings.maintenance_interval_seconds))
    since_report = 0.0

    while True:
        try:
            report = await janitor.sweep(orchestrator.store.client)
            if report.collected:
                logger.info("memory janitor: %s", report.as_dict())

            since_report += interval
            if since_report >= 24 * 3600:
                since_report = 0.0
                daily = await chronicler.build()
                logger.info("daily report:\n%s", daily.as_markdown())
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("maintenance pass failed")
        await asyncio.sleep(interval)


async def _watch_storage(orchestrator: Orchestrator, settings: Settings) -> None:
    """Flip the crawler into freshness-only mode as the database fills.

    Checks periodically rather than on every batch: the size only moves in
    megabytes, and `pg_database_size` is not free.
    """
    watchdog = StorageWatchdog(orchestrator.store.client, settings)
    interval = max(5.0, float(settings.storage_watchdog_interval_seconds))

    while True:
        state = await watchdog.state()
        if state is not None:
            if state.over_ceiling and not orchestrator.freshness_only:
                orchestrator.freshness_only = True
                logger.warning(
                    "storage watchdog: %.1f%% of the %.1f GB ceiling; "
                    "crawler switched to freshness-only",
                    state.fraction * 100,
                    settings.max_storage_mb / 1024,
                )
            elif not state.over_ceiling and orchestrator.freshness_only:
                orchestrator.freshness_only = False
                logger.info("storage watchdog: back under the ceiling; crawling resumes")
        await asyncio.sleep(interval)


async def stop_workers(handles: WorkerHandles | None) -> None:
    """Cancel every worker and wait for it to unwind."""
    if handles is None:
        return
    for name, task in handles.tasks.items():
        if not task.done():
            task.cancel()
    for name, task in handles.tasks.items():
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:  # already logged by the guard
            logger.debug("worker %s raised while stopping: %s", name, error)
    # The King's registry holds its own tasks, which are not in `handles.tasks`.
    if handles.king is not None:
        await handles.king.stop()
    logger.info("all workers stopped")


__all__ = [
    "HEALTHY_RUN_SECONDS",
    "MAX_RESTART_DELAY_SECONDS",
    "RESTART_DELAY_SECONDS",
    "WorkerHandles",
    "start_workers",
    "stop_workers",
]
