"""A definition at the top of the page, from open endpoints.

When someone searches a name rather than a topic, the useful answer is a short
definition, and the index may hold nothing about it. Wikipedia's summary endpoint
and DuckDuckGo's instant-answer endpoint are both public, need no key, and answer
in one request.

Neither is called unless federation is switched on, and both are bounded to the
same short timeout as the rest of the federated path. The result is attributed to
its source — a card that hides where its text came from is worse than no card.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Wikipedia's REST summary endpoint. Public, no key, JSON.
WIKIPEDIA_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/{title}"
#: DuckDuckGo's instant answer endpoint. Public, no key, JSON.
DUCKDUCKGO_IA = "https://api.duckduckgo.com/?q={query}&format=json&no_html=1&skip_disambig=1"

#: The federated budget. A knowledge card that arrives late is worse than none.
TIMEOUT_SECONDS = 1.5


@dataclass
class KnowledgeCard:
    title: str = ""
    extract: str = ""
    url: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "extract": self.extract,
            "url": self.url,
            "source": self.source,
        }

    def __bool__(self) -> bool:
        return bool(self.extract)


class FreeKnowledgeCourierAgent:
    """Fetch a public definition for a query, if one exists."""

    def __init__(self, http=None, *, timeout: float = TIMEOUT_SECONDS) -> None:
        self._http = http
        self.timeout = float(timeout)

    async def _get_json(self, url: str) -> dict:
        if self._http is None:
            return {}
        try:
            return await self._http.get_json(url, timeout=self.timeout)
        except Exception as error:  # noqa: BLE001 - federation must never raise
            logger.debug("knowledge fetch failed for %s: %s", url, error)
            return {}

    async def wikipedia(self, query: str) -> KnowledgeCard:
        from urllib.parse import quote

        payload = await self._get_json(WIKIPEDIA_SUMMARY.format(title=quote(query.strip())))
        extract = str(payload.get("extract") or "").strip()
        if not extract:
            return KnowledgeCard()
        return KnowledgeCard(
            title=str(payload.get("title") or query).strip(),
            extract=extract,
            url=str((payload.get("content_urls", {}).get("desktop", {}) or {}).get("page") or ""),
            source="wikipedia",
        )

    async def duckduckgo(self, query: str) -> KnowledgeCard:
        from urllib.parse import quote

        payload = await self._get_json(DUCKDUCKGO_IA.format(query=quote(query.strip())))
        abstract = str(payload.get("AbstractText") or "").strip()
        if not abstract:
            return KnowledgeCard()
        return KnowledgeCard(
            title=str(payload.get("Heading") or query).strip(),
            extract=abstract,
            url=str(payload.get("AbstractURL") or ""),
            source="duckduckgo",
        )

    async def lookup(self, query: str) -> KnowledgeCard:
        """Wikipedia first: its summaries are longer and better attributed."""
        card = await self.wikipedia(query)
        if card:
            return card
        return await self.duckduckgo(query)
