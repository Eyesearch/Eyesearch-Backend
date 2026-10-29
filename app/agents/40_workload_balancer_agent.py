"""Hold crawl concurrency down while people are searching.

The crawl and the search share one database. A crawl running at full concurrency
while someone searches is the difference between a 200ms answer and a 2s one,
and the person waiting does not care that the crawler is making progress.

So concurrency becomes a function of recent search latency: fast searches mean
the database has headroom and the crawler may have it; slow searches mean it does
not. The floor is never zero — a crawl that stops entirely never resumes.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Above this median latency, the crawler backs off.
BUSY_LATENCY_MS = 250.0
#: Below this, it may run at full concurrency.
IDLE_LATENCY_MS = 100.0
#: Never fewer than this many concurrent fetches.
MIN_CONCURRENCY = 2
#: How many recent samples the decision looks at.
WINDOW = 50


@dataclass
class BalancerState:
    concurrency: int
    median_latency_ms: float | None
    mode: str

    def as_dict(self) -> dict[str, object]:
        return {
            "concurrency": self.concurrency,
            "median_latency_ms": (
                round(self.median_latency_ms, 1) if self.median_latency_ms is not None else None
            ),
            "mode": self.mode,
        }


class WorkloadBalancerAgent:
    """Scale crawl concurrency against measured search latency."""

    def __init__(
        self,
        *,
        max_concurrency: int = 60,
        min_concurrency: int = MIN_CONCURRENCY,
        busy_ms: float = BUSY_LATENCY_MS,
        idle_ms: float = IDLE_LATENCY_MS,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_concurrency = max(1, min(int(min_concurrency), self.max_concurrency))
        self.busy_ms = float(busy_ms)
        self.idle_ms = float(idle_ms)
        self._samples: deque[float] = deque(maxlen=WINDOW)

    def observe(self, latency_ms: float) -> None:
        if latency_ms is not None and latency_ms >= 0:
            self._samples.append(float(latency_ms))

    def median(self) -> float | None:
        if not self._samples:
            return None
        ordered = sorted(self._samples)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    def state(self) -> BalancerState:
        median = self.median()
        if median is None:
            return BalancerState(self.max_concurrency, None, "unknown")

        if median >= self.busy_ms:
            # Busy: the floor, not zero.
            return BalancerState(self.min_concurrency, median, "busy")
        if median <= self.idle_ms:
            return BalancerState(self.max_concurrency, median, "idle")

        # Between the two, scale linearly rather than switching.
        span = self.busy_ms - self.idle_ms
        fraction = (self.busy_ms - median) / span if span else 1.0
        scaled = self.min_concurrency + fraction * (self.max_concurrency - self.min_concurrency)
        return BalancerState(max(self.min_concurrency, int(scaled)), median, "scaling")

    def concurrency(self) -> int:
        return self.state().concurrency
