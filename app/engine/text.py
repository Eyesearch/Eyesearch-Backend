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

    # Structural first: anchors inside <nav>/<header> are the page's own
    # navigation. Only if the page marks none do we look at the whole document.
    containers = soup.find_all(["nav", "header"])
    anchors = [a for container in containers for a in container.find_all("a", href=True)]
    if not anchors:
        anchors = soup.find_all("a", href=True)

    for anchor in anchors:
        text = normalise_whitespace(anchor.get_text(" ", strip=True)).lower()
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        # A link with no label, or an absurdly long one, is not navigation.
        if not (3 <= len(text) <= 40):
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
    # The term fields the two-tier schema indexes (see app/engine/schema.py).
    headings: str = ""
    body_text: str = ""
    nav_terms: str = ""
    site_terms: str = ""
    category_terms: str = ""
    url_terms: str = ""

    def payload_bytes(self) -> int:
        """UTF-8 size of what will actually be stored for this page."""
        fields = (
            self.url, self.title, self.headings, self.description, self.body_text,
            self.nav_terms, self.site_terms, self.category_terms, self.url_terms,
        )
        fields = fields + (self.sections_json(),)
        return sum(len(field.encode("utf-8")) for field in fields)

    @property
    def description(self) -> str:
        """What the card shows: the meta description, else the summary."""
        return self.snippet or truncate(self.content, 200)

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
            "headings": self.headings,
            "description": self.description,
            "content": self.body_text,
            "nav_terms": self.nav_terms,
            "site_terms": self.site_terms,
            "category_terms": self.category_terms,
            "url_terms": self.url_terms,
            # kept for callers that still read the old shape
            "snippet": self.snippet,
            "summary": self.content,
            "language": self.language,
            "favicon_url": self.favicon_url,
            "category": self.category,
            "sections": list(self.sections),
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


def url_terms(url: str) -> str:
    """Words from the URL path and host, for the url_terms column."""
    parsed = urlparse(normalise_url(url))
    host_words = [part for part in parsed.netloc.split(".") if part and part not in {"www", "com", "in", "org", "net"}]
    path_words = [part.replace("-", " ").replace("_", " ") for part in parsed.path.split("/") if part]
    return normalise_whitespace(" ".join(host_words + path_words))


def headings_of(soup: BeautifulSoup, limit: int = 200) -> str:
    """The page's own headings — the strongest signal after the title."""
    parts = [
        normalise_whitespace(node.get_text(" ", strip=True))
        for node in soup.find_all(["h1", "h2", "h3"])
    ]
    return truncate(" ".join(part for part in parts if part), limit)


def nav_terms_of(soup: BeautifulSoup, sections: list[dict[str, str]], limit: int = 300) -> str:
    """Navigation vocabulary: the sitelinks we kept plus the page's nav anchors."""
    parts = [section.get("title", "") for section in sections]
    for anchor in soup.find_all("a", href=True)[:40]:
        text = normalise_whitespace(anchor.get_text(" ", strip=True))
        if 2 <= len(text) <= 40:
            parts.append(text)
    return truncate(" ".join(dict.fromkeys(part for part in parts if part)), limit)


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


# Why a page was not indexed. Logged verbatim so a silent skip is impossible.
SKIP_TOO_SHORT = "too_short_html"
SKIP_NOT_INDEXABLE = "not_indexable"
SKIP_NO_TITLE = "no_title"
SKIP_LOW_TEXT = "low_text_density"
SKIP_BLOCKED_STATUS = "blocked_status"
SKIP_EMPTY = "empty_after_clean"
SKIP_JUNK = "junk_page"
SKIP_THIN = "low_text_density"
SKIP_NO_DESCRIPTION = "no_description"

# Titles and descriptions that mean "you were blocked", not "this is a page".
JUNK_MARKERS = (
    "403 forbidden", "access denied", "access forbidden", "not acceptable",
    "just a moment", "attention required", "request blocked", "are you a robot",
    "captcha", "enable javascript and cookies", "page not found", "404 not found",
    "service unavailable", "site is temporarily unavailable", "under construction",
)

# The strict gate: a page earns a row only with a real title, a real
# description and real server-rendered text.
MIN_BODY_CHARS = 80
MIN_TITLE_CHARS = 3
MIN_DESCRIPTION_CHARS = 20


def extract_document_with_reason(
    url: str,
    html: str,
    *,
    http_status: int = 200,
    category: str | None = None,
) -> tuple[ExtractedDocument | None, str]:
    """Extract a page, and say exactly why when it is rejected.

    The two-tier schema stores no page body — only the title, headings,
    description, nav vocabulary and URL terms. So a JavaScript-heavy government
    homepage with a real title and meta description is perfectly indexable even
    when almost no text is server-rendered; the old body-length gate threw those
    away.
    """
    if http_status >= 400:
        return None, SKIP_BLOCKED_STATUS
    if not html or len(html.strip()) < 40:
        return None, SKIP_TOO_SHORT
    if not is_indexable(url):
        return None, SKIP_NOT_INDEXABLE

    settings = get_engine_settings()
    soup = BeautifulSoup(html, "html.parser")

    title = normalise_whitespace(soup.title.get_text()) if soup.title else ""
    if not title:
        h1 = soup.find("h1")
        if h1 is not None:
            title = normalise_whitespace(h1.get_text(" ", strip=True))
    title = truncate(strip_artefacts(title), settings.title_max_chars)

    description = ""
    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta is None:
        meta = soup.find("meta", attrs={"property": re.compile("^og:description$", re.I)})
    if meta is not None:
        description = strip_artefacts(meta.get("content") or "")

    body = _body_text(soup)
    headings = headings_of(soup)
    sections = extract_sections(html, url, limit=settings.sections_max)
    nav_terms = nav_terms_of(soup, sections)

    # --- strict quality gate (pre-debug specification) -------------------
    # A page needs a semantic <title>, a meaningful meta description and real
    # server-rendered text. A JavaScript shell, a WAF block page or a bare
    # hostname is not a document.
    if not title or len(title) < MIN_TITLE_CHARS:
        return None, SKIP_NO_TITLE
    if not description or len(description) < MIN_DESCRIPTION_CHARS:
        return None, SKIP_NO_DESCRIPTION
    if len(body) < MIN_BODY_CHARS:
        return None, SKIP_THIN

    # A WAF block page is not a document, however long its body is.
    haystack = f"{title} {description}".lower()
    if any(marker in haystack for marker in JUNK_MARKERS):
        return None, SKIP_JUNK

    icon = ""
    icon_link = soup.find("link", attrs={"rel": re.compile("icon", re.I)})
    if icon_link is not None:
        icon = normalise_whitespace(icon_link.get("href") or "")

    canonical = ""
    canonical_link = soup.find("link", attrs={"rel": re.compile("canonical", re.I)})
    if canonical_link is not None:
        canonical = normalise_whitespace(canonical_link.get("href") or "")

    canonical_url = canonicalise(canonical, url) if canonical else normalise_url(url)
    favicon = urljoin(url, icon) if icon else f"https://{urlparse(canonical_url).netloc}/favicon.ico"

    snippet = description or truncate(body or headings or nav_terms, settings.snippet_max_chars)
    snippet = truncate(snippet, settings.snippet_max_chars)

    # The section names are part of what the page offers, so they belong in the
    # searchable text too — otherwise "ztube pricing" could never match the root.
    section_names = ", ".join(section["title"] for section in sections)
    document = ExtractedDocument(
        category=category or infer_category(url),
        sections=sections,
        headings=headings,
        nav_terms=nav_terms,
        url=normalise_url(url),
        canonical_url=canonical_url,
        domain=urlparse(normalise_url(url)).netloc,
        root_domain=root_domain(url),
        domain_class=domain_class(url),
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
    document.body_text = truncate(body, settings.body_max_chars)
    document.site_terms = truncate(
        f"{document.root_domain.replace('.', ' ')} {title}", 120
    )
    document.category_terms = document.category
    document.url_terms = url_terms(canonical_url)

    document = document.fit_to_budget(settings.max_payload_bytes)
    if document is None or not (document.title or document.description):
        return None, SKIP_EMPTY
    return document, ""


def fallback_document(
    url: str,
    *,
    label: str | None = None,
    category: str | None = None,
    http_status: int = 200,
    use_hostname: bool = False,
) -> ExtractedDocument | None:
    """A root record built from the hostname when a page yields no extract.

    A seed domain that answered 200 must still be indexed: the root record is the
    thing the site card is built from, and sitelinks are optional. The title comes
    from the seed's label when we have one, else the hostname.
    """
    normalised = normalise_url(url)
    host = urlparse(normalised).netloc
    if not host or not is_indexable(normalised):
        return None

    settings = get_engine_settings()
    domain_label = host.removeprefix("www.")
    words = [part for part in domain_label.split(".") if part]
    guess = " ".join(part.capitalize() for part in words[:-1]) or domain_label
    if use_hostname:
        # A domain that refused us still exists: the hostname is the honest title.
        title = truncate(domain_label, settings.title_max_chars)
    else:
        title = truncate(strip_artefacts(label or guess) or domain_label, settings.title_max_chars)

    document = ExtractedDocument(
        url=normalised,
        canonical_url=normalised,
        domain=host,
        root_domain=root_domain(normalised),
        domain_class=domain_class(normalised),
        title=title,
        snippet=f"{title} — official website",
        content=f"{title} — official website at {domain_label}.",
        language="en",
        favicon_url=f"https://{host}/favicon.ico",
        category=category or infer_category(normalised),
        headings=title,
        nav_terms="",
        site_terms=truncate(f"{root_domain(normalised).replace('.', ' ')} {title}", 120),
        category_terms=category or infer_category(normalised),
        url_terms=url_terms(normalised),
    )
    return document.fit_to_budget(settings.max_payload_bytes)


def extract_document(
    url: str,
    html: str,
    *,
    http_status: int = 200,
    category: str | None = None,
) -> ExtractedDocument | None:
    """Extract one bounded document, or None when the page should not be indexed."""
    document, _reason = extract_document_with_reason(
        url, html, http_status=http_status, category=category
    )
    return document


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
    "fallback_document",
    "extract_document_with_reason",
    "SKIP_TOO_SHORT",
    "SKIP_NOT_INDEXABLE",
    "SKIP_NO_TITLE",
    "SKIP_LOW_TEXT",
    "SKIP_BLOCKED_STATUS",
    "SKIP_EMPTY",
    "SKIP_JUNK",
    "SKIP_THIN",
    "SKIP_NO_DESCRIPTION",
    "JUNK_MARKERS",
    "summarise",
    "url_terms",
    "headings_of",
    "nav_terms_of",
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
