"""The query service the frontend talks to.

One place that turns a query into the exact JSON shapes `web/config/api-config.js`
parses:

* `search()`   → the card contract `GET /v1/search`
* `suggest()`  → the OpenSearch suggestions shape `[query, [titles]]`
* `retrieve()` → the flat context contract for AI clients

Every field the cards carry is either read from the index or `null`. Nothing is
invented: no fabricated thumbnails, no synthesised publish dates, no provider
snippets passed off as our own extract.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from app.engine.config import Settings, get_settings
from app.agents import load_agent
from app.engine.store import IndexStore, SearchHit, fts_tokens, highlight_terms

CATEGORIES = load_agent("07_classifier_agent").CATEGORIES
SUPPORTED_LANGUAGES = load_agent("08_language_agent").SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

SOURCE_LABEL = "eyesearch_index"

_DOCUMENT_EXTENSIONS = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")
_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg")
_NEWS_HOSTS = ("thehindu.com", "deccanherald.com", "prajavani.net", "ndtv.com", "timesofindia")

_YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "m.youtube.com")
_YOUTUBE_ID = re.compile(r"(?:v=|/)([A-Za-z0-9_-]{11})(?:[&?/]|$)")


#: How long a search result stays usable, and how many are kept.
CACHE_TTL_SECONDS = 120.0
CACHE_MAX_ENTRIES = 1000


class SearchCache:
    """A small TTL cache for search payloads.

    Keyed on everything that changes the answer — query, limit, page, filters,
    format and whether snippets were highlighted. Entries expire after
    `ttl_seconds`, and the oldest is dropped once `max_entries` is reached.

    The lock makes this safe if a caller ever runs it off the event loop; under
    asyncio's single-threaded model it is uncontended and costs a lock acquire.
    Payloads are copied in and out so a caller mutating a response cannot
    corrupt the cached copy.
    """

    def __init__(
        self,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES,
        clock=time.monotonic,
    ) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._clock = clock
        self._entries: "OrderedDict[tuple, tuple[float, dict[str, Any]]]" = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def get(self, key: tuple) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return None
            expires_at, payload = entry
            if expires_at <= self._clock():
                del self._entries[key]
                self.misses += 1
                return None
            self.hits += 1
            return copy.deepcopy(payload)

    def set(self, key: tuple, payload: dict[str, Any]) -> None:
        with self._lock:
            self._entries[key] = (self._clock() + self._ttl, copy.deepcopy(payload))
            self._entries.move_to_end(key)
            while len(self._entries) > self._max:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


@dataclass
class SearchResult:
    """One result card."""

    id: str
    title: str
    url: str
    snippet: str
    source: str = SOURCE_LABEL
    kind: str = "web"
    poster_url: str | None = None
    thumbnail_url: str | None = None
    published_at: str | None = None
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "kind": self.kind,
            "poster_url": self.poster_url,
            "thumbnail_url": self.thumbnail_url,
            "published_at": self.published_at,
            "score": self.score,
        }


def youtube_thumbnail(url: str) -> str | None:
    """The deterministic thumbnail for a YouTube video URL, or `None`.

    `i.ytimg.com/vi/<id>/hqdefault.jpg` is a pure function of the video id, so
    this is a derived value rather than a guess.
    """
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in _YOUTUBE_HOSTS:
        return None
    match = _YOUTUBE_ID.search(url)
    if not match:
        return None
    return f"https://i.ytimg.com/vi/{match.group(1)}/hqdefault.jpg"


def kind_of(url: str) -> str:
    """A coarse content type, derived only from the URL itself."""
    path = (urlparse(url).path or "").lower()
    host = (urlparse(url).hostname or "").lower()
    if host.endswith(_YOUTUBE_HOSTS) or host == "youtu.be":
        return "video"
    if path.endswith(_DOCUMENT_EXTENSIONS):
        return "document"
    if path.endswith(_IMAGE_EXTENSIONS):
        return "image"
    if any(host.endswith(news_host) for news_host in _NEWS_HOSTS):
        return "news"
    return "web"


def to_result(hit: SearchHit) -> SearchResult:
    """Map an index row onto the card contract."""
    return SearchResult(
        id=f"eyesearch:{hit.page_id}",
        title=hit.title or hit.url,
        url=hit.url,
        snippet=hit.snippet,
        kind=kind_of(hit.url),
        thumbnail_url=youtube_thumbnail(hit.url),
        score=hit.score,
    )


class SearchService:
    """Search, suggest and retrieve over the index."""

    def __init__(
        self,
        store: IndexStore,
        settings: Settings | None = None,
        cache: SearchCache | None = None,
    ) -> None:
        self._store = store
        self._settings = settings or get_settings()
        self.cache = cache if cache is not None else SearchCache()

    async def search(
        self,
        query: str,
        limit: int | None = None,
        page: int = 1,
        category: str | None = None,
        language: str | None = None,
        highlight: bool = False,
    ) -> dict[str, Any]:
        """The `GET /v1/search` payload."""
        started = time.perf_counter()
        size = self._clamp_limit(limit)
        page_number = max(1, min(page, self._settings.search_max_page))
        offset = (page_number - 1) * size

        key = ("cards", query, size, page_number, category, language, highlight)
        cached = self.cache.get(key)
        if cached is not None:
            cached["cache_hit"] = True
            cached["took_ms"] = 0
            return cached

        hits = await self._store.search(
            query,
            limit=size,
            offset=offset,
            category=category,
            language=language,
        )
        results = [to_result(hit).as_dict() for hit in hits]

        if highlight:
            tokens = fts_tokens(query)
            for result in results:
                result["snippet"] = highlight_terms(result["snippet"], tokens)

        took_ms = int((time.perf_counter() - started) * 1000)

        payload = {
            "query": query,
            "page": page_number,
            "limit": size,
            "total_estimate": len(results),
            "took_ms": took_ms,
            "degraded": False,
            "cache_hit": False,
            "providers": {SOURCE_LABEL: len(results)},
            "failed_providers": [],
            "filters": {"category": category, "language": language},
            "results": results,
        }
        self.cache.set(key, payload)
        return payload

    async def search_flat(
        self,
        query: str,
        limit: int | None = None,
        category: str | None = None,
        language: str | None = None,
        highlight: bool = False,
    ) -> dict[str, Any]:
        """The meta-search contract (`format=json`), shaped like the README."""
        started = time.perf_counter()
        size = self._clamp_limit(limit)

        key = ("json", query, size, category, language, highlight)
        cached = self.cache.get(key)
        if cached is not None:
            cached["cache_hit"] = True
            cached["took_ms"] = 0
            return cached

        hits = await self._store.search(
            query, limit=size, category=category, language=language
        )
        tokens = fts_tokens(query) if highlight else []

        payload = {
            "query": query,
            "total_hits": len(hits),
            "took_ms": int((time.perf_counter() - started) * 1000),
            "source": SOURCE_LABEL,
            "cache_hit": False,
            "results": [
                {
                    "title": hit.title or hit.url,
                    "url": hit.url,
                    "snippet": highlight_terms(hit.snippet, tokens) if highlight else hit.snippet,
                    "favicon": self._favicon(hit.url),
                }
                for hit in hits
            ],
        }
        self.cache.set(key, payload)
        return payload

    async def suggest(self, query: str, limit: int = 6) -> list[Any]:
        """The OpenSearch suggestions shape: `[query, [completions]]`."""
        try:
            titles = await self._store.suggest(query, limit=limit)
        except Exception as error:  # suggestions are a convenience, never an error
            logger.debug("suggest failed for %r: %s", query, error)
            return [query, []]
        return [query, titles]

    async def retrieve(
        self,
        query: str,
        limit: int | None = None,
        format: str = "json",
    ) -> dict[str, Any] | str:
        """Token-optimised context for AI clients."""
        size = self._clamp_limit(limit)
        hits = await self._store.search(query, limit=size)
        results = [
            {"title": hit.title or hit.url, "url": hit.url, "snippet": hit.snippet}
            for hit in hits
        ]
        if format == "markdown":
            return self._as_markdown(query, results)
        return {
            "query": query,
            "format": format,
            "degraded": False,
            "providers": {SOURCE_LABEL: len(results)},
            "failed_providers": [],
            "results": results,
            "context": [
                {"title": hit.title or hit.url, "url": hit.url, "text": hit.snippet}
                for hit in hits
            ],
        }

    async def stats(self) -> dict[str, Any]:
        stats = await self._store.stats()
        return stats.as_dict()

    @staticmethod
    def normalise_category(category: str | None) -> str | None:
        """Validate a `category` filter, or `None` when it was not supplied."""
        if category is None or not category.strip():
            return None
        value = category.strip().lower()
        if value not in CATEGORIES:
            raise ValueError(f"unknown category '{category}'")
        return value

    @staticmethod
    def normalise_language(language: str | None) -> str | None:
        """Validate a `lang` filter, or `None` when it was not supplied."""
        if language is None or not language.strip():
            return None
        value = language.strip().lower()
        if value not in SUPPORTED_LANGUAGES:
            raise ValueError(f"unsupported language '{language}'")
        return value

    # -- helpers -----------------------------------------------------------

    def _clamp_limit(self, limit: int | None) -> int:
        value = limit if isinstance(limit, int) and limit > 0 else self._settings.search_default_limit
        return max(1, min(value, self._settings.search_max_limit))

    @staticmethod
    def _favicon(url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        return f"https://{host}/favicon.ico" if host else ""

    @staticmethod
    def _as_markdown(query: str, results: list[dict[str, str]]) -> str:
        lines = [f"# {query}", ""]
        for index, result in enumerate(results, start=1):
            lines.append(f"{index}. **{result['title']}**")
            lines.append(f"   {result['url']}")
            if result["snippet"]:
                lines.append(f"   {result['snippet']}")
            lines.append("")
        return "\n".join(lines).strip() + "\n"


__all__ = [
    "CACHE_MAX_ENTRIES",
    "CACHE_TTL_SECONDS",
    "CATEGORIES",
    "SearchCache",
    "SOURCE_LABEL",
    "SUPPORTED_LANGUAGES",
    "SearchResult",
    "SearchService",
    "kind_of",
    "to_result",
    "youtube_thumbnail",
]
