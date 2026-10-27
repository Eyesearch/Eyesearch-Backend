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

from app.engine.config import Settings, get_settings
from app.orchestrator import Orchestrator

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

    if settings.crawler_enabled:
        handles.add(
            "crawler",
            asyncio.create_task(
                _supervise("crawler", orchestrator.run_forever, handles)
            ),
        )
        logger.info("crawl worker started")

    if settings.revalidation_enabled:
        handles.add(
            "revalidation",
            asyncio.create_task(
                _supervise("revalidation", orchestrator.revalidation.run_forever, handles)
            ),
        )
        logger.info("revalidation worker started")

    return handles


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
    logger.info("all workers stopped")


__all__ = [
    "HEALTHY_RUN_SECONDS",
    "MAX_RESTART_DELAY_SECONDS",
    "RESTART_DELAY_SECONDS",
    "WorkerHandles",
    "start_workers",
    "stop_workers",
]
