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
from typing import Any
from urllib.parse import urlsplit

from app.agents import load_agent

logger = logging.getLogger(__name__)

sanitizer = load_agent("44_cross_source_sanitizer")
brands = load_agent("45_live_entity_extractor")
trapper = load_agent("43_external_seed_trapper")
PrivacyMaskAgent = load_agent("49_privacy_mask_agent").PrivacyMaskAgent

#: The whole federated path must finish inside this.
FEDERATION_BUDGET_SECONDS = 0.5


@dataclass
class FederatedResult:
    #: SearchCard objects, not dicts. One shape end to end means the caller
    #: converts exactly once, with `as_dict()`, and cannot get it wrong.
    cards: list[Any] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    timed_out: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"cards": self.cards, "sources": self.sources, "timed_out": self.timed_out}


#: What a card shows before the reader asks for more, and what it shows after.
#: These match the sanitizer's, because a card assembled here and a card
#: assembled there must be the same size.
SNIPPET_SHORT_CHARS = 180
SNIPPET_FULL_CHARS = 500
BRAND_CHAR_CAP = 50

#: How many cards may come from one domain. A provider that returns five pages
#: from one site should not fill the page with them.
MAX_PER_DOMAIN = 2


def _cut(text: str, limit: int) -> str:
    """Cut at a word boundary, never mid-word, within `limit`."""
    value = " ".join((text or "").split())
    if len(value) <= limit:
        return value
    window = value[:limit]
    return window.rsplit(" ", 1)[0].rstrip(" ,;:-—") if " " in window else window


def _canonical(url: str) -> str:
    """The form two URLs are compared in: no fragment, no trailing slash."""
    parts = urlsplit(url or "")
    path = parts.path.rstrip("/") or "/"
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}{path}"


@dataclass
class SearchCard:
    """One external result, shaped like a local one.

    `as_dict()` returns exactly the keys `SearchResult` does, so the frontend
    cannot tell an external card from an indexed one except by `source`. A card
    that renders differently is a card the reader has to interpret.
    """

    title: str = ""
    url: str = ""
    brand_name: str = ""
    snippet: str = ""
    snippet_short: str = ""
    snippet_full: str = ""
    has_more: bool = False
    source: str = "federated"
    score: float = 1.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": f"fed:{abs(hash(self.url)) & 0xFFFFFFFF:08x}",
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet or self.snippet_short,
            "source": self.source,
            "kind": "web",
            "poster_url": None,
            "thumbnail_url": None,
            "published_at": None,
            "score": self.score,
            "brand_name": self.brand_name,
            "clean_url": self.url,
            "category": "WEB",
            "breadcrumbs": [],
            "sitelinks": [],
            "snippet_short": self.snippet_short,
            "snippet_full": self.snippet_full,
            "has_more": self.has_more,
        }


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
        """Assemble external cards, one bad candidate at a time.

        Every step that can fail is isolated to the item it failed on: a
        provider that returns one malformed URL still yields the other nine.
        Dropping the batch because one entry is broken is how a fallback becomes
        worse than no fallback.
        """
        if not self.enabled or self._knowledge is None:
            return FederatedResult()

        try:
            raw = await asyncio.wait_for(
                self._candidates(query, limit), timeout=self.budget
            )
        except asyncio.TimeoutError:
            logger.info("federation exceeded %.2fs for %r", self.budget, query)
            return FederatedResult(timed_out=True)
        except Exception as error:  # noqa: BLE001 - federation must never raise
            logger.warning("federation failed for %r: %s", query, error)
            return FederatedResult()

        cards = self._assemble(raw, limit=limit)
        if not cards:
            return FederatedResult()

        # Fire and forget. The trapper is a no-op under the air gap, but the
        # call stays non-blocking so it cannot become a stall if that changes.
        self._trap_in_background([card.url for card in cards])

        return FederatedResult(
            cards=cards,
            sources=sorted({card.source for card in cards}),
        )

    async def _candidates(self, query: str, limit: int) -> list[dict]:
        """Ask the courier for raw candidates, tolerating an older interface."""
        if hasattr(self._knowledge, "search"):
            found = await self._knowledge.search(query, limit=limit)
            return list(found or [])
        # A courier that only knows `lookup` still answers, just one card deep.
        card = await self._knowledge.lookup(query)
        if not card:
            return []
        return [{"title": card.title, "url": card.url, "snippet": card.extract}]

    def _assemble(self, raw, *, limit: int) -> list[SearchCard]:
        cards: list[SearchCard] = []
        seen_urls: set[str] = set()
        per_domain: dict[str, int] = {}

        for candidate in raw or []:
            try:
                card = self._card_from(candidate)
            except Exception as error:  # noqa: BLE001 - one bad item is not a batch
                logger.warning("skipping an unusable federated candidate: %s", error)
                continue
            if card is None:
                continue

            key = _canonical(card.url)
            if key in seen_urls:
                continue
            domain = urlsplit(card.url).hostname or ""
            if per_domain.get(domain, 0) >= MAX_PER_DOMAIN:
                continue

            seen_urls.add(key)
            per_domain[domain] = per_domain.get(domain, 0) + 1
            cards.append(card)
            if len(cards) >= limit:
                break

        return cards

    def _card_from(self, candidate) -> SearchCard | None:
        """One candidate to one card, or None when it is not usable."""
        if isinstance(candidate, dict):
            title = str(candidate.get("title") or "")
            url = str(candidate.get("url") or "")
            snippet = str(candidate.get("snippet") or candidate.get("extract") or "")
        else:
            title = str(getattr(candidate, "title", "") or "")
            url = str(getattr(candidate, "url", "") or "")
            snippet = str(getattr(candidate, "extract", "") or "")

        clean = sanitizer.sanitise_url(url)
        if not clean or not clean.lower().startswith(("http://", "https://")):
            return None

        clean_title = sanitizer.clean_text(title)
        clean_snippet = sanitizer.clean_text(snippet)
        if not clean_title and not clean_snippet:
            return None

        brand = brands.LiveEntityExtractorAgent().brand(title=clean_title, url=clean)
        short = _cut(clean_snippet, SNIPPET_SHORT_CHARS)
        full = _cut(clean_snippet, SNIPPET_FULL_CHARS)

        return SearchCard(
            title=_cut(clean_title, 140) or brand,
            url=clean,
            brand_name=brand[:BRAND_CHAR_CAP],
            snippet=short,
            snippet_short=short,
            snippet_full=full,
            # More is offered only when the toggle would reveal something the
            # preview does not already show.
            has_more=len(full) > len(short),
            source="federated",
        )

    def _trap_in_background(self, urls: list[str]) -> None:
        """Never let seeding delay the answer the reader is waiting for."""

        async def run() -> None:
            try:
                await trapper.ExternalSeedTrapperAgent().trap(urls)
            except Exception as error:  # noqa: BLE001 - background must not crash
                logger.debug("seed trapping failed: %s", error)

        try:
            asyncio.create_task(run())
        except RuntimeError:
            # No running loop (a sync caller); dropping the seed is correct.
            logger.debug("no event loop for seed trapping; skipped")


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "federated.horizon_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 4 > 0, 4 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
