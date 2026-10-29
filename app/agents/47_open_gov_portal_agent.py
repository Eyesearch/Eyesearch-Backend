"""Government feeds as a seed source.

Open data portals and state government sites publish notifications, circulars
and updates as feeds. They are the highest-value seeds available: primary
sources, clearly dated, and the kind of page a search engine is judged on.

The feed reading itself already exists — agent 16 parses sitemaps and feeds, and
agent 23 orders them by freshness. What this adds is the short list of known
public portal paths to try, and the priority they should get.

Nothing here runs while the crawler is off; it produces candidates, and the
frontier decides.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Paths that public portals commonly publish, relative to the host.
PORTAL_PATHS = (
    "/sitemap.xml",
    "/rss.xml",
    "/feed",
    "/notifications",
    "/circulars",
)

#: Portals known to publish open feeds, without naming a paid service.
PORTAL_HOSTS = (
    "data.gov.in",
    "india.gov.in",
    "pgportal.gov.in",
)


@dataclass
class PortalReport:
    portals: int = 0
    feeds: int = 0
    seeds: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"portals": self.portals, "feeds": self.feeds, "seeds": self.seeds}


class OpenGovPortalAgent:
    """Offer government feed URLs to the frontier as high-priority seeds."""

    def __init__(self, frontier=None, scout=None) -> None:
        self._frontier = frontier
        self._scout = scout

    def candidates(self, hosts=None) -> list[str]:
        hosts = hosts or PORTAL_HOSTS
        return [f"https://{host}{path}" for host in hosts for path in PORTAL_PATHS]

    async def scan(self, hosts=None, *, dry_run: bool = False) -> PortalReport:
        report = PortalReport()
        for url in self.candidates(hosts):
            report.portals += 1
            if dry_run or self._frontier is None:
                continue
            host = url.split("/")[2]
            if await self._frontier.add(url, source_host=host):
                report.feeds += 1
                report.seeds.append(url)
        return report
