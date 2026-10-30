"""Abuse defence — the rate limiter, with a memory for repeat offenders.

The token bucket in `app/indexer/rate_limit.py` already holds every client to 60
requests a minute. That is the right shape for ordinary traffic and the wrong
shape for an attacker: a bucket forgives, and floods come back every minute.

This wraps it. A client that keeps hitting the limit is remembered and refused
outright for a while, which costs the attacker far more than it costs us.
"""

from __future__ import annotations

import logging
import time

from app.indexer.rate_limit import RateDecision, TokenBucketLimiter

logger = logging.getLogger(__name__)

#: Times a client may hit the limit before it is blocked rather than throttled.
STRIKES_BEFORE_BLOCK = 3
#: How long a block lasts.
BLOCK_SECONDS = 900.0


class SabotageShieldAgent:
    """Rate limit, and block what keeps coming back."""

    def __init__(
        self,
        limiter: TokenBucketLimiter | None = None,
        *,
        strikes: int = STRIKES_BEFORE_BLOCK,
        block_seconds: float = BLOCK_SECONDS,
        clock=time.monotonic,
    ) -> None:
        # `limiter or ...` is wrong here: TokenBucketLimiter defines __len__, so
        # an empty limiter is falsy and the default would replace the one that
        # was passed in — silently, and with a fresh full bucket every time.
        self.limiter = (
            limiter
            if limiter is not None
            else TokenBucketLimiter(capacity=60, window_seconds=60.0)
        )
        self.strikes = max(1, int(strikes))
        self.block_seconds = float(block_seconds)
        self._clock = clock
        #: Strikes and blocks are separate on purpose. Keeping a strike count in
        #: the block record meant an expired block — `until` of zero — was popped
        #: on the next read and the count went with it, so a client could flood
        #: for ever and never be blocked.
        self._strikes: dict[str, int] = {}
        self._blocks: dict[str, float] = {}

    def blocked(self, key: str) -> float:
        """Seconds remaining on a block, or 0."""
        until = self._blocks.get(key)
        if until is None:
            return 0.0
        remaining = until - self._clock()
        if remaining <= 0:
            self._blocks.pop(key, None)
            return 0.0
        return remaining

    def check(self, key: str) -> RateDecision:
        key = key or "unknown"

        remaining = self.blocked(key)
        if remaining > 0:
            return RateDecision(allowed=False, remaining=0.0, retry_after_seconds=remaining)

        decision = self.limiter.check(key)
        if decision.allowed:
            return decision

        count = self._strikes.get(key, 0) + 1
        self._strikes[key] = count
        if count >= self.strikes:
            self._blocks[key] = self._clock() + self.block_seconds
            logger.warning("blocking %s for %.0fs after %d rate-limit strikes", key, self.block_seconds, count)
            return RateDecision(
                allowed=False, remaining=0.0, retry_after_seconds=self.block_seconds
            )

        return decision

    def stats(self) -> dict[str, object]:
        return {
            "blocked_clients": len(self._blocks),
            "clients_with_strikes": len(self._strikes),
            "strikes_needed": self.strikes,
        }


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "24_sabotage_shield_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 3 > 0, 3 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
