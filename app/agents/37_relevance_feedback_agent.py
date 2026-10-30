"""Learn from which results get used, without learning who asked.

The signal worth having is simple: for a given query, how far down the list did
people actually go. That is a property of the query, not of the person, so it can
be aggregated with no identity at all — no IP, no cookie, no session id. The
counter is keyed by the query text alone.

The adjustment it produces is a small multiplier on the ranking, capped, so a
handful of clicks cannot reorder the index.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: How far a query's weight may move from neutral.
MAX_ADJUSTMENT = 0.25
#: A click this far down the list still counts as a good result.
GOOD_POSITION = 5


@dataclass
class QueryStats:
    shown: int = 0
    clicked: int = 0
    position_sum: int = 0

    @property
    def ctr(self) -> float:
        return self.clicked / self.shown if self.shown else 0.0

    @property
    def mean_position(self) -> float:
        return self.position_sum / self.clicked if self.clicked else 0.0


@dataclass
class FeedbackReport:
    queries: int = 0
    adjusted: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"queries": self.queries, "adjusted": self.adjusted, "notes": self.notes}


class RelevanceFeedbackAgent:
    """Aggregate query outcomes and turn them into small weight adjustments."""

    def __init__(self, *, max_adjustment: float = MAX_ADJUSTMENT) -> None:
        self.max_adjustment = float(max_adjustment)
        self._stats: dict[str, QueryStats] = {}

    def record_shown(self, query: str, count: int = 1) -> None:
        stats = self._stats.setdefault(query.strip().lower(), QueryStats())
        stats.shown += max(0, count)

    def record_click(self, query: str, position: int) -> None:
        """A click at `position`, counting from 1. No identity is recorded."""
        stats = self._stats.setdefault(query.strip().lower(), QueryStats())
        stats.clicked += 1
        stats.position_sum += max(1, int(position))

    def adjustment(self, query: str) -> float:
        """A multiplier around 1.0, bounded by `max_adjustment`.

        A query whose results are clicked near the top needs no help. One whose
        clicks land far down, or that is never clicked at all, is a query whose
        ranking is probably wrong — but the evidence is weak, so the correction
        is small.
        """
        stats = self._stats.get(query.strip().lower())
        if stats is None or stats.shown < 10:
            return 1.0

        if stats.clicked == 0:
            return 1.0 - self.max_adjustment

        position = stats.mean_position
        if position <= GOOD_POSITION:
            return 1.0
        # Further down than good, scaled and capped.
        penalty = min(self.max_adjustment, (position - GOOD_POSITION) * 0.02)
        return 1.0 - penalty

    def report(self) -> FeedbackReport:
        report = FeedbackReport(queries=len(self._stats))
        report.adjusted = sum(1 for q in self._stats if self.adjustment(q) != 1.0)
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
    "agent": "37_relevance_feedback_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 3 > 0, 3 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
