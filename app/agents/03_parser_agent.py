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
from urllib.parse import unquote, urlsplit

from app.engine.urls import safe_urljoin

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

#: Hard ceiling on stored body text, ellipsis included.
BODY_CHAR_CAP = 1000

#: Ceilings for the two metadata fields.
BRAND_CHAR_CAP = 80
CLEAN_URL_CHAR_CAP = 160
ACTION_TITLE_CHAR_CAP = 120
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


# ---------------------------------------------------------------------------
# Card-oriented extraction: brand, breadcrumb, action title
# ---------------------------------------------------------------------------

#: Separators that sites stuff keywords into a <title> with. Each one splits
#: "Karnataka Health Department | Notifications | Apply Online | Official Site"
#: into parts, of which only the first is usually the page's actual subject.
TITLE_SEPARATORS = ("|", " - ", " – ", " — ", " :: ", " · ", " • ", " » ")

#: Words that say nothing about what the page does.
EMPTY_TITLE_WORDS = frozenset(
    {"home", "homepage", "welcome", "index", "untitled", "official site",
     "official website", "click here", "page", "website"}
)

#: How many words an action title may run to.
ACTION_TITLE_WORDS = 4

#: Path segments that carry no meaning for a reader.
NOISE_SEGMENTS = frozenset(
    {"index", "index.html", "index.php", "index.aspx", "default", "default.aspx",
     "home", "main", "en", "english", "www", "pages", "page"}
)


def domain_root(url: str) -> str:
    """The registrable name without `www.` or a public suffix tail."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and parts[-2] in {"gov", "nic", "ac", "co", "org", "net", "edu", "res"}:
        # karnataka.gov.in -> karnataka
        return parts[-3]
    return parts[0] if parts else ""


def brand_from_domain(url: str) -> str:
    """A readable fallback brand: 'thehindu.com' -> 'The Hindu' is not derivable,
    but 'karnataka.gov.in' -> 'Karnataka' is, and that is better than nothing."""
    root = domain_root(url)
    if not root:
        return ""
    label = root.replace("-", " ").replace("_", " ").strip()
    if not label:
        return ""
    if len(label) <= 4:
        return label.upper()
    return " ".join(word.capitalize() for word in label.split())


def clean_url_for(url: str, max_segments: int = 3) -> str:
    """A Google-style breadcrumb: 'karnataka.gov.in › health › notifications'."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""

    segments: list[str] = []
    for raw in parts.path.split("/"):
        if not raw:
            continue
        try:
            text = unquote(raw)
        except Exception:
            text = raw
        text = re.sub(r"\.(html?|php|aspx?|jsp|txt)$", "", text, flags=re.I)
        text = text.replace("-", " ").replace("_", " ").strip()
        text = collapse(text)
        if not text or text.lower() in NOISE_SEGMENTS:
            continue
        if len(text) > 40:
            text = text[:39].rstrip() + "…"
        segments.append(text)
        if len(segments) >= max_segments:
            break

    return " › ".join([host, *segments]) if segments else host


def _split_title(title: str) -> list[str]:
    """Split a stuffed title into its parts."""
    parts = [title]
    for separator in TITLE_SEPARATORS:
        parts = [piece for part in parts for piece in part.split(separator)]
    return [collapse(piece).strip(" .,:;·•»-") for piece in parts if collapse(piece)]


def action_title_from(title: str, h1: str = "", brand: str = "") -> str:
    """The 2-4 word statement of what the page is for.

    An H1 wins when there is one, because it is what the page itself calls
    itself. Otherwise the title is split on the usual stuffing separators and
    the first part that is not just the site's own name is used.
    """
    heading = collapse(h1 or "")
    if heading and 1 < len(heading.split()) <= 12 and heading.lower() not in EMPTY_TITLE_WORDS:
        candidate = heading
    else:
        candidate = ""
        parts = _split_title(collapse(title or ""))
        for part in parts:
            lowered = part.lower()
            if lowered in EMPTY_TITLE_WORDS:
                continue
            if brand and lowered == brand.lower():
                continue
            if len(part.split()) < 2:
                continue
            candidate = part
            break
        if not candidate and parts:
            candidate = parts[0]

    if not candidate:
        return ""

    words = candidate.split()
    if len(words) > ACTION_TITLE_WORDS:
        words = words[:ACTION_TITLE_WORDS]
    return collapse(" ".join(words)).strip(" .,:;-")


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

        # Read the brand first: JSON-LD lives in a <script>, which the strip
        # below removes before anything else can see it.
        brand_name = self._brand_name(soup, url)

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
        action_title = action_title_from(title, self._h1(soup), brand_name)
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
    def _h1(soup: BeautifulSoup) -> str:
        """The page's own heading, when it has exactly one obvious one."""
        heading = soup.find("h1")
        if heading is None:
            return ""
        return collapse(heading.get_text(" "))

    @staticmethod
    def _brand_name(soup: BeautifulSoup, url: str) -> str:
        """The site's own name: og:site_name, then JSON-LD, then the domain.

        og:site_name is what the publisher calls itself, so it beats anything
        derived from the host. JSON-LD carries the same idea for sites that
        never set the Open Graph tag.
        """
        meta = soup.find("meta", attrs={"property": "og:site_name"})
        if meta and meta.get("content"):
            name = collapse(meta["content"])
            if name:
                return name

        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            for node in data if isinstance(data, list) else [data]:
                if not isinstance(node, dict):
                    continue
                publisher = node.get("publisher") or node.get("sourceOrganization")
                if isinstance(publisher, dict) and publisher.get("name"):
                    name = collapse(str(publisher["name"]))
                    if name:
                        return name
                if node.get("@type") in ("Organization", "WebSite") and node.get("name"):
                    name = collapse(str(node["name"]))
                    if name:
                        return name

        return brand_from_domain(url)

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
