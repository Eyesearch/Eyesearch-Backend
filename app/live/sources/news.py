"""Headlines from RSS feeds that are free and meant to be read.

No key, no scraping: RSS is the interface the publishers publish. Each feed is
one fact holding its newest headline, and the headline carries the feed's own
timestamp so the reader knows how old it is.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

NAME = "news"
TTL_SECONDS = 1800
TIMEOUT_SECONDS = 10.0

#: Headlines kept per feed. Three is enough to answer "what is the news" and
#: few enough that one prolific feed cannot fill the answer on its own.
ITEMS_PER_FEED = 3

USER_AGENT = "EyesearchBot/1.0 (+https://eyesearch.in; contact@eyesearch.in)"

#: Feeds filled on every pass, one per country edition where the publisher
#: offers one. Government first: PIB is the feed whose headlines are the source
#: of record rather than a report of it.
#:
#: The Google News URLs are built here rather than written out, because every
#: one of them needs `hl`, `gl` and `ceid` and a request that loses them is
#: served the American edition — which is how an Indian product ended up quoting
#: a story about a Trump golf course. They were written as literals once and
#: arrived without their query strings, so all nine editions were the same feed.
GOOGLE_NEWS_EDITIONS: dict[str, tuple[str, str, str]] = {
    "in": ("en-IN", "IN", "IN:en"),
    "in_kn": ("kn", "IN", "IN:kn"),
    "in_hi": ("hi", "IN", "IN:hi"),
    "us": ("en-US", "US", "US:en"),
    "gb": ("en-GB", "GB", "GB:en"),
    "ae": ("en-AE", "AE", "AE:en"),
    "au": ("en-AU", "AU", "AU:en"),
    "ca": ("en-CA", "CA", "CA:en"),
    "sg": ("en-SG", "SG", "SG:en"),
}


def _google_news_url(language: str, country: str, edition: str) -> str:
    query = urlencode({"hl": language, "gl": country, "ceid": edition})
    return f"https://news.google.com/rss"


#: PIB's feed is not an RSS document at a fixed path; it is that script with
#: three parameters, and without them it answers an empty page.
PIB_URL = "https://www.pib.gov.in/RssMain.aspx?" + urlencode(
    {"ModId": "6", "Lang": "1", "Regid": "3"}
)

FEEDS: dict[str, str] = {
    "pib": PIB_URL,
    "thehindu": "https://www.thehindu.com/news/national/feeder/default.rss",
    "toi": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    **{
        name: _google_news_url(language, country, edition)
        for name, (language, country, edition) in GOOGLE_NEWS_EDITIONS.items()
    },
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
                    # Google News answers with a redirect, and not following it
                    # made that feed look healthy while contributing nothing.
                    response = await client.get(
                        url,
                        headers={
                            "Accept": "application/rss+xml, application/xml, text/xml;q=0.9",
                            "User-Agent": USER_AGENT,
                        },
                        follow_redirects=True,
                    )
                    response.raise_for_status()
                except Exception as error:  # noqa: BLE001 - one feed, not the pass
                    logger.warning("news feed %s failed: %s", name, error)
                    continue

                items = parse_items(response.text, limit=ITEMS_PER_FEED)
                if not items:
                    # A 200 with nothing in it is a feed that has changed shape,
                    # and silence about that is how a source looks alive while
                    # contributing nothing.
                    logger.warning("news feed %s returned no items", name)
                    continue
                for index, (title, link) in enumerate(items, start=1):
                    facts.append((f"news.{name}.{index}", title, link))
        finally:
            if self._http is None:
                await client.aclose()
        return facts
