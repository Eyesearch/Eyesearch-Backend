"""Organic web results from a SearXNG instance we run ourselves.

SearXNG is a meta-search front end: it queries the engines and returns their
results as JSON, with no key and no per-query cost. Running it ourselves is the
point — the queries go through a service on our own network rather than to a
commercial API, and the engines behind it are our choice.

It is optional. If no base URL is configured, this courier does nothing at all,
because a missing configuration is not a failure and must not look like one.
"""

from __future__ import annotations

import logging

from app.agents.federated.knowledge_courier import (
    MAX_CARDS,
    SNIPPET_FULL_CHARS,
    SNIPPET_SHORT_CHARS,
    TITLE_CHARS,
    KnowledgeCard,
    clean_query,
    is_cardable_url,
    sanitize_text,
    truncate_at_word,
)

logger = logging.getLogger(__name__)

#: How many results one query may take from the instance.
SEARXNG_RESULT_LIMIT = 10


def card_from_result(result: dict) -> KnowledgeCard | None:
    """One SearXNG result as a card, or None when it is not a page."""
    if not isinstance(result, dict):
        return None
    url = str(result.get("url") or "").strip()
    if not url or not is_cardable_url(url):
        return None

    title = sanitize_text(str(result.get("title") or ""))
    body = sanitize_text(str(result.get("content") or result.get("snippet") or ""))
    if not title and not body:
        return None

    engine = sanitize_text(str(result.get("engine") or "searxng"))
    return KnowledgeCard(
        title=truncate_at_word(title or body, TITLE_CHARS),
        extract=truncate_at_word(body or title, SNIPPET_SHORT_CHARS),
        url=url,
        source="searxng",
        snippet_full=truncate_at_word(body or title, SNIPPET_FULL_CHARS),
        brand_name=engine.title()[:50] if engine else "SearXNG",
    )


class SearXNGOCourier:
    """Results from a self-hosted SearXNG, or nothing when it is not configured."""

    def __init__(self, http=None, *, base_url: str = "", timeout: float = 0.8) -> None:
        self._http = http
        self.base_url = (base_url or "").rstrip("/")
        self.timeout = float(timeout)

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self._http is not None)

    async def search(self, query: str, limit: int = SEARXNG_RESULT_LIMIT) -> list[KnowledgeCard]:
        """No base URL means no request, and no log line either.

        An unconfigured optional source is the normal case for a fresh install,
        not something to warn about on every query.
        """
        if not self.enabled:
            return []

        safe = clean_query(query)
        if not safe:
            return []

        from urllib.parse import quote

        url = f"{self.base_url}/search?q={quote(safe)}&format=json&safesearch=1"
        try:
            payload = await self._http.get_json(url)
        except Exception as error:  # noqa: BLE001 - a source failing is not fatal
            logger.debug("searxng search failed: %s", error)
            return []

        cards: list[KnowledgeCard] = []
        for result in payload.get("results") or []:
            card = card_from_result(result)
            if card is not None:
                cards.append(card)
            if len(cards) >= min(limit, MAX_CARDS):
                break
        return cards


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "federated.searxng_courier",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule(
            "an_unconfigured_source_is_silent",
            True,
            True,
            "a missing optional base URL is a normal install, not a fault",
        ),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)


__all__ = ["SEARXNG_RESULT_LIMIT", "SearXNGOCourier", "card_from_result"]
