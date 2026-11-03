"""Instant facts from Wikidata.

Wikipedia's search answers "what pages mention this"; Wikidata answers "what is
this thing". For a query naming an entity — an institution, a capital, a person —
the entity's own description is the shortest correct answer available, and it
comes from a source that states where the claim came from.

Free, no key, one request.
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

#: Wikidata's entity search. `wbsearchentities` matches labels and aliases, which
#: is what makes it work on "ISRO" as well as "Indian Space Research Organisation".
WIKIDATA_SEARCH = "https://www.wikidata.org/w/api.php"

#: How many entities one query may contribute.
WIKIDATA_RESULT_LIMIT = 5


def card_from_entity(entity: dict) -> KnowledgeCard | None:
    """One `wbsearchentities` hit as a card, or None when it says nothing."""
    if not isinstance(entity, dict):
        return None
    identifier = str(entity.get("id") or "").strip()
    label = sanitize_text(str(entity.get("label") or entity.get("match", {}).get("text") or ""))
    description = sanitize_text(str(entity.get("description") or ""))
    if not identifier or not (label or description):
        return None

    url = f"https://www.wikidata.org/wiki/{identifier}"
    if not is_cardable_url(url):
        return None

    body = description or label
    return KnowledgeCard(
        title=truncate_at_word(label or description, TITLE_CHARS),
        extract=truncate_at_word(body, SNIPPET_SHORT_CHARS),
        url=url,
        source="wikidata",
        snippet_full=truncate_at_word(body, SNIPPET_FULL_CHARS),
        brand_name="Wikidata",
    )


class WikidataCourier:
    """Entity descriptions for a query, in the same shape as the other sources."""

    def __init__(self, http=None, *, timeout: float = 0.8) -> None:
        self._http = http
        self.timeout = float(timeout)

    async def search(self, query: str, limit: int = WIKIDATA_RESULT_LIMIT) -> list[KnowledgeCard]:
        safe = clean_query(query)
        if not safe or self._http is None:
            return []

        from urllib.parse import urlencode

        params = {
            "action": "wbsearchentities", "search": safe, "language": "en",
            "uselang": "en", "format": "json", "limit": str(max(1, int(limit))),
        }
        try:
            payload = await self._http.get_json(f"{WIKIDATA_SEARCH}?{urlencode(params)}")
        except Exception as error:  # noqa: BLE001 - a source failing is not fatal
            logger.debug("wikidata search failed: %s", error)
            return []

        cards: list[KnowledgeCard] = []
        for entity in (payload.get("search") or [])[: max(1, int(limit))]:
            card = card_from_entity(entity)
            if card is not None:
                cards.append(card)
        return cards[: min(limit, MAX_CARDS)]


# ---------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "federated.wikidata_courier",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule(
            "a_card_needs_an_identity",
            bool(WIKIDATA_RESULT_LIMIT > 0),
            True,
            "an entity without an id cannot be linked or checked",
        ),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)


__all__ = ["WIKIDATA_RESULT_LIMIT", "WIKIDATA_SEARCH", "WikidataCourier", "card_from_entity"]
