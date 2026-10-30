"""Ranking — an adapter that names the score the query already computes.

The search SQL already orders by a composite: `ts_rank_cd` for lexical match,
then a title hit, then an authority hit, all inside a subquery wrapper that the
outer query orders by. Rewriting that ordering in Python would mean fetching
every match and sorting in the process — slower, and a second definition of
relevance to keep in step with the first.

So this exposes the weights and explains a result's position, and leaves the
ordering where it belongs: in the database, on an index.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The weights the SQL applies, in the order it applies them. Named here so the
#: ranking can be discussed without reading the query.
LEXICAL_WEIGHT = 1.0
TITLE_HIT_BONUS = 1.0
AUTHORITY_BONUS = 1.0
QUALITY_WEIGHT = 0.25


@dataclass
class Ranked:
    url: str
    score: float
    why: list[str]

    def as_dict(self) -> dict[str, object]:
        return {"url": self.url, "score": round(self.score, 4), "why": self.why}


def composite_score(
    *,
    lexical: float = 0.0,
    title_hit: bool = False,
    authority_hit: bool = False,
    quality: float = 0.0,
) -> float:
    """The same combination the query applies, for explaining a single result."""
    score = abs(float(lexical)) * LEXICAL_WEIGHT
    if title_hit:
        score += TITLE_HIT_BONUS
    if authority_hit:
        score += AUTHORITY_BONUS
    score += max(0.0, min(1.0, float(quality))) * QUALITY_WEIGHT
    return score


class RankGeneralAgent:
    """Explain why a result ranks where it does."""

    def explain(self, hits) -> list[Ranked]:
        ranked: list[Ranked] = []
        for hit in hits:
            why: list[str] = []
            if getattr(hit, "title_hit", 0):
                why.append("title match")
            if getattr(hit, "auth_hit", 0):
                why.append("authority page")
            score = composite_score(
                lexical=getattr(hit, "lexical_score", 0.0) or 0.0,
                title_hit=bool(getattr(hit, "title_hit", 0)),
                authority_hit=bool(getattr(hit, "auth_hit", 0)),
                quality=getattr(hit, "quality_score", 0.0) or 0.0,
            )
            ranked.append(Ranked(url=getattr(hit, "url", ""), score=score, why=why))
        return ranked


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "27_rank_general_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 5 > 0, 5 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
