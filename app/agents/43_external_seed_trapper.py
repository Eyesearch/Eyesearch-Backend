"""Turn federated answers into future local coverage.

A query the index cannot answer is a gap worth filling. The URLs that filled it
are exactly the pages that should be crawled next — but only once the crawler is
allowed to run, and only through the frontier's own scope rules.

So this normalises the URLs and hands them to the frontier. It does not decide
what is admissible; the frontier does, and it is the only place that decision
should live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.agents import load_agent

logger = logging.getLogger(__name__)

sanitizer = load_agent("44_cross_source_sanitizer")
_shield = load_agent("18_trap_shield_agent")


@dataclass
class SeedReport:
    offered: int = 0
    accepted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"offered": self.offered, "accepted": self.accepted, "rejected": self.rejected}


class ExternalSeedTrapperAgent:
    """Deposit federated URLs into the frontier for future ingestion."""

    def __init__(self, frontier=None) -> None:
        self._frontier = frontier

    def normalise(self, url: str) -> str:
        return sanitizer.sanitise_url(url)

    async def trap(self, urls, *, dry_run: bool = False) -> SeedReport:
        report = SeedReport()
        for raw in urls or []:
            report.offered += 1
            url = self.normalise(str(raw or ""))
            if not url:
                continue

            # The shield's verdict comes first: a federated answer is no reason
            # to put a trap URL into the queue.
            reason = _shield.classify_trap(url)
            if reason:
                report.rejected[reason] = report.rejected.get(reason, 0) + 1
                continue

            if dry_run or self._frontier is None:
                report.accepted += 1
                continue

            host = (urlsplit(url).hostname or "").lower()
            if await self._frontier.add(url, source_host=host):
                report.accepted += 1

        return report
