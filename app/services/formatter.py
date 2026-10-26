"""Response builders for the two output contracts.

* Human UI  -> structured result cards with media fields.
* AI client -> token-optimised markdown or JSON, boilerplate stripped.

No result is ever invented here: a missing thumbnail stays ``None``, and the
only derived media URL is a YouTube thumbnail, which is deterministic from the
video id.
"""

from __future__ import annotations

import hashlib
import html
import re
from typing import Any, Iterable, Protocol, Sequence
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.config import Settings, get_settings
from app.schemas import AIContextResult, SearchResult

# Query parameters that carry no identity for a page.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "msclkid",
    "ref",
    "ref_src",
    "spm",
    "igshid",
    "mc_cid",
    "mc_eid",
}

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_YOUTUBE_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?(?:.*?&)?v=|shorts/|embed/)|youtu\.be/)([A-Za-z0-9_-]{6,})"
)
_SENTENCE_END = (".", "!", "?", "。", "।")

PROVIDER_WEIGHT: dict[str, float] = {
    "cache": 1.0,
    "duckduckgo": 1.0,
    "duckduckgo_html": 0.92,
    "duckduckgo_lite": 0.86,
}


class RawResultLike(Protocol):
    """Structural type for a provider result (see ``live_retrieval.RawResult``)."""

    title: str
    url: str
    snippet: str
    source: str
    position: int


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def canonical_url(url: str) -> str:
    """Normalise a URL for deduplication (no tracking params, no trailing slash)."""
    if not url:
        return ""
    candidate = url.strip()
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return candidate
    if not parsed.scheme:
        parsed = urlparse("https://" + candidate)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS
    ]
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower() or "https", host, path, "", urlencode(query_pairs), ""))


def clean_snippet(text: str, max_chars: int = 260) -> str:
    """Strip tags/entities, collapse whitespace, trim at a sentence boundary."""
    if not text:
        return ""
    plain = _TAG_RE.sub(" ", html.unescape(text))
    plain = _WS_RE.sub(" ", plain).strip()
    if len(plain) <= max_chars:
        return plain
    window = plain[:max_chars]
    cut = max(window.rfind(ch) for ch in _SENTENCE_END)
    if cut >= max_chars // 2:
        return window[: cut + 1].strip()
    if " " in window:
        return window[: window.rfind(" ")].rstrip(",;:- ") + "…"
    return window.rstrip() + "…"


def classify_kind(url: str, title: str = "") -> str:
    """Coarse content type used by the UI to pick a card layout."""
    haystack = f"{url} {title}".lower()
    if _YOUTUBE_RE.search(url) or "vimeo.com" in haystack:
        return "video"
    if any(token in haystack for token in ("imdb.com/title", "rotten tomatoes", "letterboxd")):
        return "film"
    if any(token in haystack for token in ("/news", "news.", "reuters.com", "thehindu.com")):
        return "news"
    if any(token in haystack for token in (".pdf", "/pdf/")):
        return "document"
    if any(token in haystack for token in ("/image", "/photo", "imgur.com", "flickr.com")):
        return "image"
    return "web"


def derive_media(url: str) -> tuple[str | None, str | None]:
    """Deterministic media URLs only. Never fabricate a poster."""
    match = _YOUTUBE_RE.search(url)
    if match:
        video_id = match.group(1)
        return (
            f"https://img.youtube.com/vi/{video_id}/maxresdefault.jpg",
            f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg",
        )
    return (None, None)


def _score(source: str, position: int, index: int) -> float:
    weight = PROVIDER_WEIGHT.get(source, 0.8)
    rank_penalty = 0.04 * max(position, index)
    return round(max(0.05, weight - rank_penalty), 3)


def dedupe(raws: Iterable[Any]) -> list[Any]:
    """Drop duplicate URLs, keeping the first (best-ranked) occurrence."""
    seen: set[str] = set()
    unique: list[Any] = []
    for raw in raws:
        url = _get(raw, "url") or ""
        key = canonical_url(url)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(raw)
    return unique


def build_ui_results(
    raws: Iterable[Any],
    *,
    settings: Settings | None = None,
    limit: int | None = None,
) -> list[SearchResult]:
    """Turn raw provider results into bounded, scored UI cards."""
    cfg = settings or get_settings()
    cap = min(limit or cfg.default_limit, cfg.max_limit)
    cards: list[SearchResult] = []
    for index, raw in enumerate(dedupe(raws)[:cap]):
        url = (_get(raw, "url") or "").strip()
        if not url:
            continue
        source = _get(raw, "source") or "unknown"
        title = clean_snippet(_get(raw, "title") or url, 200)
        snippet = clean_snippet(_get(raw, "snippet") or "", cfg.snippet_max_chars)
        poster, thumbnail = derive_media(url)
        cards.append(
            SearchResult(
                id=f"{source}:{hashlib.sha1(canonical_url(url).encode()).hexdigest()[:10]}",
                title=title,
                url=url,
                snippet=snippet,
                source=source,
                kind=classify_kind(url, title),  # type: ignore[arg-type]
                poster_url=_get(raw, "image_url") or poster,
                thumbnail_url=thumbnail or _get(raw, "image_url"),
                published_at=_get(raw, "published_at"),
                score=_score(source, int(_get(raw, "position") or 0), index),
            )
        )
    return cards


def build_ai_results(
    raws: Iterable[Any],
    *,
    settings: Settings | None = None,
    limit: int | None = None,
) -> list[AIContextResult]:
    """The AI payload: title, URL and the minimum prose that answers."""
    cfg = settings or get_settings()
    cap = min(limit or cfg.default_limit, cfg.max_limit)
    out: list[AIContextResult] = []
    for raw in dedupe(raws)[:cap]:
        url = (_get(raw, "url") or "").strip()
        if not url:
            continue
        out.append(
            AIContextResult(
                title=clean_snippet(_get(raw, "title") or url, 160),
                # Canonical form: no tracking parameters, fewer tokens, same page.
                url=canonical_url(url),
                snippet=clean_snippet(_get(raw, "snippet") or "", cfg.ai_snippet_max_chars),
            )
        )
    return out


def to_markdown(
    query: str,
    results: Sequence[AIContextResult],
    *,
    failed_providers: Sequence[str] = (),
) -> str:
    """Markdown context for LLM clients. One result, three lines, nothing else."""
    lines: list[str] = [f"# {query}", ""]
    if not results:
        lines.append("No results.")
    for index, result in enumerate(results, start=1):
        lines.append(f"{index}. **{result.title}**")
        lines.append(f"   {result.url}")
        if result.snippet:
            lines.append(f"   {result.snippet}")
        lines.append("")
    if failed_providers:
        noun = "provider" if len(failed_providers) == 1 else "providers"
        lines.append(f"> Partial result: {len(failed_providers)} {noun} unavailable.")
    return "\n".join(lines).rstrip() + "\n"
