"""Purge what is dead.

The revalidation agent already re-fetches pages on a schedule and marks them
stale. What it does not do is decide when a page should stop existing: a 404 seen
once may be a bad minute, a 404 seen on three passes is a page that is gone.

That decision is here, and it is a count rather than a single reading — deleting
a page because one request failed throws away work that cannot be recovered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Statuses that mean the page is gone for good.
GONE_STATUSES = frozenset({404, 410, 451})
#: Server errors are the host's problem, not evidence the page is gone.
TRANSIENT_STATUSES = frozenset({500, 502, 503, 504, 429})
#: How many gone readings before the page is removed.
STRIKES_TO_PURGE = 3


@dataclass
class ReapReport:
    checked: int = 0
    gone: int = 0
    purged: int = 0
    transient: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "gone": self.gone,
            "purged": self.purged,
            "transient": self.transient,
            "notes": self.notes,
        }


def classify_status(status: int) -> str:
    """`gone`, `transient` or `alive`."""
    if status in GONE_STATUSES:
        return "gone"
    if status in TRANSIENT_STATUSES or status >= 500:
        return "transient"
    return "alive"


class BrokenLinkReaperAgent:
    """Count gone readings per URL and remove the pages that earn it."""

    def __init__(self, client, *, strikes: int = STRIKES_TO_PURGE) -> None:
        self._client = client
        self.strikes = max(1, int(strikes))
        self._counts: dict[str, int] = {}

    def record(self, url: str, status: int) -> int:
        """Record a reading; returns the strike count for that URL."""
        verdict = classify_status(status)
        if verdict == "alive":
            self._counts.pop(url, None)
            return 0
        if verdict == "transient":
            return self._counts.get(url, 0)
        count = self._counts.get(url, 0) + 1
        self._counts[url] = count
        return count

    def should_purge(self, url: str) -> bool:
        return self._counts.get(url, 0) >= self.strikes

    async def purge(self, urls: list[str], *, dry_run: bool = False) -> int:
        doomed = [url for url in urls if self.should_purge(url)]
        if not doomed or dry_run:
            return len(doomed)

        removed = 0
        for start in range(0, len(doomed), 200):
            chunk = doomed[start : start + 200]
            placeholders = ", ".join("?" for _ in chunk)
            result = await self._client.execute(
                f"DELETE FROM pages WHERE url IN ({placeholders})", tuple(chunk)
            )
            removed += int(getattr(result, "rowcount", 0) or len(chunk))
            for url in chunk:
                self._counts.pop(url, None)
        return removed
