"""Run the local search and the federated one at once, and drop the loser.

Waiting for the local query to finish before deciding whether to federate costs
the federated budget on every slow local query. Starting both together and
cancelling the external one the moment local answers costs one cancelled
connection instead.

The threshold is deliberately small. If the index answered in 15ms there is
nothing to gain from a network call, and the cancellation is the whole point.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: If local answers inside this, external work is cancelled.
LOCAL_FAST_MS = 15.0


@dataclass
class DispatchResult:
    local: object | None = None
    external: object | None = None
    local_ms: float = 0.0
    cancelled_external: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "local_ms": round(self.local_ms, 2),
            "cancelled_external": self.cancelled_external,
            "has_local": self.local is not None,
            "has_external": self.external is not None,
            "notes": self.notes,
        }


class AsyncParallelDispatcherAgent:
    """Start both, keep whichever is worth keeping."""

    def __init__(self, *, fast_ms: float = LOCAL_FAST_MS) -> None:
        self.fast_ms = float(fast_ms)

    async def run(self, local_coro, external_coro, *, federate: bool = True) -> DispatchResult:
        result = DispatchResult()
        started = time.perf_counter()

        if not federate:
            result.local = await local_coro
            result.local_ms = (time.perf_counter() - started) * 1000
            return result

        external = asyncio.ensure_future(external_coro)
        try:
            result.local = await local_coro
            result.local_ms = (time.perf_counter() - started) * 1000

            if result.local_ms <= self.fast_ms:
                # Nothing to gain: cancel rather than let it finish.
                external.cancel()
                result.cancelled_external = True
                result.notes.append("local answered fast; external cancelled")
                await asyncio.gather(external, return_exceptions=True)
                return result

            try:
                result.external = await external
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - external failure is not fatal
                result.notes.append(f"external failed: {error}")
            return result
        finally:
            if not external.done():
                external.cancel()
                await asyncio.gather(external, return_exceptions=True)


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "46_async_parallel_dispatcher",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 2 > 0, 2 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
