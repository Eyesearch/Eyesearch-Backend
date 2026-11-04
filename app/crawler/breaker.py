"""Per-domain circuit breaker.

Five dead hosts produced 540 failures. Each of their queued URLs was fetched,
timed out, retried, and failed again — the crawl spending hours re-proving that
a domain which stopped answering has stopped answering.

The breaker is deliberately per-domain and in-memory. Per-domain because one
dead host says nothing about another; in-memory because it is a fact about this
run, and a process that restarts should get to form its own opinion rather than
inherit a stale verdict from a day ago.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class BreakerState:
    """What is known about one domain in this run."""

    consecutive_failures: int = 0
    total_failures: int = 0
    opened_at: float | None = None
    #: Set once the domain has been reported, so the skip is logged once rather
    #: than once per queued URL.
    reported: bool = False

    def is_open(self, now: float, cooldown: float) -> bool:
        return self.opened_at is not None and (now - self.opened_at) < cooldown


class DomainCircuitBreaker:
    """Stop asking a domain that has stopped answering.

    A domain opens after `threshold` consecutive failures. It stays open for
    `cooldown` seconds, then closes on its own so a host that comes back is
    crawled again without a restart.
    """

    def __init__(self, *, threshold: int = 5, cooldown: float = 86400.0) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown = float(cooldown)
        self._states: dict[str, BreakerState] = {}

    def state(self, domain: str) -> BreakerState:
        return self._states.setdefault(domain, BreakerState())

    def is_open(self, domain: str, *, now: float | None = None) -> bool:
        state = self._states.get(domain)
        if state is None or state.opened_at is None:
            return False
        return state.is_open(now if now is not None else time.monotonic(), self.cooldown)

    def record_success(self, domain: str) -> None:
        """One answered request closes the domain's account."""
        state = self.state(domain)
        state.consecutive_failures = 0
        # The total is kept: a host that fails half the time is still a host to
        # be wary of, even though it has not tripped the consecutive rule.
        state.opened_at = None

    def record_failure(self, domain: str, *, now: float | None = None) -> bool:
        """Count a failure. Returns True when this failure opened the domain."""
        state = self.state(domain)
        state.consecutive_failures += 1
        state.total_failures += 1
        if state.opened_at is not None:
            return False
        if state.consecutive_failures < self.threshold:
            return False
        state.opened_at = now if now is not None else time.monotonic()
        return True

    def open_domains(self) -> list[str]:
        now = time.monotonic()
        return [domain for domain in self._states if self.is_open(domain, now=now)]

    def should_report(self, domain: str) -> bool:
        """True the first time a domain is reported, then never again."""
        state = self.state(domain)
        if state.reported:
            return False
        state.reported = True
        return True


__all__ = ["BreakerState", "DomainCircuitBreaker"]
