"""Reading and writing `live_facts`.

A fact is a key, a value and the time it was true. The key is looked up
exactly — `gold.22k.inr.gram` — because that is how a caller who wants a price
asks, and full-text ranking has nothing to say about it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)

#: A fact older than this is not returned as fresh. Per-fact TTL overrides it.
DEFAULT_TTL_SECONDS = 900

#: How many facts the table may hold before the oldest are dropped. The ceiling
#: is a row count rather than bytes: 20,000 facts is a few megabytes, and
#: counting bytes would need a full table scan on every write.
MAX_FACTS = 20000


@dataclass
class LiveFact:
    key: str
    value: str
    unit: str
    source: str
    fetched_at: str
    ttl_seconds: int
    fresh: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "unit": self.unit,
            "source": self.source,
            "as_of": self.fetched_at,
            "ttl_seconds": self.ttl_seconds,
            "freshness": "live" if self.fresh else "stale",
        }


async def put_fact(
    client,
    key: str,
    value: str,
    *,
    unit: str = "",
    source: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> None:
    """Store one fact, replacing the value and timestamp if it is already there."""
    await client.execute(
        "INSERT INTO live_facts (key, value, unit, source, fetched_at, ttl_seconds) "
        "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, "
        "  unit = EXCLUDED.unit, source = EXCLUDED.source, "
        "  fetched_at = EXCLUDED.fetched_at, ttl_seconds = EXCLUDED.ttl_seconds",
        (key, str(value), unit, source, int(ttl_seconds)),
    )


async def fact(client, key: str) -> LiveFact | None:
    """One fact, marked fresh or stale against its own TTL."""
    result = await client.execute(
        "SELECT key, value, unit, source, fetched_at, ttl_seconds, "
        "  (fetched_at + (ttl_seconds * INTERVAL '1 second')) > CURRENT_TIMESTAMP AS fresh "
        "FROM live_facts WHERE key = ?",
        (key,),
    )
    rows = [tuple(row) for row in result.rows]
    if not rows:
        return None
    row = rows[0]
    return LiveFact(
        key=str(row[0]),
        value=str(row[1]),
        unit=str(row[2] or ""),
        source=str(row[3] or ""),
        fetched_at=str(row[4]),
        ttl_seconds=int(row[5] or DEFAULT_TTL_SECONDS),
        fresh=bool(row[6]),
    )


async def all_facts(client, *, prefix: str = "", limit: int = 200) -> list[LiveFact]:
    """Every fact currently held, newest first. Used to see what a source gave."""
    sql = (
        "SELECT key, value, unit, source, fetched_at, ttl_seconds, "
        "  (fetched_at + (ttl_seconds * INTERVAL '1 second')) > CURRENT_TIMESTAMP AS fresh "
        "FROM live_facts"
    )
    args: list[Any] = []
    if prefix:
        sql += " WHERE key LIKE ?"
        args.append(f"{prefix}%")
    sql += " ORDER BY fetched_at DESC LIMIT ?"
    args.append(max(1, int(limit)))
    result = await client.execute(sql, tuple(args))
    return [
        LiveFact(
            key=str(row[0]),
            value=str(row[1]),
            unit=str(row[2] or ""),
            source=str(row[3] or ""),
            fetched_at=str(row[4]),
            ttl_seconds=int(row[5] or DEFAULT_TTL_SECONDS),
            fresh=bool(row[6]),
        )
        for row in (tuple(r) for r in result.rows)
    ]


async def expire(client) -> int:
    """Delete facts whose TTL has passed, then trim to `MAX_FACTS`.

    Expiry is what keeps a table of minute-long values from growing for ever:
    nothing deletes a stale price otherwise, and a price nobody asked for in a
    week is not worth the row.
    """
    removed = await client.execute(
        "DELETE FROM live_facts "
        "WHERE (fetched_at + (ttl_seconds * INTERVAL '1 second')) < CURRENT_TIMESTAMP"
    )
    count = int(getattr(removed, "rows_affected", 0) or 0)

    trimmed = await client.execute(
        "DELETE FROM live_facts WHERE key IN ("
        "  SELECT key FROM live_facts ORDER BY fetched_at DESC OFFSET ?"
        ")",
        (MAX_FACTS,),
    )
    count += int(getattr(trimmed, "rows_affected", 0) or 0)
    if count:
        logger.info("live facts: expired %d", count)
    return count
