"""The supervisor: watch every worker and restart the ones that stop breathing.

`scheduler._supervise` already restarts a worker whose task *raises*. It cannot
see a task that is alive but stuck — awaiting a socket that will never answer, or
looping without progress. That is the failure this agent exists for, and it is
the harder one: a crashed task is obvious, a stalled one looks healthy from
outside.

Workers call `beat(name)` from their loop. Anything that has not beaten within
`stale_after` seconds is cancelled and restarted from its factory.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

#: A worker that has not beaten for this long is treated as stalled.
DEFAULT_STALE_AFTER = 30.0
#: How often to look.
DEFAULT_INTERVAL = 5.0


@dataclass
class WorkerRecord:
    factory: Callable[[], Awaitable[None]]
    task: asyncio.Task | None = None
    last_beat: float = 0.0
    restarts: int = 0


@dataclass
class KingReport:
    running: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)
    restarted: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "running": self.running,
            "stalled": self.stalled,
            "restarted": self.restarted,
            "failed": self.failed,
        }


class KingOrchestratorAgent:
    """Register workers, watch their heartbeats, restart the stalled ones."""

    def __init__(
        self,
        *,
        stale_after: float = DEFAULT_STALE_AFTER,
        interval: float = DEFAULT_INTERVAL,
        clock=time.monotonic,
    ) -> None:
        self.stale_after = max(1.0, float(stale_after))
        self.interval = max(0.5, float(interval))
        self._clock = clock
        self._workers: dict[str, WorkerRecord] = {}
        self._running = False
        self.beats = 0

    # -- registration -------------------------------------------------------

    def register(self, name: str, factory: Callable[[], Awaitable[None]]) -> asyncio.Task:
        """Start a worker and take responsibility for keeping it alive."""
        record = WorkerRecord(factory=factory, last_beat=self._clock())
        self._workers[name] = record
        record.task = asyncio.create_task(self._run(name, record))
        return record.task

    def beat(self, name: str) -> None:
        """Called by a worker that is making progress."""
        record = self._workers.get(name)
        if record is not None:
            record.last_beat = self._clock()
            self.beats += 1

    # -- inspection ---------------------------------------------------------

    def stalled(self) -> list[str]:
        now = self._clock()
        return [
            name
            for name, record in self._workers.items()
            if record.task is not None
            and not record.task.done()
            and (now - record.last_beat) > self.stale_after
        ]

    def health(self) -> dict[str, object]:
        return {
            "workers": {
                name: {
                    "alive": record.task is not None and not record.task.done(),
                    "restarts": record.restarts,
                    "silent_for": round(self._clock() - record.last_beat, 1),
                }
                for name, record in self._workers.items()
            },
            "beats": self.beats,
        }

    # -- the loop -----------------------------------------------------------

    async def _run(self, name: str, record: WorkerRecord) -> None:
        while True:
            try:
                await record.factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                # A factory that dies on its own is restarted with a pause, so a
                # broken dependency cannot spin the CPU in a tight loop.
                record.restarts += 1
                logger.exception("%s died; restarting in %.1fs", name, self.interval)
                await asyncio.sleep(self.interval)

    async def check_once(self) -> KingReport:
        """Restart anything stalled. One pass, so it can be tested."""
        report = KingReport()
        now = self._clock()

        for name, record in list(self._workers.items()):
            if record.task is not None and not record.task.done():
                report.running.append(name)
                if (now - record.last_beat) > self.stale_after:
                    report.stalled.append(name)
                    logger.warning("%s has been silent for %.1fs; restarting", name, now - record.last_beat)
                    record.task.cancel()
                    record.restarts += 1
                    record.last_beat = now
                    record.task = asyncio.create_task(self._run(name, record))
                    report.restarted.append(name)
            else:
                # Finished or crashed: start it again.
                report.restarted.append(name)
                record.last_beat = now
                record.task = asyncio.create_task(self._run(name, record))

        return report

    async def supervise_forever(self) -> None:
        self._running = True
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("supervisor pass failed")
            await asyncio.sleep(self.interval)

    async def stop(self) -> None:
        self._running = False
        for record in self._workers.values():
            if record.task is not None and not record.task.done():
                record.task.cancel()
        await asyncio.gather(
            *(record.task for record in self._workers.values() if record.task is not None),
            return_exceptions=True,
        )
