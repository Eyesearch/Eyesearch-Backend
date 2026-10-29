"""Watch database size and fall back to freshness-only operation.

The crawl has a hard ceiling — 4.5 GB by default. Past it, adding pages costs
money and slows every query, while the pages already held keep going stale. So
the response is not "stop working" but "change what the work is": stop taking
new URLs, and spend the effort on the pages already indexed.

`evaluate` is a pure function so the decision can be tested without a database;
`storage_bytes` reads the real figure from PostgreSQL.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The two modes the worker can run in.
MODE_NORMAL = "normal"
MODE_FRESHNESS = "freshness"


@dataclass
class StorageState:
    bytes_used: int
    limit_bytes: int
    mode: str
    fraction: float

    @property
    def over_ceiling(self) -> bool:
        return self.mode == MODE_FRESHNESS

    def as_dict(self) -> dict[str, object]:
        return {
            "bytes_used": self.bytes_used,
            "limit_bytes": self.limit_bytes,
            "mode": self.mode,
            "fraction": round(self.fraction, 4),
        }


def evaluate(bytes_used: int, limit_bytes: int, threshold: float = 0.95) -> StorageState:
    """Decide the mode from a size in bytes.

    The threshold is a fraction of the ceiling, not the ceiling itself: reacting
    at 100% means reacting after the disk is already full, and a crawl batch in
    flight can add a few hundred pages between two checks.
    """
    limit = max(1, int(limit_bytes))
    used = max(0, int(bytes_used))
    fraction = used / limit
    mode = MODE_FRESHNESS if fraction >= threshold else MODE_NORMAL
    return StorageState(bytes_used=used, limit_bytes=limit, mode=mode, fraction=fraction)


class StorageWatchdog:
    """Reads the database size and reports which mode the worker should use."""

    def __init__(self, client, settings) -> None:
        self._client = client
        self._settings = settings

    @property
    def limit_bytes(self) -> int:
        return int(float(self._settings.max_storage_mb) * 1024 * 1024)

    async def storage_bytes(self) -> int | None:
        """The database's on-disk size, or None when it cannot be read.

        `pg_database_size` is PostgreSQL-specific and the app also runs on
        SQLite, so a failure here is reported rather than raised: the watchdog
        is a safety net, not a reason to stop serving.
        """
        try:
            result = await self._client.execute(
                "SELECT pg_database_size(current_database())"
            )
        except Exception as error:  # noqa: BLE001 - never fatal
            logger.debug("could not read the database size: %s", error)
            return None
        value = result.scalar()
        return int(value) if value is not None else None

    async def state(self) -> StorageState | None:
        used = await self.storage_bytes()
        if used is None:
            return None
        state = evaluate(used, self.limit_bytes, float(self._settings.storage_watchdog_threshold))
        if state.over_ceiling:
            logger.warning(
                "database is at %.1f%% of its %.1f GB ceiling; switching to freshness-only",
                state.fraction * 100,
                self._settings.max_storage_mb / 1024,
            )
        return state
