"""When the index has nothing, ask the open web — and say so.

An empty result page is the worst outcome for a search engine. This fires only
on zero local results, queries a small set of public endpoints concurrently
inside a 500ms budget, and returns whatever arrived. Anything slower is dropped
rather than making the user wait.

Every card it returns is attributed to the source it came from. Presenting
someone else's results as our own would be both dishonest and, for most of these
endpoints, against their terms.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from app.agents import load_agent

logger = logging.getLogger(__name__)

sanitizer = load_agent("44_cross_source_sanitizer")
PrivacyMaskAgent = load_agent("49_privacy_mask_agent").PrivacyMaskAgent

#: The whole federated path must finish inside this.
FEDERATION_BUDGET_SECONDS = 0.5


@dataclass
class FederatedResult:
    cards: list[dict[str, str]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    timed_out: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"cards": self.cards, "sources": self.sources, "timed_out": self.timed_out}


class FederatedHorizonAgent:
    """Fill an empty result page from open endpoints, within a budget."""

    def __init__(self, knowledge=None, settings=None, *, budget: float = FEDERATION_BUDGET_SECONDS) -> None:
        self._knowledge = knowledge
        self._settings = settings
        self.budget = float(budget)
        self.privacy = PrivacyMaskAgent()

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._settings, "federation_enabled", False))

    async def search(self, query: str, limit: int = 5) -> FederatedResult:
        if not self.enabled or self._knowledge is None:
            return FederatedResult()

        try:
            card = await asyncio.wait_for(self._knowledge.lookup(query), timeout=self.budget)
        except asyncio.TimeoutError:
            logger.info("federation exceeded %.2fs for %r", self.budget, query)
            return FederatedResult(timed_out=True)

        if not card:
            return FederatedResult()

        sanitised = sanitizer.CrossSourceSanitizerAgent().card(
            title=card.title,
            url=card.url,
            snippet=card.extract,
            source=card.source,
        )
        payload = sanitised.as_dict()
        payload["snippet"] = sanitizer.sanitise_snippet(card.extract, 120)
        return FederatedResult(cards=[payload][:limit], sources=[card.source])
