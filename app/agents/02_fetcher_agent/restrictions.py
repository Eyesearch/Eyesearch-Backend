"""Domains that have refused us, and for how long we leave them alone.

A 403, a 202 challenge or a 429 from a WAF is an answer: the site does not want
this crawler. Re-queuing it every pass wastes the queue and makes the block
worse, so the host is marked restricted for a cooldown and skipped outright.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Statuses that mean "blocked", not "broken".
RESTRICTING_STATUSES: frozenset[int] = frozenset({202, 403, 405, 429, 451})

DEFAULT_COOLDOWN_SECONDS = 6 * 60 * 60  # six hours


@dataclass(slots=True)
class Restriction:
    host: str
    status: int
    marked_at: float
    reason: str = ""


_restricted: dict[str, Restriction] = {}


def _now() -> float:
    return time.monotonic()


def is_restricting(status: int | None) -> bool:
    """True when this HTTP status means the site is blocking us."""
    return status is not None and status in RESTRICTING_STATUSES


def mark_restricted(host: str, status: int, *, reason: str = "") -> None:
    """Record a block once, so the log does not repeat it every pass."""
    key = (host or "").lower().removeprefix("www.")
    if not key:
        return
    if key in _restricted:
        return
    _restricted[key] = Restriction(key, status, _now(), reason or f"status {status}")
    logger.info("domain restricted (%s): %s — bypassing it for the cooldown", status, key)


def is_restricted(host: str, *, cooldown_seconds: int | None = None) -> bool:
    """Whether the host is still inside its cooldown."""
    key = (host or "").lower().removeprefix("www.")
    entry = _restricted.get(key)
    if entry is None:
        return False
    cooldown = cooldown_seconds if cooldown_seconds is not None else DEFAULT_COOLDOWN_SECONDS
    if _now() - entry.marked_at >= cooldown:
        _restricted.pop(key, None)
        logger.info("domain restriction expired: %s", key)
        return False
    return True


def restricted_hosts() -> dict[str, Restriction]:
    return dict(_restricted)


def clear(host: str | None = None) -> None:
    if host is None:
        _restricted.clear()
    else:
        _restricted.pop((host or "").lower().removeprefix("www."), None)


__all__ = [
    "RESTRICTING_STATUSES",
    "Restriction",
    "clear",
    "is_restricted",
    "is_restricting",
    "mark_restricted",
    "restricted_hosts",
]
