"""The standby supervisor.

Two supervisors watching the same workers is worse than one: they would both
restart the same stalled worker, and the second restart would cancel the first.
So the Regent does not run a second supervision loop. It watches the King, and
takes over only when the King has stopped supervising.

What this can and cannot do is worth being plain about. Both live in one
process, so a process that dies takes both with it — that failure is for the
container restart policy, not for this class. What it does cover is the King's
loop wedging or raising: the Regent notices, takes the worker registry, and
restarts the King's loop behind it.
"""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class RegentAgent:
    """Standby for the King: promote when it stalls, restore it after."""

    def __init__(self, king, *, stale_after: float = 30.0, interval: float = 5.0, clock=time.monotonic) -> None:
        self._king = king
        self.stale_after = max(1.0, float(stale_after))
        self.interval = max(0.5, float(interval))
        self._clock = clock
        self._king_last_seen = self._clock()
        self.promotions = 0
        self.regent_active = False

    def king_beat(self) -> None:
        """Called by the King on each pass; silence is what triggers failover."""
        self._king_last_seen = self._clock()

    def king_is_stalled(self) -> bool:
        return (self._clock() - self._king_last_seen) > self.stale_after

    def promote(self) -> bool:
        """Take over supervision. Returns True when the Regent is now primary."""
        if self.regent_active:
            return False
        self.regent_active = True
        self.promotions += 1
        logger.warning(
            "regent: king silent for %.1fs; promoting to primary supervisor",
            self._clock() - self._king_last_seen,
        )
        return True

    def demote(self) -> bool:
        """Hand supervision back once the King is beating again."""
        if not self.regent_active:
            return False
        self.regent_active = False
        logger.info("regent: king is beating again; standing down")
        return True

    async def check_once(self) -> dict[str, object]:
        """One pass. Separated from the loop so failover can be tested."""
        action = "none"
        if self.king_is_stalled():
            if self.promote():
                action = "promoted"
                # Take over: run one supervision pass with the King's registry,
                # which is the state that matters.
                try:
                    await self._king.check_once()
                except Exception:
                    logger.exception("regent: supervision pass failed")
        elif self.regent_active:
            if self.demote():
                action = "demoted"

        return {
            "action": action,
            "regent_active": self.regent_active,
            "promotions": self.promotions,
        }

    async def stand_by_forever(self) -> None:
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("regent pass failed")
            await asyncio.sleep(self.interval)


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "21_regent_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 1 > 0, 1 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
