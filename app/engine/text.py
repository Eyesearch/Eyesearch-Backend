"""HTML → indexable document.

Everything here is defensive: a page that is not HTML, has no title, or is
nothing but navigation must degrade to a skipped document rather than an
exception. The extractor is deliberately dependency-light — BeautifulSoup is
already a gateway dependency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.engine.config import get_engine_settings

_WS = re.compile(r"\s+")
_KANNADA = re.compile(r"[\u0C80-\u0CFF]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_BOILERPLATE_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "form", "aside", "svg")

# Elements that usually carry the article body, in preference order.
_CONTENT_SELECTORS = ("article", "main", '[role="main"]', "#content", ".content", "#main", ".post")


@dataclass(slots=True)
class ExtractedDocument:
    url: str
    canonical_url: str
    domain: str
    title: str
    snippet: str
    content: str
    language: str
    favicon_url: str | None

    def as_row(self) -> dict[str, object]:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "domain": self.domain,
            "title": self.title,
            "snippet": self.snippet,
            "content": self.content,
            "language": self.language,
            "favicon_url": self.favicon_url,
        }


def normalise_whitespace(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()


def detect_language(text: str) -> str:
    """Coarse script detection: Kannada, Devanagari, or English."""
    sample = text[:4000]
    kannada = len(_KANNADA.findall(sample))
    devanagari = len(_DEVANAGARI.findall(sample))
    if kannada > max(20, devanagari):
        return "kn"
    if devanagari > 20:
        return "hi"
    return "en"


def canonicalise(url: str, base: str | None = None) -> str:
    """Resolve relative links and strip fragments/tracking parameters."""
    absolute = urljoin(base, url) if base else url
    parsed = urlparse(absolute)
    host = parsed.netloc.lower().removeprefix("www.")
    query_parts = [
        part
        for part in parsed.query.split("&")
        if part and not part.lower().startswith(("utm_", "fbclid", "gclid", "ref="))
    ]
    path = parsed.path.rstrip("/") or "/"
    query = f"?{'&'.join(query_parts)}" if query_parts else ""
    return f"{parsed.scheme or 'https'}://{host}{path}{query}"


def _body_text(soup: BeautifulSoup) -> str:
    for tag in soup(_BOILERPLATE_TAGS):
        tag.decompose()

    best: str = ""
    for selector in _CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            candidate = normalise_whitespace(node.get_text(" ", strip=True))
            if len(candidate) > len(best):
                best = candidate
    if len(best) < 200:
        # Fall back to the whole document when the page has no semantic wrapper.
        body = soup.body or soup
        best = normalise_whitespace(body.get_text(" ", strip=True))
    return best


def extract_document(url: str, html: str, *, http_status: int = 200) -> ExtractedDocument | None:
    """Extract one document, or return None when the page is not worth indexing."""
    if not html or len(html.strip()) < 80:
        return None

    settings = get_engine_settings()
    soup = BeautifulSoup(html, "html.parser")

    title = normalise_whitespace(soup.title.get_text()) if soup.title else ""
    h1 = soup.find("h1")
    if not title and h1 is not None:
        title = normalise_whitespace(h1.get_text(" ", strip=True))
    if not title:
        return None

    description = ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta is None:
        meta = soup.find("meta", attrs={"property": re.compile("^og:description$", re.I)})
    if meta is not None:
        description = normalise_whitespace(meta.get("content") or "")

    canonical = ""
    link = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    if link is not None:
        canonical = normalise_whitespace(link.get("href") or "")

    icon = ""
    icon_link = soup.find("link", attrs={"rel": re.compile("icon", re.I)})
    if icon_link is not None:
        icon = normalise_whitespace(icon_link.get("href") or "")

    body = _body_text(soup)
    if len(body) < 120:
        return None

    canonical_url = canonicalise(canonical, url) if canonical else canonicalise(url)
    favicon = urljoin(url, icon) if icon else f"https://{urlparse(canonical_url).netloc}/favicon.ico"

    snippet = description or body[: settings.max_snippet_chars]
    if len(snippet) > settings.max_snippet_chars:
        snippet = snippet[: settings.max_snippet_chars].rsplit(" ", 1)[0].rstrip(",;:") + "…"

    return ExtractedDocument(
        url=canonicalise(url),
        canonical_url=canonical_url,
        domain=urlparse(canonical_url).netloc,
        title=title[:300],
        snippet=snippet,
        content=body[: settings.max_content_chars],
        language=detect_language(f"{title} {body}"),
        favicon_url=favicon,
    )


def extract_links(html: str, base_url: str) -> Iterable[str]:
    """Same-host http(s) links, canonicalised and de-duplicated, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(base_url).netloc.removeprefix("www.")
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = canonicalise(urljoin(base_url, href))
        if not absolute.startswith(("http://", "https://")):
            continue
        if urlparse(absolute).netloc.removeprefix("www.") != host:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        yield absolute


__all__ = [
    "ExtractedDocument",
    "extract_document",
    "extract_links",
    "canonicalise",
    "detect_language",
    "normalise_whitespace",
]
