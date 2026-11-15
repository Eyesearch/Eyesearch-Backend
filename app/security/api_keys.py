"""API keys: issue them, check them, and count what they spend.

The key is a bearer credential, so only its hash is stored: a leaked database
must not be a leaked set of keys. The key itself is shown once, at creation, and
never again.

Two limits apply to every key, because they fail in different ways. A per-minute
limit stops a runaway loop; a daily quota stops a slow leak that never trips the
per-minute check but still costs the box all day.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Characters in a generated key. 32 bytes of urandom is far beyond guessable.
KEY_BYTES = 32

#: How many characters of the key are kept in the clear, for the operator to
#: recognise a key in a list without being able to use it.
PREFIX_CHARS = 8

DEFAULT_RATE_PER_MINUTE = 60
DEFAULT_DAILY_QUOTA = 10000


def hash_key(key: str) -> str:
    """The stored form of a key. Never store the key itself."""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


@dataclass
class ApiKey:
    key_id: str
    name: str
    prefix: str
    rate_limit_per_minute: int
    daily_quota: int
    calls_total: int
    calls_today: int
    revoked: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "key_id": self.key_id,
            "name": self.name,
            "prefix": self.prefix,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "daily_quota": self.daily_quota,
            "calls_total": self.calls_total,
            "calls_today": self.calls_today,
            "revoked": self.revoked,
        }


async def create_key(
    client,
    name: str,
    *,
    rate_limit_per_minute: int = DEFAULT_RATE_PER_MINUTE,
    daily_quota: int = DEFAULT_DAILY_QUOTA,
) -> tuple[str, ApiKey]:
    """Issue a key. The plain key is returned once and never stored."""
    key = secrets.token_urlsafe(KEY_BYTES)
    key_id = secrets.token_hex(8)
    await client.execute(
        "INSERT INTO api_keys (key_id, name, key_hash, prefix, rate_limit_per_minute, "
        "daily_quota, created_at, calls_total, calls_today, calls_day) "
        "VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, 0, 0, CURRENT_DATE)",
        (key_id, name, hash_key(key), key[:PREFIX_CHARS], int(rate_limit_per_minute), int(daily_quota)),
    )
    logger.info("api key created: %s (%s)", key_id, name)
    record = ApiKey(
        key_id=key_id,
        name=name,
        prefix=key[:PREFIX_CHARS],
        rate_limit_per_minute=rate_limit_per_minute,
        daily_quota=daily_quota,
        calls_total=0,
        calls_today=0,
        revoked=False,
    )
    return key, record


async def verify_key(client, key: str) -> ApiKey | None:
    """The key record, or None when the key is unknown or revoked."""
    if not key:
        return None
    result = await client.execute(
        "SELECT key_id, name, prefix, rate_limit_per_minute, daily_quota, calls_total, "
        "calls_today, revoked_at FROM api_keys WHERE key_hash = ?",
        (hash_key(key),),
    )
    rows = [tuple(row) for row in result.rows]
    if not rows:
        return None
    row = rows[0]
    return ApiKey(
        key_id=str(row[0]),
        name=str(row[1]),
        prefix=str(row[2] or ""),
        rate_limit_per_minute=int(row[3] or DEFAULT_RATE_PER_MINUTE),
        daily_quota=int(row[4] or DEFAULT_DAILY_QUOTA),
        calls_total=int(row[5] or 0),
        calls_today=int(row[6] or 0),
        revoked=row[7] is not None,
    )


async def record_call(client, key_id: str) -> None:
    """Count one call, resetting the daily figure when the day turns over."""
    await client.execute(
        "UPDATE api_keys SET calls_total = calls_total + 1, "
        "  calls_today = CASE WHEN calls_day = CURRENT_DATE THEN calls_today + 1 ELSE 1 END, "
        "  calls_day = CURRENT_DATE, last_used_at = CURRENT_TIMESTAMP "
        "WHERE key_id = ?",
        (key_id,),
    )


async def list_keys(client) -> list[ApiKey]:
    result = await client.execute(
        "SELECT key_id, name, prefix, rate_limit_per_minute, daily_quota, calls_total, "
        "calls_today, revoked_at FROM api_keys ORDER BY created_at DESC"
    )
    return [
        ApiKey(
            key_id=str(row[0]),
            name=str(row[1]),
            prefix=str(row[2] or ""),
            rate_limit_per_minute=int(row[3] or DEFAULT_RATE_PER_MINUTE),
            daily_quota=int(row[4] or DEFAULT_DAILY_QUOTA),
            calls_total=int(row[5] or 0),
            calls_today=int(row[6] or 0),
            revoked=row[7] is not None,
        )
        for row in (tuple(r) for r in result.rows)
    ]


async def revoke_key(client, key_id: str) -> bool:
    """Revoke by id. The row stays, so the history of what it spent survives."""
    result = await client.execute(
        "UPDATE api_keys SET revoked_at = CURRENT_TIMESTAMP "
        "WHERE key_id = ? AND revoked_at IS NULL",
        (key_id,),
    )
    return int(getattr(result, "rows_affected", 0) or 0) > 0


class KeyLimiter:
    """Per-minute limits, held in this process.

    In memory on purpose: a per-minute window is short enough that a restart
    costs at most a minute of overspend, and a database round trip on every call
    to count seconds would cost more than the limit protects.
    """

    def __init__(self, now=time.monotonic) -> None:
        self._now = now
        self._hits: dict[str, list[float]] = {}

    def allow(self, key_id: str, limit: int) -> tuple[bool, int]:
        """`(allowed, remaining)` for this key in the last sixty seconds."""
        if limit <= 0:
            return True, 0
        now = self._now()
        window = [stamp for stamp in self._hits.get(key_id, []) if now - stamp < 60.0]
        if len(window) >= limit:
            self._hits[key_id] = window
            return False, 0
        window.append(now)
        self._hits[key_id] = window
        return True, max(0, limit - len(window))
