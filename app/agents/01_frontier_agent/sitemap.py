"""Sitemap and robots.txt harvesting.

A domain that publishes a sitemap tells us exactly which pages it considers
canonical, which is better than guessing from navigation links. This module
reads `robots.txt` for `Sitemap:` directives, falls back to `/sitemap.xml` and
`/sitemap_index.xml`, and parses both plain and nested (`<sitemapindex>`) forms.

Parsing is incremental: `iterparse` over a stream with `elem.clear()` after each
entry, so a 50,000-URL sitemap never becomes a 50,000-item list.
"""

from __future__ import annotations

import io
import logging
from typing import Iterable, Iterator
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree

logger = logging.getLogger(__name__)

# Files that are never content pages.
ASSET_SUFFIXES: tuple[str, ...] = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".tif",
    ".zip", ".rar", ".7z", ".gz", ".tar", ".exe", ".msi", ".apk", ".dmg",
    ".css", ".js", ".json", ".xml", ".rss", ".atom", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".avi", ".mov", ".wmv", ".flv", ".webm", ".wav",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".csv",
)

STANDARD_SITEMAP_PATHS: tuple[str, ...] = ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml")

MAX_SITEMAPS_PER_HOST = 8
MAX_URLS_PER_HOST = 200


def _local(tag: str) -> str:
    """`{http://www.sitemaps.org/schemas/sitemap/0.9}loc` → `loc`."""
    return tag.rsplit("}", 1)[-1].lower()


def sitemap_urls_from_robots(robots_text: str, base_url: str) -> list[str]:
    """Every `Sitemap:` directive in a robots.txt, made absolute."""
    found: list[str] = []
    for line in (robots_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition(":")
        if key.strip().lower() != "sitemap":
            continue
        candidate = urljoin(base_url, value.strip())
        if candidate and candidate not in found:
            found.append(candidate)
    return found


def is_content_url(url: str, *, host: str | None = None) -> bool:
    """True for a page worth indexing: right host, no asset extension."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    if host and parsed.netloc.lower().removeprefix("www.") != host.lower().removeprefix("www."):
        return False
    path = parsed.path.lower()
    if not path or path == "/":
        return True
    return not path.endswith(ASSET_SUFFIXES)


def _iter_locs(stream: io.BufferedIOBase | io.StringIO, *, limit: int) -> Iterator[str]:
    """Yield `<loc>` values incrementally, clearing each element as it is read."""
    count = 0
    try:
        for _event, element in ElementTree.iterparse(stream, events=("end",)):
            if _local(element.tag) != "loc":
                continue
            value = (element.text or "").strip()
            element.clear()
            if not value:
                continue
            yield value
            count += 1
            if count >= limit:
                return
    except ElementTree.ParseError as exc:
        logger.debug("sitemap parse stopped early: %s", exc)


def iter_sitemap_urls(xml_text: str, *, limit: int = MAX_URLS_PER_HOST) -> Iterator[str]:
    """Content URLs from a `<urlset>` sitemap."""
    if not xml_text or "<urlset" not in xml_text:
        return iter(())
    return _iter_locs(io.StringIO(xml_text), limit=limit)


def iter_sitemap_index(xml_text: str, *, limit: int = MAX_SITEMAPS_PER_HOST) -> Iterator[str]:
    """Child sitemap URLs from a `<sitemapindex>`."""
    if not xml_text or "<sitemapindex" not in xml_text:
        return iter(())
    return _iter_locs(io.StringIO(xml_text), limit=limit)


def looks_like_sitemap(xml_text: str) -> bool:
    return bool(xml_text) and ("<urlset" in xml_text or "<sitemapindex" in xml_text)


def harvest_urls(
    documents: Iterable[tuple[str, str]],
    *,
    host: str,
    limit: int = MAX_URLS_PER_HOST,
) -> list[str]:
    """Content URLs from a set of already-fetched sitemap documents."""
    collected: list[str] = []
    seen: set[str] = set()
    for _url, text in documents:
        for candidate in iter_sitemap_urls(text, limit=limit):
            if len(collected) >= limit:
                return collected
            if candidate in seen or not is_content_url(candidate, host=host):
                continue
            seen.add(candidate)
            collected.append(candidate)
    return collected


__all__ = [
    "ASSET_SUFFIXES",
    "STANDARD_SITEMAP_PATHS",
    "MAX_SITEMAPS_PER_HOST",
    "MAX_URLS_PER_HOST",
    "harvest_urls",
    "is_content_url",
    "iter_sitemap_index",
    "iter_sitemap_urls",
    "looks_like_sitemap",
    "sitemap_urls_from_robots",
]
