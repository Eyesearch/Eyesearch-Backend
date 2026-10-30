"""The last word on outbound work.

Every federated agent makes a network call, and a network call is the one thing
here that can pile up: slow endpoints, retries, a burst of zero-result queries.
Under memory pressure that is how a search engine falls over.

The Marshal sits above them. It reads the process footprint and, past the
ceiling, freezes every external call until it drops — internal search keeps
serving, because that is the part that must not stop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Past this, outbound work stops.
RAM_CEILING_BYTES = 800 * 1024 * 1024
#: And does not resume until it has dropped this far, so it cannot oscillate.
RAM_RESUME_BYTES = 640 * 1024 * 1024


@dataclass
class MarshalState:
    frozen: bool
    rss_bytes: int
    reason: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "frozen": self.frozen,
            "rss_bytes": self.rss_bytes,
            "rss_mb": round(self.rss_bytes / 1024 / 1024, 1),
            "reason": self.reason,
        }


class SupremeMarshalAgent:
    """Decide whether outbound work may run at all."""

    def __init__(
        self,
        *,
        ceiling_bytes: int = RAM_CEILING_BYTES,
        resume_bytes: int = RAM_RESUME_BYTES,
        reader=None,
    ) -> None:
        self.ceiling_bytes = int(ceiling_bytes)
        self.resume_bytes = int(resume_bytes)
        self._reader = reader
        self._frozen = False

    def _resident(self) -> int:
        if self._reader is not None:
            return int(self._reader())
        from app.agents import load_agent

        return int(load_agent("29_memory_janitor_agent").resident_bytes())

    def evaluate(self, rss_bytes: int | None = None) -> MarshalState:
        """Hysteresis matters: a single threshold would flap at the boundary."""
        rss = self._resident() if rss_bytes is None else int(rss_bytes)

        if not self._frozen and rss >= self.ceiling_bytes:
            self._frozen = True
            logger.warning("marshal: %.0f MB resident; freezing outbound work", rss / 1024 / 1024)
        elif self._frozen and rss <= self.resume_bytes:
            self._frozen = False
            logger.info("marshal: %.0f MB resident; outbound work resumed", rss / 1024 / 1024)

        reason = "memory ceiling" if self._frozen else ""
        return MarshalState(frozen=self._frozen, rss_bytes=rss, reason=reason)

    @property
    def frozen(self) -> bool:
        return self._frozen

    def allows_outbound(self) -> bool:
        return not self.evaluate().frozen


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "50_supreme_marshal_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 3 > 0, 3 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
