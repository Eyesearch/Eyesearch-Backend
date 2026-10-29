"""Per-client rate limiting for the public read endpoints.

A token bucket per client: each client may spend `capacity` requests, and the
bucket refills at `refill_per_second`. A burst is therefore allowed up to the
capacity and sustained traffic is held to the refill rate — which is the shape
you want for an API, since a person searching quickly should not be throttled
but a scraper should be.

The bucket lives in this process. With one worker that is exactly right; with
several, each holds its own count, so the effective limit is per worker. That is
a deliberate trade: a shared counter means a network round trip on every search,
which is the latency this endpoint exists to avoid.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

__all__ = ["RateDecision", "TokenBucketLimiter"]


@dataclass
class RateDecision:
    allowed: bool
    remaining: float = 0.0
    retry_after_seconds: float = 0.0


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """`capacity` requests per client, refilling at `refill_per_second`."""

    def __init__(
        self,
        capacity: int = 60,
        window_seconds: float = 60.0,
        *,
        clock=time.monotonic,
        max_clients: int = 10_000,
    ) -> None:
        self.capacity = max(1, int(capacity))
        self.window_seconds = max(0.001, float(window_seconds))
        #: A full bucket over one window is the sustained rate.
        self.refill_per_second = self.capacity / self.window_seconds
        self._clock = clock
        self._max_clients = max(1, int(max_clients))
        self._buckets: dict[str, _Bucket] = {}

    def _bucket_for(self, key: str, now: float) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            # A new client starts with a full bucket: no penalty for arriving.
            bucket = _Bucket(tokens=float(self.capacity), updated_at=now)
            if len(self._buckets) >= self._max_clients:
                self._prune(now)
            self._buckets[key] = bucket
        else:
            elapsed = max(0.0, now - bucket.updated_at)
            bucket.tokens = min(float(self.capacity), bucket.tokens + elapsed * self.refill_per_second)
            bucket.updated_at = now
        return bucket

    def _prune(self, now: float) -> None:
        """Drop buckets that have refilled completely; they carry no state."""
        stale = [
            key
            for key, bucket in self._buckets.items()
            if now - bucket.updated_at > self.window_seconds
        ]
        for key in stale:
            self._buckets.pop(key, None)

    def check(self, key: str) -> RateDecision:
        """Spend one token for `key`, or report how long until one is free."""
        now = self._clock()
        bucket = self._bucket_for(key or "unknown", now)

        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return RateDecision(allowed=True, remaining=bucket.tokens)

        missing = 1.0 - bucket.tokens
        return RateDecision(
            allowed=False,
            remaining=bucket.tokens,
            retry_after_seconds=missing / self.refill_per_second,
        )

    def reset(self) -> None:
        self._buckets.clear()

    def __len__(self) -> int:
        return len(self._buckets)
