"""HTML → a strictly bounded indexable document.

The index stores *answers*, not copies of pages. A row is capped at
`INDEX_MAX_PAYLOAD_BYTES` (2 KB by default) and holds only:

    title        ≤ 100 characters
    canonical_url
    snippet      meta description, or the first ~200 clean characters
    favicon
    language     detected from the script
    content      a short lead, capped, only so FTS5 has something to match

Everything else — markup, scripts, stylesheets, inline images, base64 data,
tracking pixels and paragraph dumps — is stripped before it reaches storage.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

import json

from app.engine.config import get_engine_settings
from app.engine.urls import domain_class, infer_category, is_indexable, normalise_url, root_domain

_WS = re.compile(r"\s+")
_KANNADA = re.compile(r"[\u0C80-\u0CFF]")
_DEVANAGARI = re.compile(r"[\u0900-\u097F]")
_DATA_URI = re.compile(r"data:[^\s\"')]+", re.I)
_BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")
_BOILERPLATE_TAGS = ("script", "style", "noscript", "nav", "footer", "header", "form", "aside", "svg", "iframe", "img", "picture", "video", "audio", "canvas")

# Elements that usually carry the article body, in preference order.
_CONTENT_SELECTORS = ("article", "main", '[role="main"]', "#content", ".content", "#main", ".post")


def extract_sections(
    html: str,
    base_url: str,
    *,
    limit: int = 5,
) -> list[dict[str, str]]:
    """Pull the high-value navigation targets out of a page.

    Returns up to `limit` entries of `{title, url, snippet}` — the sections the
    frontend renders as sitelinks under the root result. Matching is by anchor
    text or href path against generic patterns (sign in, docs, pricing, …), so a
    site's own wording is what decides.
    """
    if not html:
        return []

    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(normalise_url(base_url)).netloc
    found: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        text = normalise_whitespace(anchor.get_text(" ", strip=True)).lower()
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        target = normalise_url(urljoin(base_url, href))
        parsed = urlparse(target)
        if parsed.netloc != host:
            continue
        depth = len([part for part in parsed.path.split("/") if part])
        if depth > 2:
            continue

        haystack = f"{text} {parsed.path.lower()}"
        if target in seen_urls:
            continue
        for label, hint, keywords in SECTION_PATTERNS:
            if label in seen_titles:
                continue
            if not any(keyword in haystack for keyword in keywords):
                continue
            seen_urls.add(target)
            seen_titles.add(label)
            found.append({"title": label, "url": target, "snippet": hint})
            break

        if len(found) >= limit:
            break

    return found


@dataclass(slots=True)
class ExtractedDocument:
    url: str
    canonical_url: str
    domain: str
    root_domain: str
    domain_class: str
    title: str
    snippet: str
    content: str
    language: str
    favicon_url: str | None
    category: str = "web"
    sections: list[dict[str, str]] = field(default_factory=list)

    def payload_bytes(self) -> int:
        """UTF-8 size of what will actually be stored for this page."""
        fields = (
            self.url, self.canonical_url, self.domain, self.root_domain,
            self.domain_class, self.category, self.title, self.snippet, self.content,
            self.language, self.favicon_url or "",
        )
        fields = fields + (self.sections_json(),)
        return sum(len(field.encode("utf-8")) for field in fields)

    def sections_json(self) -> str:
        """The compact JSON array stored alongside the document."""
        return json.dumps(self.sections, ensure_ascii=False, separators=(",", ":")) if self.sections else ""

    def fit_to_budget(self, max_bytes: int | None = None) -> "ExtractedDocument":
        """Trim content, then snippet, then title until the row fits the budget."""
        budget = max_bytes or get_engine_settings().max_payload_bytes

        while self.payload_bytes() > budget and self.content:
            overflow = self.payload_bytes() - budget
            keep = max(0, len(self.content.encode("utf-8")) - max(overflow + 64, 128))
            self.content = self.content.encode("utf-8")[:keep].decode("utf-8", "ignore").rstrip()

        while self.payload_bytes() > budget and self.snippet:
            self.snippet = self.snippet[:-32].rstrip()

        while self.payload_bytes() > budget and self.title:
            self.title = self.title[:-8].rstrip()

        return self

    def as_row(self) -> dict[str, object]:
        return {
            "url": self.url,
            "canonical_url": self.canonical_url,
            "domain": self.domain,
            "root_domain": self.root_domain,
            "domain_class": self.domain_class,
            "title": self.title,
            "snippet": self.snippet,
            "content": self.content,
            "language": self.language,
            "favicon_url": self.favicon_url,
            "category": self.category,
            "sections_json": self.sections_json(),
            "payload_bytes": self.payload_bytes(),
        }


def normalise_whitespace(text: str) -> str:
    return _WS.sub(" ", (text or "")).strip()


def strip_artefacts(text: str) -> str:
    """Remove data URIs, base64 blobs and tracking-pixel leftovers from text."""
    cleaned = _DATA_URI.sub(" ", text or "")
    cleaned = _BASE64_BLOB.sub(" ", cleaned)
    return normalise_whitespace(cleaned)


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


_SENTENCE_END = re.compile(r"(?<=[.!?।])\s+")

# High-value navigation targets worth keeping as sitelinks, matched by anchor
# text or href path. Generic patterns only — nothing here is domain-specific.
SECTION_PATTERNS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Sign in", "Access your account", ("sign in", "signin", "log in", "login", "sign-in")),
    ("Sign up", "Create an account", ("sign up", "signup", "register", "get started", "create account")),
    ("Documentation", "Official developer guides", ("docs", "documentation", "developers", "api reference", "guides")),
    ("Pricing", "Plans and pricing", ("pricing", "plans", "price")),
    ("Dashboard", "Your workspace", ("dashboard", "console", "workspace", "my account")),
    ("Download", "Download and install", ("download", "install")),
    ("Support", "Help and contact", ("support", "help", "contact")),
)


def summarise(text: str, *, max_sentences: int = 4, max_chars: int = 1500) -> str:
    """The 3–4 sentence factual summary we store instead of the page body.

    Sentences are taken in order until the sentence budget or the character cap
    is reached, so the summary always ends on a sentence boundary rather than
    mid-clause. A page with no punctuation still yields a bounded string.
    """
    cleaned = strip_artefacts(text)
    if not cleaned:
        return ""

    sentences = [part.strip() for part in _SENTENCE_END.split(cleaned) if part.strip()]
    if not sentences:
        return truncate(cleaned, max_chars)

    kept: list[str] = []
    total = 0
    for sentence in sentences[: max(1, max_sentences)]:
        if total + len(sentence) + 1 > max_chars and kept:
            break
        kept.append(sentence)
        total += len(sentence) + 1

    summary = " ".join(kept)
    if len(summary) > max_chars:
        summary = truncate(summary, max_chars)
    return summary


def truncate(text: str, limit: int) -> str:
    """Cut at a word boundary, never mid-word, and never over `limit` characters."""
    value = normalise_whitespace(text)
    if len(value) <= limit:
        return value
    window = value[:limit]
    cut = window.rfind(" ")
    if cut >= limit // 2:
        window = window[:cut]
    return window.rstrip(" ,;:-–—") + "…"


def canonicalise(url: str, base: str | None = None) -> str:
    """Resolve relative links, then apply the strict normalisation rules."""
    absolute = urljoin(base, url) if base else url
    return normalise_url(absolute)


def _body_text(soup: BeautifulSoup) -> str:
    for tag in soup(_BOILERPLATE_TAGS):
        tag.decompose()

    best = ""
    for selector in _CONTENT_SELECTORS:
        node = soup.select_one(selector)
        if node is not None:
            candidate = strip_artefacts(node.get_text(" ", strip=True))
            if len(candidate) > len(best):
                best = candidate
    if len(best) < 120:
        body = soup.body or soup
        best = strip_artefacts(body.get_text(" ", strip=True))
    return best


def extract_document(
    url: str,
    html: str,
    *,
    http_status: int = 200,
    category: str | None = None,
) -> ExtractedDocument | None:
    """Extract one bounded document, or None when the page should not be indexed."""
    if not html or len(html.strip()) < 80:
        return None
    if not is_indexable(url):
        return None

    settings = get_engine_settings()
    soup = BeautifulSoup(html, "html.parser")

    title = normalise_whitespace(soup.title.get_text()) if soup.title else ""
    if not title:
        h1 = soup.find("h1")
        if h1 is not None:
            title = normalise_whitespace(h1.get_text(" ", strip=True))
    title = truncate(strip_artefacts(title), settings.title_max_chars)
    if not title:
        return None

    description = ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta is None:
        meta = soup.find("meta", attrs={"property": re.compile("^og:description$", re.I)})
    if meta is not None:
        description = strip_artefacts(meta.get("content") or "")

    icon = ""
    icon_link = soup.find("link", attrs={"rel": re.compile("icon", re.I)})
    if icon_link is not None:
        icon = normalise_whitespace(icon_link.get("href") or "")

    canonical = ""
    canonical_link = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    if canonical_link is not None:
        canonical = normalise_whitespace(canonical_link.get("href") or "")

    body = _body_text(soup)
    if len(body) < 80 and not description:
        return None

    canonical_url = canonicalise(canonical, url) if canonical else normalise_url(url)
    favicon = urljoin(url, icon) if icon else f"https://{urlparse(canonical_url).netloc}/favicon.ico"

    snippet = description or truncate(body, settings.snippet_max_chars)
    snippet = truncate(snippet, settings.snippet_max_chars)

    sections = extract_sections(html, url, limit=settings.sections_max)
    # The section names are part of what the page offers, so they belong in the
    # searchable text too — otherwise "ztube pricing" could never match the root.
    section_names = ", ".join(section["title"] for section in sections)
    document = ExtractedDocument(
        category=category or infer_category(url),
        sections=sections,
        url=normalise_url(url),
        canonical_url=canonical_url,
        domain=urlparse(canonical_url).netloc,
        root_domain=root_domain(canonical_url),
        domain_class=domain_class(canonical_url),
        title=title,
        snippet=snippet,
        content=truncate(
            summarise(
                body,
                max_sentences=settings.summary_max_sentences,
                max_chars=settings.content_max_chars,
            )
            + (f" Sections: {section_names}." if section_names else ""),
            settings.content_max_chars,
        ),
        language=detect_language(f"{title} {body}"),
        favicon_url=favicon,
    )
    return document.fit_to_budget(settings.max_payload_bytes)


def extract_links(html: str, base_url: str) -> Iterable[str]:
    """Same-host, indexable links only — the crawl stays on entry portals."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlparse(normalise_url(base_url)).netloc
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = canonicalise(href, base_url)
        if not absolute.startswith("https://"):
            continue
        if urlparse(absolute).netloc != host:
            continue
        if absolute in seen or not is_indexable(absolute):
            continue
        seen.add(absolute)
        yield absolute


__all__ = [
    "summarise",
    "extract_sections",
    "ExtractedDocument",
    "extract_document",
    "extract_links",
    "canonicalise",
    "detect_language",
    "normalise_whitespace",
    "strip_artefacts",
    "truncate",
]
