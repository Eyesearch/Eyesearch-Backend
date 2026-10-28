"""Agent 03 — the parser.

Turns HTML into the three fields the index stores: title, description and body
text. The body is **hard-capped at 1,000 characters**, cut at the nearest word
boundary and closed with an ellipsis, so a page can never push a row past the
storage budget and a card never shows half a word.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

#: Hard ceiling on stored body text, ellipsis included.
BODY_CHAR_CAP = 1000

#: Ceilings for the two metadata fields.
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

        for tag in soup.find_all(STRIP_TAGS):
            tag.decompose()

        title = self._title(soup)
        body_text = truncate_body(soup.get_text(" "), self.body_char_cap)
        description = build_description(title, self._description(soup), body_text)
        links = self.extract_links(soup, url)

        if not title and not body_text:
            return ParsedPage(url=url, ok=False, reason="no_content")

        return ParsedPage(
            url=url,
            ok=True,
            title=title[:TITLE_CHAR_CAP],
            description=description[:DESCRIPTION_CHAR_CAP],
            body_text=body_text,
            links=links,
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

            # urljoin raises ValueError on an unclosed IPv6 literal such as
            # 'http://[::1/x'. A href is page-supplied data, so one dirty link
            # must cost that link and nothing else.
            try:
                absolute = urljoin(base_url, href)
            except ValueError:
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
