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


@dataclass
class WorkerHandles:
    """The running background tasks, so the lifespan can stop them."""

    tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    def add(self, name: str, task: asyncio.Task) -> None:
        self.tasks[name] = task

    @property
    def names(self) -> list[str]:
        return sorted(self.tasks)

    def running(self) -> list[str]:
        return [name for name, task in self.tasks.items() if not task.done()]


async def _guarded(name: str, coro) -> None:
    """Run a worker loop and swallow only its final exception, loudly."""
    try:
        await coro
    except asyncio.CancelledError:
        logger.info("worker stopped: %s", name)
        raise
    except Exception as error:  # a dead worker must be visible, not silent
        logger.exception("worker %s died: %s", name, error)


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
            asyncio.create_task(_guarded("crawler", orchestrator.run_forever())),
        )
        logger.info("crawl worker started")

    if settings.revalidation_enabled:
        handles.add(
            "revalidation",
            asyncio.create_task(
                _guarded("revalidation", orchestrator.revalidation.run_forever())
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


__all__ = ["WorkerHandles", "start_workers", "stop_workers"]
