"""Agent 03 — the parser.

Turns HTML into the three fields the index stores: title, description and body
text. The body is **hard-capped at 1,000 characters**, cut at the nearest word
boundary and closed with an ellipsis, so a page can never push a row past the
storage budget and a card never shows half a word.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from app.engine.urls import safe_urljoin

from app.agents import load_agent

_brand = load_agent("10_brand_canonical_agent")
ACTION_TITLE_CHAR_CAP = _brand.ACTION_TITLE_CHAR_CAP
BrandCanonicalAgent = _brand.BrandCanonicalAgent
action_title_from = _brand.action_title_from
clean_url_for = _brand.clean_url_for

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

#: Hard ceiling on stored body text, ellipsis included.
BODY_CHAR_CAP = 1000

#: Ceilings for the card metadata fields. The brand sits on its own line now,
#: so the title carries the whole subject rather than four words of it.
BRAND_CHAR_CAP = 80
CLEAN_URL_CHAR_CAP = 160
TITLE_CHAR_CAP = 200
DESCRIPTION_CHAR_CAP = 400

#: A meta description shorter than this says nothing useful.
MIN_DESCRIPTION_CHARS = 20

#: How much body text a rebuilt description borrows.
DESCRIPTION_SNIPPET_CHARS = 180

#: Openings that mean the description is the site's boilerplate, repeated
#: verbatim on every subpage — so it identifies nothing about this page.
GENERIC_DESCRIPTION_PREFIXES: tuple[str, ...] = (
    "official portal",
    "official website",
    "official web site",
    "welcome to",
    "home page",
    "homepage",
    "this is the official",
    "government of india",
)

ELLIPSIS = "…"

#: How many outbound links one page may contribute to the frontier.
MAX_LINKS_PER_PAGE = 40

#: Hrefs that are never a page to crawl.
SKIP_HREF_PREFIXES: tuple[str, ...] = (
    "#",
    "mailto:",
    "tel:",
    "javascript:",
    "data:",
    "ftp:",
    "file:",
)

# Elements that are never prose.
STRIP_TAGS: tuple[str, ...] = (
    "script",
    "style",
    "noscript",
    "iframe",
    "svg",
    "canvas",
    "form",
    "nav",
    "footer",
    "header",
    "aside",
    "template",
)

WHITESPACE = re.compile(r"\s+")


#: Control characters, NUL included. SQLite keeps the bytes but truncates on
#: them: `length('before\x00after')` is 6, not 12, so everything after a NUL is
#: silently lost from the stored page. Tab, newline and carriage return are left
#: for WHITESPACE to collapse.
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def collapse(text: str) -> str:
    """Collapse every run of whitespace to one space and trim."""
    # Replaced with a space, not deleted: dropping the NUL runs "before" and
    # "after" together and breaks the word boundary for search.
    cleaned = CONTROL_CHARS.sub(" ", (text or "").replace("\u00a0", " "))
    return WHITESPACE.sub(" ", cleaned).strip()


def snippet_from(text: str, limit: int = DESCRIPTION_SNIPPET_CHARS) -> str:
    """The first `limit` characters of `text`, cut at a word boundary.

    The ellipsis is only appended when something was actually removed, so a
    short page does not advertise a truncation that did not happen.
    """
    cleaned = collapse(text)
    if len(cleaned) <= limit:
        return cleaned

    window = cleaned[:limit]
    boundary = window.rsplit(" ", 1)[0].rstrip(" ,;:-—")
    return (boundary or window) + ELLIPSIS


def is_generic_description(description: str, title: str) -> bool:
    """True when the meta description cannot distinguish this page."""
    cleaned = collapse(description)
    if len(cleaned) < MIN_DESCRIPTION_CHARS:
        return True

    lowered = cleaned.lower()
    if lowered.startswith(GENERIC_DESCRIPTION_PREFIXES):
        return True

    # A description that only repeats the title adds nothing to it.
    if title and lowered == collapse(title).lower():
        return True

    return False


def build_description(title: str, meta_description: str, body_text: str) -> str:
    """A description that is specific to this page.

    Portals often ship one boilerplate meta description across hundreds of
    subpages, which makes every result card read identically. When the meta
    description is missing, too short, or generic, the title is combined with
    the opening of the page's own body text instead.
    """
    meta = collapse(meta_description)
    if meta and not is_generic_description(meta, title):
        return meta[:DESCRIPTION_CHAR_CAP]

    snippet = snippet_from(body_text)
    if title and snippet:
        return f"{title} — {snippet}"[:DESCRIPTION_CHAR_CAP]
    return (snippet or meta or collapse(title))[:DESCRIPTION_CHAR_CAP]


def truncate_body(text: str, cap: int = BODY_CHAR_CAP) -> str:
    """Cut `text` to at most `cap` characters at a word boundary.

    The ellipsis counts against the cap, so the returned string is never
    longer than `cap`. A single unbroken token longer than the cap is cut at
    the cap rather than being returned whole.
    """
    cleaned = collapse(text)
    if len(cleaned) <= cap:
        return cleaned

    window = cleaned[: max(1, cap - len(ELLIPSIS))]
    boundary = window.rsplit(" ", 1)[0].rstrip(" ,;:-—")
    if not boundary:
        boundary = window
    return boundary + ELLIPSIS


@dataclass
class ParsedPage:
    """What the parser found, and whether it is usable."""

    url: str
    ok: bool
    title: str = ""
    description: str = ""
    body_text: str = ""
    reason: str = ""
    links: list[str] = field(default_factory=list)
    #: What the card shows instead of a raw <title> and a bare URL.
    brand_name: str = ""
    clean_url: str = ""
    action_title: str = ""
    #: Schema.org @type values from the page's JSON-LD, lower-cased.
    schema_types: list[str] = field(default_factory=list)
    #: Action links found on the page: [{"intent","label","url"}].
    sitelinks: list[dict[str, str]] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return len(self.body_text)


class ParserAgent:
    """HTML in, bounded fields out. Never raises on bad markup."""

    def __init__(self, body_char_cap: int = BODY_CHAR_CAP) -> None:
        self.body_char_cap = body_char_cap

    def parse(self, url: str, html: str) -> ParsedPage:
        if not html or not html.strip():
            return ParsedPage(url=url, ok=False, reason="empty_response")

        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as error:  # bs4 is tolerant, but never fatal here
            logger.debug("parse failed for %s: %s", url, error)
            return ParsedPage(url=url, ok=False, reason="unparseable")

        # Read the brand and the schema types first: JSON-LD lives in a
        # <script>, which the strip below removes before anything else sees it.
        brand_name = self._brand_name(soup, url)
        schema_types = self._schema_types(soup)
        sitelinks = self._sitelinks(soup, url)

        for tag in soup.find_all(STRIP_TAGS):
            tag.decompose()

        title = self._title(soup)
        # Read the body only: `soup.get_text()` also returns the contents of
        # <head>, so the keyword-stuffed <title> and every <meta> description
        # ended up at the front of body_text and showed up in snippets.
        body_source = soup.body or soup
        body_text = truncate_body(body_source.get_text(" "), self.body_char_cap)
        description = build_description(title, self._description(soup), body_text)
        links = self.extract_links(soup, url)
        action_title = action_title_from(
            title, self._h1(soup), brand_name, og_title=self._og_title(soup), url=url
        )
        clean_url = clean_url_for(url)

        if not title and not body_text:
            return ParsedPage(url=url, ok=False, reason="no_content")

        return ParsedPage(
            url=url,
            ok=True,
            title=title[:TITLE_CHAR_CAP],
            description=description[:DESCRIPTION_CHAR_CAP],
            body_text=body_text,
            links=links,
            schema_types=schema_types,
            sitelinks=sitelinks,
            brand_name=brand_name[:BRAND_CHAR_CAP],
            clean_url=clean_url[:CLEAN_URL_CHAR_CAP],
            action_title=(action_title or title)[:ACTION_TITLE_CHAR_CAP],
        )

    @staticmethod
    def extract_links(
        soup: BeautifulSoup,
        base_url: str,
        limit: int = MAX_LINKS_PER_PAGE,
    ) -> list[str]:
        """Absolute outbound URLs from `<a href>`, in document order.

        Relative hrefs are resolved against the page's own URL, so the
        frontier receives only absolute URLs and never has to know where a
        link came from. Fragments, `mailto:`, `javascript:` and the rest are
        dropped here; the frontier applies the crawl-scope rules.
        """
        links: list[str] = []
        seen: set[str] = set()

        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href") or "").strip()
            if not href or href.lower().startswith(SKIP_HREF_PREFIXES):
                continue

            # A href is page-supplied data: one dirty link costs that link.
            absolute = safe_urljoin(base_url, href)
            if absolute is None:
                continue

            if not absolute.lower().startswith(("http://", "https://")):
                continue
            # The fragment identifies a position in a page, not another page.
            absolute = absolute.split("#", 1)[0]
            if absolute in seen:
                continue
            seen.add(absolute)
            links.append(absolute)
            if len(links) >= limit:
                break

        return links

    @staticmethod
    def _title(soup: BeautifulSoup) -> str:
        if soup.title and soup.title.string:
            title = collapse(soup.title.string)
            if title:
                return title
        for attribute, key in (
            ("property", "og:title"),
            ("name", "twitter:title"),
        ):
            meta = soup.find("meta", attrs={attribute: key})
            if meta and meta.get("content"):
                title = collapse(meta["content"])
                if title:
                    return title
        heading = soup.find("h1")
        if heading:
            return collapse(heading.get_text(" "))
        return ""

    @staticmethod
    def _schema_types(soup: BeautifulSoup) -> list[str]:
        """Every Schema.org @type on the page, lower-cased and de-duplicated.

        The classifier treats these as the publisher stating what the page is,
        which is stronger evidence than anything the copy implies.
        """
        found: list[str] = []

        def collect(node):
            if isinstance(node, list):
                for item in node:
                    collect(item)
                return
            if not isinstance(node, dict):
                return
            value = node.get("@type")
            if isinstance(value, str):
                found.append(value.strip().lower())
            elif isinstance(value, list):
                found.extend(str(item).strip().lower() for item in value)
            graph = node.get("@graph")
            if isinstance(graph, list):
                collect(graph)

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                collect(json.loads(raw))
            except (ValueError, TypeError):
                continue

        return list(dict.fromkeys(found))

    @staticmethod
    def _sitelinks(soup: BeautifulSoup, url: str) -> list[dict[str, str]]:
        """The action links on the page — Apply Online, Contact Us, Login."""
        try:
            return [link.as_dict() for link in extract_sitelinks(soup, url)]
        except Exception:  # noqa: BLE001 - a sitelink is never worth a failed parse
            return []

    @staticmethod
    def _og_title(soup: BeautifulSoup) -> str:
        """The publisher's own title for the page, when it sets one."""
        for attribute, key in (("property", "og:title"), ("name", "twitter:title")):
            meta = soup.find("meta", attrs={attribute: key})
            if meta and meta.get("content"):
                text = collapse(meta["content"])
                if text:
                    return text
        return ""

    @staticmethod
    def _h1(soup: BeautifulSoup) -> str:
        """The page's own heading, when it has exactly one obvious one."""
        heading = soup.find("h1")
        if heading is None:
            return ""
        return collapse(heading.get_text(" "))

    @staticmethod
    def _brand_name(soup: BeautifulSoup, url: str) -> str:
        """Delegated to the brand agent, which owns this resolution order."""
        return BrandCanonicalAgent().brand(soup, url)


    @staticmethod
    def _description(soup: BeautifulSoup) -> str:
        for attribute, key in (
            ("name", "description"),
            ("property", "og:description"),
            ("name", "twitter:description"),
        ):
            meta = soup.find("meta", attrs={attribute: key})
            if meta and meta.get("content"):
                description = collapse(meta["content"])
                if description:
                    return description
        return ""


__all__ = [
    "BODY_CHAR_CAP",
    "DESCRIPTION_CHAR_CAP",
    "DESCRIPTION_SNIPPET_CHARS",
    "GENERIC_DESCRIPTION_PREFIXES",
    "MIN_DESCRIPTION_CHARS",
    "build_description",
    "is_generic_description",
    "snippet_from",
    "ELLIPSIS",
    "MAX_LINKS_PER_PAGE",
    "SKIP_HREF_PREFIXES",
    "TITLE_CHAR_CAP",
    "ParsedPage",
    "ParserAgent",
    "collapse",
    "truncate_body",
]


# ---------------------------------------------------------------------------
# Sitelinks
#
# The action links a page offers — Apply Online, Contact Us, Login, Pay — are
# read from the DOM, so the parser owns the rule that finds them. They are
# stored with the page because search no longer has the HTML.
# ---------------------------------------------------------------------------
import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

#: Action intents, in the order worth showing. Each entry is (intent, patterns).
INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apply", ("apply online", "apply now", "apply for", "registration form", "आवेदन", "ಅರ್ಜಿ")),
    ("track", ("track status", "check status", "track application", "status enquiry", "स्थिति", "ಸ್ಥಿತಿ")),
    ("download", ("download", "download form", "pdf", "डाउनलोड", "ಡೌನ್ಲೋಡ್")),
    ("contact", ("contact us", "contact", "helpline", "support", "संपर्क", "ಸಂಪರ್ಕ")),
    ("login", ("log in", "login", "sign in", "signin", "साइन इन", "ಲಾಗಿನ್")),
    ("register", ("sign up", "signup", "register", "create account", "नोंदणी", "ನೋಂದಣಿ")),
    ("pay", ("pay now", "make payment", "pay online", "भुगतान", "ಪಾವತಿ")),
)

#: An anchor longer than this is a sentence, not a link label.
MAX_LABEL_CHARS = 40
#: How many sitelinks to return.
MAX_SITELINKS = 5

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Sitelink:
    intent: str
    label: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {"intent": self.intent, "label": self.label, "url": self.url}


def classify_label(text: str) -> str:
    """The intent an anchor label expresses, or "" for none."""
    label = _WHITESPACE.sub(" ", (text or "").strip()).lower()
    if not label or len(label) > MAX_LABEL_CHARS:
        return ""
    for intent, patterns in INTENTS:
        if any(pattern in label for pattern in patterns):
            return intent
    return ""


def extract_sitelinks(soup, page_url: str, limit: int = MAX_SITELINKS) -> list[Sitelink]:
    """The highest-value actions on a page, one per intent, in intent order."""
    seen: dict[str, Sitelink] = {}

    for anchor in soup.find_all("a", href=True):
        label = _WHITESPACE.sub(" ", anchor.get_text(" ").strip())
        intent = classify_label(label)
        if not intent or intent in seen:
            continue
        href = urljoin(page_url, anchor["href"].strip())
        if not href.startswith(("http://", "https://")):
            continue
        seen[intent] = Sitelink(intent=intent, label=label[:MAX_LABEL_CHARS], url=href)

    order = [intent for intent, _ in INTENTS]
    return sorted(seen.values(), key=lambda link: order.index(link.intent))[:limit]
