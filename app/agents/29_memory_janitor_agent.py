"""Memory hygiene, done sparingly.

`gc.collect()` is not free: a full collection walks the whole heap, and calling
it on a timer in a request-serving process pauses everything for the duration.
CPython's generational collector is already tuned for this workload, and
disabling it to run it manually is usually a net loss.

So the janitor is honest about what it is for. It runs a collection only when
the process is genuinely large and has just dropped a lot of work, and its main
job is the part that actually helps: closing connection pools that have gone
idle, and reporting the footprint so someone can see a leak forming.
"""

from __future__ import annotations

import gc
import logging
import os
import resource
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Only collect once the process holds more than this.
COLLECT_THRESHOLD_BYTES = 512 * 1024 * 1024
#: And no more often than this.
MIN_INTERVAL_SECONDS = 300.0


@dataclass
class JanitorReport:
    rss_bytes: int = 0
    collected: bool = False
    freed_bytes: int = 0
    pools_closed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "rss_bytes": self.rss_bytes,
            "collected": self.collected,
            "freed_bytes": self.freed_bytes,
            "pools_closed": self.pools_closed,
            "notes": self.notes,
        }


def resident_bytes() -> int:
    """Resident set size of this process.

    `ru_maxrss` is kilobytes on Linux and bytes on macOS; the platform check
    matters, because the wrong unit turns a 500 MB process into a 500 GB one.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(usage) * 1024 if os.name == "posix" and os.uname().sysname == "Linux" else int(usage)


class MemoryJanitorAgent:
    """Report the footprint, and collect only when it is worth it."""

    def __init__(
        self,
        *,
        threshold_bytes: int = COLLECT_THRESHOLD_BYTES,
        min_interval: float = MIN_INTERVAL_SECONDS,
        clock=time.monotonic,
    ) -> None:
        self.threshold_bytes = int(threshold_bytes)
        self.min_interval = float(min_interval)
        self._clock = clock
        self._last_collect = 0.0
        self.collections = 0

    def should_collect(self, rss: int) -> bool:
        return (
            rss >= self.threshold_bytes
            and (self._clock() - self._last_collect) >= self.min_interval
        )

    async def sweep(self, client=None, *, force: bool = False) -> JanitorReport:
        report = JanitorReport(rss_bytes=resident_bytes())

        if force or self.should_collect(report.rss_bytes):
            before = report.rss_bytes
            collected = gc.collect()
            self._last_collect = self._clock()
            self.collections += 1
            report.collected = True
            report.freed_bytes = max(0, before - resident_bytes())
            report.notes.append(f"gc.collect() reclaimed {collected} objects")
        else:
            report.notes.append("below the collection threshold; left to the generational collector")

        # An idle pool holds connections the database could give to someone else.
        if client is not None:
            try:
                pool = getattr(client, "_pool", None)
                if pool is not None and hasattr(pool, "check"):
                    await pool.check()
                    report.pools_closed.append("pool checked")
            except Exception as error:  # noqa: BLE001 - hygiene must not fail a request
                report.notes.append(f"pool check skipped: {error}")

        return report
