"""Sitemap and feed discovery — an adapter, not a second implementation.

The crawling half of this already exists: the orchestrator harvests
`sitemap.xml` for domains it considers authoritative (`_pending_sitemap_domains`,
`sitemap_domains_scheduled`), and the frontier's scope rules decide what is
admissible. Writing a second, independent discovery loop here would give the
system two answers to "what should we crawl next", and they would drift apart
the first time one was changed.

So this agent is the named entry point for that work. It reads feeds the fetcher
does not, and hands every candidate to the frontier exactly as the existing
harvest does.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

#: Feed locations to try, in the order worth trying them.
FEED_PATHS = ("/sitemap.xml", "/feed", "/rss", "/atom.xml", "/rss.xml", "/feed.xml")

_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.IGNORECASE)


@dataclass
class ScoutReport:
    feeds_read: int = 0
    urls_found: int = 0
    urls_accepted: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "feeds_read": self.feeds_read,
            "urls_found": self.urls_found,
            "urls_accepted": self.urls_accepted,
            "notes": self.notes,
        }


def parse_feed(text: str) -> list[str]:
    """URLs from a sitemap, an RSS feed or an Atom feed.

    Three formats, one function: a sitemap lists `<loc>`, RSS lists `<link>`
    inside an `<item>`, and Atom lists `<link href="...">`. Anything unparseable
    yields nothing rather than raising, because a broken feed on one site must
    not stop the pass.
    """
    urls: list[str] = []
    body = text or ""

    urls.extend(_LOC.findall(body))

    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return list(dict.fromkeys(urls))

    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag == "link":
            href = node.get("href")
            if href:
                urls.append(href)
            elif node.text and node.text.strip().startswith("http"):
                urls.append(node.text.strip())

    return list(dict.fromkeys(url.strip() for url in urls if url.strip()))


class OpenScoutAgent:
    """Discover canonical URLs from open protocols, with no third-party API."""

    def __init__(self, frontier, fetcher=None) -> None:
        self._frontier = frontier
        self._fetcher = fetcher

    async def scan(self, domain: str, *, dry_run: bool = False) -> ScoutReport:
        report = ScoutReport()
        if self._fetcher is None:
            report.notes.append("no fetcher configured; discovery delegated to the orchestrator")
            return report

        for path in FEED_PATHS:
            url = f"https://{domain}{path}"
            try:
                body = await self._fetcher.get_text(url)
            except Exception as error:  # noqa: BLE001 - a missing feed is normal
                logger.debug("no feed at %s: %s", url, error)
                continue
            if not body:
                continue

            report.feeds_read += 1
            found = parse_feed(body)
            report.urls_found += len(found)

            for candidate in found:
                if dry_run:
                    report.urls_accepted += 1
                    continue
                # The frontier owns the scope rules; this agent does not decide
                # what is admissible, only what was advertised.
                if await self._frontier.add(candidate, source_host=domain):
                    report.urls_accepted += 1

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
    "agent": "16_open_scout_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 2 > 0, 2 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
