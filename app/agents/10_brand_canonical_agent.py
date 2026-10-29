"""Resolve a page's brand and its clean action title.

The card prints the brand on its own line and the title beneath it, so the two
must not repeat each other. Everything here is derived from the page and its
URL — there is no dictionary of site names, because such a dictionary classifies
the sites someone thought of and goes stale for the rest.

Brand resolution, in order of how much the source means:

1. `og:site_name` — the publisher's own name for the site.
2. The JSON-LD `publisher` or `Organization` name — the same idea for sites that
   never set the Open Graph tag.
3. The registrable domain root, humanised. Weak, but never empty.

Title resolution prefers `og:title`, then the H1, then the `<title>`, and strips
the parts that are only the brand or the site root. What is left is the subject.
"""

from __future__ import annotations

import json
import re
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

__all__ = [
    "ACTION_TITLE_CHAR_CAP",
    "ACTION_TITLE_WORDS",
    "BrandCanonicalAgent",
    "action_title_from",
    "brand_from_domain",
    "clean_url_for",
    "domain_root",
    "slug_title",
]

#: A title is a label on a phone screen: three to five words, and never more
#: than this many characters however many words fit.
ACTION_TITLE_WORDS = 5
ACTION_TITLE_CHAR_CAP = 60

#: Separators that sites stuff keywords into a <title> with. Each one splits
#: "Karnataka Health Department | Notifications | Apply Online | Official Site"
#: into parts, of which only the first is usually the page's actual subject.
TITLE_SEPARATORS = ("|", " - ", " – ", " — ", " :: ", " · ", " • ", " » ")

#: Words that say nothing about what the page does.
EMPTY_TITLE_WORDS = frozenset(
    {"home", "homepage", "welcome", "index", "untitled", "official site",
     "official website", "click here", "page", "website"}
)

#: Path segments that carry no meaning for a reader.
NOISE_SEGMENTS = frozenset(
    {"index", "index.html", "index.php", "index.aspx", "default", "default.aspx",
     "home", "main", "en", "english", "www", "pages", "page"}
)

#: Second-level labels that are part of a suffix, not a name.
_SUFFIX_LABELS = {"gov", "nic", "ac", "co", "org", "net", "edu", "res", "mil", "com"}


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def domain_root(url: str) -> str:
    """The registrable name without `www.` or a public suffix tail."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    parts = [part for part in host.split(".") if part]
    if len(parts) >= 3 and parts[-2] in _SUFFIX_LABELS:
        # karnataka.gov.in -> karnataka
        return parts[-3]
    return parts[0] if parts else ""


def brand_from_domain(url: str) -> str:
    """A readable fallback brand: 'karnataka.gov.in' -> 'Karnataka'."""
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
        text = _collapse(text.replace("-", " ").replace("_", " "))
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
    return [piece.strip(" .,:;·•»-") for piece in (_collapse(p) for p in parts) if piece]


def _title_key(value: str) -> str:
    """A comparable form of a name: lower case, letters and digits only.

    "Amazon.in", "Amazon in" and "amazon.in" all collapse to "amazonin", so a
    brand is recognised however the page punctuates it.
    """
    return "".join(ch for ch in (value or "").lower() if ch.isalnum())


def _is_brand_part(part: str, brand: str, root: str) -> bool:
    key = _title_key(part)
    if not key:
        return True
    for name in (brand, root):
        other = _title_key(name)
        if other and key == other:
            return True
    return False


def slug_title(url: str) -> str:
    """A readable title from the last meaningful path segment."""
    try:
        path = urlsplit(url).path
    except ValueError:
        return ""
    for raw in reversed([segment for segment in path.split("/") if segment]):
        text = unquote(raw)
        text = re.sub(r"\.(html?|php|aspx?|jsp|txt)$", "", text, flags=re.I)
        text = _collapse(text.replace("-", " ").replace("_", " "))
        text = text.strip(" .,:;-")
        if text and text.lower() not in NOISE_SEGMENTS:
            return _shorten(text)
    return ""


def _shorten(text: str) -> str:
    """Trim to the word and character caps, without cutting a word in half."""
    words = text.split()
    if len(words) > ACTION_TITLE_WORDS:
        words = words[:ACTION_TITLE_WORDS]
    joined = _collapse(" ".join(words))
    if len(joined) > ACTION_TITLE_CHAR_CAP:
        cut = joined[:ACTION_TITLE_CHAR_CAP]
        # Drop the partial trailing word rather than showing "Notificatio".
        joined = cut.rsplit(" ", 1)[0] if " " in cut else cut
    return joined.strip(" .,:;-")


def action_title_from(
    title: str,
    h1: str = "",
    brand: str = "",
    og_title: str = "",
    url: str = "",
) -> str:
    """The clean subject of the page, with the brand stripped out.

    Candidates are tried in order — og:title, the H1 when it is longer than five
    characters, then the <title> — and each is split on the separators sites
    stuff keywords with. Parts that are just the site's name are dropped, and
    what remains is the subject. A page with nothing usable falls back to its
    URL slug.
    """
    root = domain_root(url) if url else ""
    heading = _collapse(h1 or "")
    candidates = [
        _collapse(og_title or ""),
        heading if len(heading) > 5 else "",
        _collapse(title or ""),
    ]

    for candidate in candidates:
        if not candidate or candidate.lower() in EMPTY_TITLE_WORDS:
            continue
        parts = _split_title(candidate)
        kept = [part for part in parts if not _is_brand_part(part, brand, root)]
        if not kept:
            continue
        chosen = kept[0].strip(" .,:;-")
        if len(chosen) > 1:
            return _shorten(chosen)

    return slug_title(url)


class BrandCanonicalAgent:
    """Brand and title resolution for one page."""

    def brand(self, soup: BeautifulSoup, url: str) -> str:
        """The site's own name: og:site_name, then JSON-LD, then the domain."""
        meta = soup.find("meta", attrs={"property": "og:site_name"})
        if meta and meta.get("content"):
            name = _collapse(meta["content"])
            if name:
                return name

        for node in self._json_ld(soup):
            publisher = node.get("publisher") or node.get("sourceOrganization")
            if isinstance(publisher, dict) and publisher.get("name"):
                name = _collapse(str(publisher["name"]))
                if name:
                    return name
            if node.get("@type") in ("Organization", "WebSite") and node.get("name"):
                name = _collapse(str(node["name"]))
                if name:
                    return name

        return brand_from_domain(url)

    def clean_title(self, title: str, brand: str = "", url: str = "") -> str:
        """The action title for a raw <title>."""
        return action_title_from(title=title, brand=brand, url=url)

    @staticmethod
    def _json_ld(soup: BeautifulSoup) -> list[dict]:
        nodes: list[dict] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text() or ""
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except (ValueError, TypeError):
                continue
            for node in data if isinstance(data, list) else [data]:
                if isinstance(node, dict):
                    nodes.append(node)
        return nodes
