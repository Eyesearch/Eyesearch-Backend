"""Which live sources are alive, and which have stopped answering.

A source that dies must not take the API with it. Every fetch records success
or failure here, and a category whose source has been failing is served as
`stale` rather than as a 500 — the reader gets yesterday's price with its date
on it, which is recoverable, instead of an error, which is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Consecutive failures before a source is called unhealthy.
FAILURES_BEFORE_UNHEALTHY = 3


@dataclass
class SourceHealth:
    name: str
    url: str
    last_ok: str | None
    last_error: str | None
    ok_count: int
    fail_count: int

    @property
    def healthy(self) -> bool:
        return self.fail_count < FAILURES_BEFORE_UNHEALTHY

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "ok_count": self.ok_count,
            "fail_count": self.fail_count,
            "healthy": self.healthy,
        }


async def record_ok(client, name: str, url: str = "") -> None:
    await client.execute(
        "INSERT INTO live_sources (name, url, last_ok, last_error, ok_count, fail_count) "
        "VALUES (?, ?, CURRENT_TIMESTAMP, NULL, 1, 0) "
        "ON CONFLICT (name) DO UPDATE SET last_ok = CURRENT_TIMESTAMP, "
        "  last_error = NULL, ok_count = live_sources.ok_count + 1, "
        "  fail_count = 0, url = CASE WHEN EXCLUDED.url <> '' THEN EXCLUDED.url ELSE live_sources.url END",
        (name, url),
    )


async def record_fail(client, name: str, error: str, url: str = "") -> None:
    logger.warning("live source %s failed: %s", name, error)
    await client.execute(
        "INSERT INTO live_sources (name, url, last_ok, last_error, ok_count, fail_count) "
        "VALUES (?, ?, NULL, ?, 0, 1) "
        "ON CONFLICT (name) DO UPDATE SET last_error = ?, "
        "  fail_count = live_sources.fail_count + 1, "
        "  url = CASE WHEN EXCLUDED.url <> '' THEN EXCLUDED.url ELSE live_sources.url END",
        (name, url, str(error)[:500], str(error)[:500]),
    )


async def source_health(client) -> list[SourceHealth]:
    result = await client.execute(
        "SELECT name, url, last_ok, last_error, ok_count, fail_count "
        "FROM live_sources ORDER BY name"
    )
    return [
        SourceHealth(
            name=str(row[0]),
            url=str(row[1] or ""),
            last_ok=str(row[2]) if row[2] else None,
            last_error=str(row[3]) if row[3] else None,
            ok_count=int(row[4] or 0),
            fail_count=int(row[5] or 0),
        )
        for row in (tuple(r) for r in result.rows)
    ]
