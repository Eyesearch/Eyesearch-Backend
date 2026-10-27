"""Sitemap and robots.txt harvesting.

A domain that publishes a sitemap tells us exactly which pages it considers
canonical, which is better than guessing from navigation links. This module
reads `robots.txt` for `Sitemap:` directives, falls back to `/sitemap.xml` and
`/sitemap_index.xml`, and parses both plain and nested (`<sitemapindex>`) forms.

Parsing is incremental: `iterparse` over a stream with `elem.clear()` after each
entry, so a 50,000-URL sitemap never becomes a 50,000-item list.
"""

from __future__ import annotations

import gzip
import io
import logging
import zlib
from typing import IO, Iterable, Iterator
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

STANDARD_SITEMAP_PATHS: tuple[str, ...] = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemap.xml.gz",
    "/sitemap_index.xml.gz",
)

GZIP_MAGIC = b"\x1f\x8b"

MAX_SITEMAPS_PER_HOST = 8
MAX_URLS_PER_HOST = 200


def is_gzip_payload(payload: bytes | str | None) -> bool:
    """True when the bytes are a gzip member (magic header), whatever the name."""
    return isinstance(payload, (bytes, bytearray)) and bytes(payload[:2]) == GZIP_MAGIC


def open_stream(payload: bytes | str | None, *, url: str = "") -> IO[str] | None:
    """A text stream over a sitemap, decompressing gzip as it is read.

    `gzip.GzipFile` decompresses lazily, so `iterparse` reads the archive
    incrementally rather than the whole payload being inflated in memory at once.
    """
    if payload is None:
        return None

    if isinstance(payload, (bytes, bytearray)):
        raw = bytes(payload)
        if is_gzip_payload(raw) or url.lower().endswith(".gz"):
            try:
                return io.TextIOWrapper(gzip.GzipFile(fileobj=io.BytesIO(raw)), encoding="utf-8", errors="replace")
            except (OSError, EOFError, zlib.error) as exc:
                logger.debug("gzip sitemap failed to open (%s): %s", url, exc)
                return None
        return io.StringIO(raw.decode("utf-8", "replace"))

    return io.StringIO(payload)


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
    except (ElementTree.ParseError, OSError, EOFError, zlib.error) as exc:
        # malformed XML, or a gzip member that fails part-way through
        logger.debug("sitemap parse stopped early: %s", exc)


def iter_sitemap_urls(
    payload: bytes | str | None, *, limit: int = MAX_URLS_PER_HOST, url: str = ""
) -> Iterator[str]:
    """Content URLs from a `<urlset>` sitemap, plain or gzipped."""
    stream = open_stream(payload, url=url)
    if stream is None:
        return iter(())
    return _iter_locs(stream, limit=limit)


def iter_sitemap_index(
    payload: bytes | str | None, *, limit: int = MAX_SITEMAPS_PER_HOST, url: str = ""
) -> Iterator[str]:
    """Child sitemap URLs from a `<sitemapindex>`, plain or gzipped."""
    stream = open_stream(payload, url=url)
    if stream is None:
        return iter(())
    return _iter_locs(stream, limit=limit)


def looks_like_sitemap(payload: bytes | str | None) -> bool:
    """Whether this is a sitemap at all, gzipped or not."""
    if not payload:
        return False
    if is_gzip_payload(payload):
        return True
    if isinstance(payload, (bytes, bytearray)):
        return b"<urlset" in bytes(payload) or b"<sitemapindex" in bytes(payload)
    return "<urlset" in payload or "<sitemapindex" in payload


def harvest_urls(
    documents: Iterable[tuple[str, bytes | str]],
    *,
    host: str,
    limit: int = MAX_URLS_PER_HOST,
) -> list[str]:
    """Content URLs from a set of already-fetched sitemap documents."""
    collected: list[str] = []
    seen: set[str] = set()
    for url, text in documents:
        for candidate in iter_sitemap_urls(text, limit=limit, url=url):
            if len(collected) >= limit:
                return collected
            if candidate in seen or not is_content_url(candidate, host=host):
                continue
            seen.add(candidate)
            collected.append(candidate)
    return collected


__all__ = [
    "GZIP_MAGIC",
    "is_gzip_payload",
    "open_stream",
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
