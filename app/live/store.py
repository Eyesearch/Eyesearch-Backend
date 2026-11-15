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
#:
#: Twice the fifteen-minute refresh interval, not equal to it: with an equal
#: TTL there is a window before every pass in which every price is already
#: stale, and a reader asking at the wrong minute is told the data is old while
#: the refresh is still running.
DEFAULT_TTL_SECONDS = 1800

#: How many facts the table may hold before the oldest are dropped.
MAX_FACTS = 20000

#: The byte ceiling on the live tables, in megabytes. Rows alone are not the
#: limit that was agreed: a headline is a few hundred bytes and a stored body is
#: not, so the same row count can mean kilobytes or hundreds of megabytes. The
#: size is read from the catalogue, which is a lookup, not a scan.
MAX_LIVE_MB = 500


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


async def expire(client, max_facts: int | None = None) -> int:
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
        (int(max_facts or MAX_FACTS),),
    )
    count += int(getattr(trimmed, "rows_affected", 0) or 0)

    count += await trim_to_size(client)
    if count:
        logger.info("live facts: expired %d", count)
    return count


async def live_bytes(client) -> int:
    """Bytes the live tables occupy, including their indexes."""
    try:
        result = await client.execute(
            "SELECT coalesce(pg_total_relation_size('live_facts'), 0) "
            "     + coalesce(pg_total_relation_size('live_sources'), 0)"
        )
        return int(result.scalar(0) or 0)
    except Exception:  # noqa: BLE001 - a size we cannot read is not a failure
        return 0


async def trim_to_size(client, max_mb: int = MAX_LIVE_MB) -> int:
    """Drop the oldest facts until the live tables fit the agreed ceiling.

    The row cap alone does not bound the bytes: the same twenty thousand rows are
    kilobytes of prices and hundreds of megabytes of text. This is the ceiling
    that was actually agreed, so it is the one enforced.
    """
    limit_bytes = int(max_mb) * 1024 * 1024
    size = await live_bytes(client)
    if size <= limit_bytes:
        return 0

    removed = 0
    # Drop in batches until the size comes down, so one pass never becomes an
    # unbounded delete on a table that has to keep serving reads.
    for _ in range(20):
        result = await client.execute(
            "DELETE FROM live_facts WHERE key IN ("
            "  SELECT key FROM live_facts ORDER BY fetched_at ASC LIMIT 500"
            ")"
        )
        removed += int(getattr(result, "rows_affected", 0) or 0)
        size = await live_bytes(client)
        if size <= limit_bytes or removed == 0:
            break
    if removed:
        logger.info("live facts: trimmed %d to fit %d MB (now %.1f MB)", removed, max_mb, size / 1048576)
    return removed
