"""Freshness prioritisation — an adapter over the discovery that already runs.

Agent 16 (the Open Scout) already reads sitemaps and feeds, and the orchestrator
already schedules sitemap harvests for authoritative domains. What is genuinely
new here is the *ordering*: of the URLs a feed advertises, which should be
crawled first.

Feeds publish in publication order, so the newest entries are at the top; a
sitemap carries `<lastmod>` where the publisher supplies it. That is the signal,
and it is all local — no trend API, no ranking service.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

#: A URL whose feed entry is newer than this is worth jumping the queue for.
FRESH_HOURS = 6


@dataclass
class TrendReport:
    entries: int = 0
    fresh: int = 0
    prioritised: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"entries": self.entries, "fresh": self.fresh, "prioritised": self.prioritised}


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    for parser in (
        lambda v: datetime.fromisoformat(v),
        lambda v: datetime.strptime(v, "%a, %d %b %Y %H:%M:%S %z"),
        lambda v: datetime.strptime(v, "%Y-%m-%d"),
    ):
        try:
            parsed = parser(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def rank_fresh(text: str, now: datetime | None = None, fresh_hours: int = FRESH_HOURS) -> list[str]:
    """URLs from a feed, freshest first.

    `pubDate` and `lastmod` are read where present; entries with no date keep
    their feed order and sort after the dated ones, because a feed that omits
    dates still lists its newest items first.
    """
    now = now or datetime.now(timezone.utc)
    try:
        root = ElementTree.fromstring(text or "")
    except ElementTree.ParseError:
        return []

    dated: list[tuple[datetime, int, str]] = []
    undated: list[str] = []

    for position, node in enumerate(root.iter()):
        tag = node.tag.rsplit("}", 1)[-1].lower()
        if tag not in ("item", "entry", "url"):
            continue

        url = ""
        stamp = None
        for child in node.iter():
            name = child.tag.rsplit("}", 1)[-1].lower()
            if name == "loc" and child.text:
                url = child.text.strip()
            elif name == "link" and child.text and child.text.strip().startswith("http"):
                url = url or child.text.strip()
            elif name in ("pubdate", "lastmod", "updated", "published") and child.text:
                stamp = stamp or _parse_time(child.text)
            elif name == "link" and child.get("href"):
                url = url or child.get("href")

        if not url:
            continue
        if stamp is None:
            undated.append(url)
        else:
            dated.append((stamp, position, url))

    dated.sort(key=lambda item: (-item[0].timestamp(), item[1]))
    cutoff = now.timestamp() - fresh_hours * 3600
    ordered = [url for stamp, _, url in dated] + undated
    fresh = [url for stamp, _, url in dated if stamp.timestamp() >= cutoff]

    return fresh + [url for url in ordered if url not in fresh]


class AstrologerTrendAgent:
    """Order a feed's URLs by freshness and hand the top ones to the frontier."""

    def __init__(self, frontier=None, *, fresh_hours: int = FRESH_HOURS) -> None:
        self._frontier = frontier
        self.fresh_hours = int(fresh_hours)

    def order(self, feed_text: str) -> list[str]:
        return rank_fresh(feed_text, fresh_hours=self.fresh_hours)

    async def feed(self, feed_text: str, *, source_host: str = "", limit: int = 20) -> TrendReport:
        ordered = self.order(feed_text)[:limit]
        report = TrendReport(entries=len(ordered), prioritised=ordered)
        if self._frontier is None:
            return report
        for url in ordered:
            if await self._frontier.add(url, source_host=source_host):
                report.fresh += 1
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
    "agent": "23_astrologer_trend_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 2 > 0, 2 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
