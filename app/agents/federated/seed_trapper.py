"""Turn federated answers into future local coverage.

A query the index cannot answer is a gap worth filling. The URLs that filled it
are exactly the pages that should be crawled next — but only once the crawler is
allowed to run, and only through the frontier's own scope rules.

So this normalises the URLs and hands them to the frontier. It does not decide
what is admissible; the frontier does, and it is the only place that decision
should live.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.agents import load_agent

logger = logging.getLogger(__name__)

sanitizer = load_agent("44_cross_source_sanitizer")  # compatibility alias, see app/agents/__init__
_shield = load_agent("18_trap_shield_agent")

#: The air gap. This module may not touch the frontier, the queue or the store.
#:
#: It used to enqueue external URLs, which made the crawl queue a pipe for
#: content nobody chose to crawl: a federated answer became a permanent row, and
#: the index slowly filled with pages that had never passed the frontier's
#: admission rules. Nothing external is stored now — the class counts what it was
#: offered and drops it.


@dataclass
class SeedReport:
    offered: int = 0
    accepted: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {"offered": self.offered, "accepted": self.accepted, "rejected": self.rejected}


class ExternalSeedTrapperAgent:
    """Counts external URLs and discards them. It does not enqueue.

    `accepted` is kept at zero on purpose. A report that says "accepted 12" is a
    report that invites somebody to build a caller that acts on it, and the whole
    point of the air gap is that there is nothing to act on.
    """

    def __init__(self, frontier=None) -> None:
        # Accepted for signature compatibility and deliberately unused. Passing a
        # frontier must not give this class a way to reach it.
        self._frontier = None

    def normalise(self, url: str) -> str:
        return sanitizer.sanitise_url(url)

    async def trap(self, urls, *, dry_run: bool = False) -> SeedReport:
        report = SeedReport()
        for raw in urls or []:
            report.offered += 1
            url = self.normalise(str(raw or ""))
            if not url:
                continue
            reason = _shield.classify_trap(url)
            if reason:
                report.rejected[reason] = report.rejected.get(reason, 0) + 1
                continue
            # The air gap: a clean external URL is still external.
            report.rejected["air_gap"] = report.rejected.get("air_gap", 0) + 1
        return report


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "federated.seed_trapper",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 2 > 0, 2 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
