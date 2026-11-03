"""Blend the crawled index with the external fleet, and rank the result.

The search page has two kinds of answer available. The index holds pages we
crawled and verified; the fleet can add a definition or an authoritative link we
never crawled. Showing them as one undifferentiated list would be dishonest —
the reader cannot tell which is which — so they stay in separate slots and the
blender decides which slot leads.

Three rules shape everything here:

- The organic result always survives. Whatever the fleet does — hang, fail, return
  nothing — the pages we crawled are returned unchanged. A federated fault may
  cost a knowledge panel; it may never cost a search result.
- The budget is hard. 850 ms, and the answer is whatever has arrived by then.
- Nothing is written. The blender is pure: it takes inputs and returns a payload.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: The whole federated leg gets this long. Above it the page is answered from the
#: index alone: a reader who searched deserves an answer more than a panel.
DEFAULT_TIMEOUT_MS = 850

#: How many federated cards may appear. The courier and sanitizer already cap at
#: twenty; this is the display ceiling, which is about the page, not the source.
DEFAULT_CARD_CEILING = 20

#: A card must beat this to be shown as the hero. Below it the panel still lists
#: it among others, because "probably relevant" is not "the answer".
HERO_THRESHOLD = 0.55

#: Words that ask for a definition rather than a destination.
INFORMATIONAL_OPENERS = (
    "what is", "what are", "who is", "who was", "define", "meaning of",
    "how does", "how do", "why is", "explain", "history of", "when was",
    "full form of", "kya hai",
)

#: How much each signal contributes. The weights sum to 1.0 so a score is a
#: proportion and the hero threshold means the same thing for every query.
WEIGHT_OVERLAP = 0.45
WEIGHT_TITLE = 0.25
WEIGHT_DENSITY = 0.15
WEIGHT_AUTHORITY = 0.15

#: Sources we have reason to trust more than a random result. Not a claim about
#: truth — a claim about edit history and attribution.
SOURCE_AUTHORITY = {
    "wikipedia": 1.0,
    "duckduckgo": 0.6,
    "federated": 0.6,
}

_WORD = re.compile(r"[^\W_]+", re.UNICODE)

#: Words that frame a question without naming anything. They are counted when
#: detecting intent and ignored when scoring: "what is karnataka" should score a
#: Karnataka article on the word that matters, not lose a third of its overlap to
#: the two that do not.
STOPWORDS = frozenset(
    {
        "a", "an", "the", "is", "are", "was", "were", "what", "who", "when",
        "where", "why", "how", "of", "in", "on", "for", "to", "and", "or",
        "do", "does", "did", "me", "tell", "about", "please", "full", "form",
        "meaning", "define", "explain", "kya", "hai",
    }
)


def _tokens(text: str) -> list[str]:
    return [token.lower() for token in _WORD.findall(text or "")]


def _content_tokens(text: str) -> set[str]:
    """The words that carry meaning, for scoring."""
    return {token for token in _tokens(text) if token not in STOPWORDS}


def _canonical(url: str) -> str:
    parts = urlsplit(url or "")
    path = (parts.path or "/").rstrip("/") or "/"
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}{path}"


@dataclass
class ScoredCard:
    """A federated card and the reasons it scored what it did."""

    card: Any
    score: float = 0.0
    signals: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = self.card.as_dict() if hasattr(self.card, "as_dict") else dict(self.card)
        payload["relevance"] = round(self.score, 4)
        return payload


class KnowledgeBlender:
    """Merge, rank and budget. Pure: no store, no queue, no client."""

    def __init__(
        self,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        ceiling: int = DEFAULT_CARD_CEILING,
        hero_threshold: float = HERO_THRESHOLD,
    ) -> None:
        self.timeout_ms = int(timeout_ms)
        self.ceiling = max(1, int(ceiling))
        self.hero_threshold = float(hero_threshold)

    # -- intent ---------------------------------------------------------

    @staticmethod
    def is_informational(query: str) -> bool:
        """Whether the query asks for an explanation rather than a destination."""
        text = " ".join(_tokens(query) or [])
        return any(text.startswith(opener) for opener in INFORMATIONAL_OPENERS)

    @staticmethod
    def is_navigational(query: str, organic: Sequence[dict]) -> bool:
        """Whether the reader is trying to reach a site they already named.

        A query whose words appear in the host of a crawled result is a
        destination, not a question, and the crawled page should lead.
        """
        words = _content_tokens(query)
        if not words:
            return False
        for result in organic:
            host = (urlsplit(str(result.get("url") or "")).hostname or "").lower()
            if not host:
                continue
            # Compared as a string as well as a token: "karnataka bank" names
            # karnatakabank.com, and the domain is one word where the query has
            # two. Token equality alone misses every concatenated brand.
            if words & set(_tokens(host.replace(".", " "))):
                return True
            if any(word in host for word in words if len(word) >= 4):
                return True
        return False

    # -- scoring --------------------------------------------------------

    def score(self, query: str, card: Any) -> ScoredCard:
        """How well a card answers this query, from four signals."""
        payload = card.as_dict() if hasattr(card, "as_dict") else dict(card)
        title = str(payload.get("title") or "")
        snippet = str(payload.get("snippet") or payload.get("snippet_short") or "")
        source = str(payload.get("source") or "")

        query_tokens = _content_tokens(query)
        title_tokens = set(_tokens(title))
        body_tokens = set(_tokens(snippet))

        overlap = (
            len(query_tokens & (title_tokens | body_tokens)) / len(query_tokens)
            if query_tokens else 0.0
        )
        # A title that contains every query word is a stronger signal than the
        # same words scattered through the snippet.
        title_match = len(query_tokens & title_tokens) / len(query_tokens) if query_tokens else 0.0
        density = min(1.0, len(body_tokens) / 40.0)
        authority = SOURCE_AUTHORITY.get(source, 0.5)

        total = (
            WEIGHT_OVERLAP * overlap
            + WEIGHT_TITLE * title_match
            + WEIGHT_DENSITY * density
            + WEIGHT_AUTHORITY * authority
        )
        return ScoredCard(
            card=card,
            score=round(total, 6),
            signals={
                "overlap": round(overlap, 4),
                "title_match": round(title_match, 4),
                "density": round(density, 4),
                "authority": authority,
            },
        )

    def rank(self, query: str, cards: Sequence[Any], organic: Sequence[dict] = ()) -> list[ScoredCard]:
        """Score, drop what the index already has, and sort.

        A federated card pointing at a page we crawled is a duplicate: the reader
        already has that answer, with a body we verified.
        """
        indexed = {_canonical(str(result.get("url") or "")) for result in organic}
        scored: list[ScoredCard] = []
        seen: set[str] = set()

        for card in cards:
            payload = card.as_dict() if hasattr(card, "as_dict") else dict(card)
            key = _canonical(str(payload.get("url") or ""))
            if not key or key in indexed or key in seen:
                continue
            seen.add(key)
            scored.append(self.score(query, card))

        scored.sort(key=lambda item: -item.score)
        return scored[: self.ceiling]

    # -- blending -------------------------------------------------------

    async def blend(
        self,
        query: str,
        organic: Sequence[dict],
        federated,
        *,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """The search payload: crawled results, ranked cards, and which leads.

        `federated` is an awaitable factory — a callable returning the cards — so
        this module never needs to know how they are fetched.
        """
        started = time.perf_counter()
        budget = (timeout_ms if timeout_ms is not None else self.timeout_ms) / 1000.0
        organic_results = list(organic)

        cards: list[Any] = []
        degraded = False
        if federated is not None:
            task = asyncio.ensure_future(federated())
            try:
                cards = list(await asyncio.wait_for(task, timeout=budget) or [])
            except asyncio.TimeoutError:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                degraded = True
                logger.info("blender: federated leg exceeded %d ms", budget * 1000)
            except Exception as error:  # noqa: BLE001 - a fault here costs a panel
                logger.warning("blender: federated leg failed: %s", error)
                degraded = True

        ranked = self.rank(query, cards, organic_results)
        payloads = [item.as_dict() for item in ranked]

        informational = self.is_informational(query)
        navigational = self.is_navigational(query, organic_results)

        primary = payloads[0] if payloads and ranked[0].score >= self.hero_threshold else None
        # A navigational query keeps the crawled page first even when a card is
        # confident: the reader typed a destination, not a question.
        if navigational and organic_results:
            primary = None

        took_ms = int((time.perf_counter() - started) * 1000)
        return {
            "query": query,
            "organic_results": organic_results,
            "knowledge_cards": payloads,
            "primary_card": primary,
            "meta": {
                "intent": "navigational" if navigational else ("informational" if informational else "general"),
                "organic_count": len(organic_results),
                "knowledge_count": len(payloads),
                "sources": sorted({str((card.get("source") or "")) for card in payloads}),
                "took_ms": took_ms,
                "degraded": degraded,
                "timeout_ms": int(budget * 1000),
                "ceiling": self.ceiling,
            },
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
    "agent": "federated.knowledge_blender",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule(
            "the_weights_sum_to_one",
            round(WEIGHT_OVERLAP + WEIGHT_TITLE + WEIGHT_DENSITY + WEIGHT_AUTHORITY, 6) == 1.0,
            True,
            "the scoring weights are a proportion, so a threshold means the same thing everywhere",
        ),
        _contract_rules.Rule(
            "the_hero_threshold_is_reachable",
            0.0 < HERO_THRESHOLD < 1.0,
            True,
            "a threshold at 0 makes every card a hero; one at 1 makes none",
        ),
        _contract_rules.Rule(
            "the_budget_is_under_a_second",
            0 < DEFAULT_TIMEOUT_MS < 1000,
            True,
            "a federated leg that may take a second is a leg the reader waits for",
        ),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)


__all__ = [
    "DEFAULT_CARD_CEILING",
    "DEFAULT_TIMEOUT_MS",
    "HERO_THRESHOLD",
    "KnowledgeBlender",
    "ScoredCard",
]
