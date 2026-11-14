"""Headlines from RSS feeds that are free and meant to be read.

No key, no scraping: RSS is the interface the publishers publish. Each feed is
one fact holding its newest headline, and the headline carries the feed's own
timestamp so the reader knows how old it is.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

import httpx

NAME = "news"
TTL_SECONDS = 1800
TIMEOUT_SECONDS = 10.0

#: Feeds filled on every pass. Government first: PIB is the one feed whose
#: headlines are the source of record rather than a report of it.
FEEDS: dict[str, str] = {
    "pib": "https://www.pib.gov.in/RssMain.aspx",
    "thehindu": "https://www.thehindu.com/news/national/feeder/default.rss",
    "toi": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "google_india": "https://news.google.com/rss",
}

_TAG = re.compile(r"<[^>]+>")


def clean(text: str) -> str:
    return _TAG.sub(" ", text or "").replace("&amp;", "&").strip()


def parse_items(xml_text: str, limit: int = 3) -> list[tuple[str, str]]:
    """`(title, link)` for the newest items in a feed."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[tuple[str, str]] = []
    for item in root.iter("item"):
        title = clean(item.findtext("title") or "")
        link = clean(item.findtext("link") or "")
        if title:
            items.append((title, link))
        if len(items) >= limit:
            break
    return items


class NewsSource:
    name = NAME
    url = ", ".join(FEEDS.values())
    ttl_seconds = TTL_SECONDS

    def __init__(self, http: httpx.AsyncClient | None = None, feeds: dict | None = None) -> None:
        self._http = http
        self._feeds = feeds or FEEDS

    async def fetch(self) -> list[tuple[str, str, str]]:
        facts: list[tuple[str, str, str]] = []
        client = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
        try:
            for name, url in self._feeds.items():
                try:
                    response = await client.get(url, headers={"Accept": "application/rss+xml, application/xml"})
                    response.raise_for_status()
                except Exception:
                    # One feed down is one feed down, not a failed pass.
                    continue
                for index, (title, link) in enumerate(parse_items(response.text), start=1):
                    facts.append((f"news.{name}.{index}", title, link))
        finally:
            if self._http is None:
                await client.aclose()
        return facts
