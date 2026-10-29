"""Make an external result look like one of ours.

Anything arriving from outside has to satisfy the same rules the index enforces,
or the cards become inconsistent: a 300-character snippet next to 40-character
ones, a tracking URL that opens someone else's analytics, a title that is the
whole <title> tag.

The rules are imported from where they are already defined, not copied, so the
ceiling that governs ingestion governs federation too.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.agents import load_agent

logger = logging.getLogger(__name__)

_brand = load_agent("10_brand_canonical_agent")
_enforcer = load_agent("11_rule_enforcer_agent")

SNIPPET_CHARS = 40
strip_tracking = _enforcer.strip_tracking
action_title_from = _brand.action_title_from


@dataclass
class ExternalCard:
    title: str = ""
    url: str = ""
    snippet: str = ""
    brand: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "brand": self.brand,
            "source": self.source,
        }


def sanitise_url(url: str) -> str:
    """The URL with its tracking parameters removed."""
    return strip_tracking(url or "")


def sanitise_snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Collapse whitespace and cut at a word boundary within the ceiling."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


class CrossSourceSanitizerAgent:
    """Enforce our own rules on results we did not produce."""

    def card(self, *, title: str, url: str, snippet: str, source: str = "") -> ExternalCard:
        clean_url = sanitise_url(url)
        brand = _brand.brand_from_domain(clean_url) if clean_url else ""
        return ExternalCard(
            title=action_title_from(title=title, brand=brand, url=clean_url) or sanitise_snippet(title),
            url=clean_url,
            snippet=sanitise_snippet(snippet),
            brand=brand,
            source=source,
        )

    def cards(self, items) -> list[ExternalCard]:
        out: list[ExternalCard] = []
        for item in items or []:
            card = self.card(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("snippet") or item.get("text") or ""),
                source=str(item.get("source") or ""),
            )
            if card.url:
                out.append(card)
        return out
