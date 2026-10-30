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
ACTION_TITLE_WORDS = 4
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


def _host_label(url: str) -> str:
    """The bare host, for when even the domain root yields nothing."""
    try:
        return (urlsplit(url).hostname or "").lstrip("www.") or ""
    except ValueError:
        return ""


def _humanise_label(value: str) -> str:
    """A run-together handle into words: "indiatoday" -> "Indiatoday".

    Splitting a run-together handle into words needs a dictionary, which goes
    stale. Capitalising it at least stops the card showing a lower-case slug.
    """
    cleaned = value.replace("-", " ").replace("_", " ").strip()
    if not cleaned:
        return ""
    if " " in cleaned:
        return " ".join(word.capitalize() for word in cleaned.split())
    return cleaned[:1].upper() + cleaned[1:]


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


# ---------------------------------------------------------------------------
# Curated brands, and the length limit
#
# Same trade as the classifier: a short list of the portals people actually
# search for, checked first. It is narrow and it is checked before any parsing,
# so a page on karnataka.gov.in is called "Government of Karnataka" rather than
# "Karnataka", and a stale entry degrades to the parsing below rather than to a
# wrong name.
# ---------------------------------------------------------------------------

#: A card has room for a label, not a sentence.
MAX_BRAND_LENGTH = 35

CURATED_BRANDS: dict[str, str] = {
    "sbi.co.in": "State Bank of India",
    "onlinesbi.sbi": "State Bank of India",
    "rbi.org.in": "Reserve Bank of India",
    "hdfcbank.com": "HDFC Bank",
    "icicibank.com": "ICICI Bank",
    "paytm.com": "Paytm Payments",
    "phonepe.com": "PhonePe",
    "zerodha.com": "Zerodha",
    "karnataka.gov.in": "Government of Karnataka",
    "bangaloreone.gov.in": "Bangalore One",
    "ksp.karnataka.gov.in": "Karnataka State Police",
    "sevasindhu.karnataka.gov.in": "Seva Sindhu",
    "kpsc.kar.nic.in": "KPSC Karnataka",
    "india.gov.in": "National Portal of India",
    "uidai.gov.in": "UIDAI Aadhaar",
    "eci.gov.in": "Election Commission",
    "sci.gov.in": "Supreme Court of India",
    "karnatakahi.gov.in": "Karnataka High Court",
    "ecourts.gov.in": "eCourts Services India",
    "indiankanoon.org": "Indian Kanoon",
    "prajavani.net": "Prajavani",
    "deccanherald.com": "Deccan Herald",
    "kannadaprabha.com": "Kannada Prabha",
    "thehindu.com": "The Hindu",
    "timesofindia.indiatimes.com": "The Times of India",
    "google.com": "Google",
    "github.com": "GitHub",
    "huggingface.co": "Hugging Face",
    "coursera.org": "Coursera",
    "stackoverflow.com": "Stack Overflow",
}

#: Fluff a site puts after its own name, and the suffix it repeats.
BRAND_FLUFF = re.compile(
    r"\s*[-|•–:]\s*(official\s*(website|site|portal)?|home(page)?|welcome.*)$"
    r"|\s*(online\s*portal|portal\s*home)$"
    r"|\.(com|org|in|net|gov|edu|co|io|ai)$",
    re.IGNORECASE,
)


#: What a cut can leave behind: "Government of Karnataka -" is not a name.
DANGLING_BRAND_PUNCTUATION = re.compile(r"[\s\-_|:&,./;]+$")


def trim_brand(text: str) -> str:
    """Strip the fluff, normalise shouting, and cut at a word boundary."""
    cleaned = BRAND_FLUFF.sub("", (text or "").strip()).strip()
    cleaned = _collapse(cleaned)

    # A brand in capitals reads as shouting — but a short unspaced one is an
    # acronym, and "Uidai" is worse than "UIDAI".
    if cleaned.isupper() and len(cleaned) > 3 and (" " in cleaned or len(cleaned) > 6):
        cleaned = cleaned.title()

    if len(cleaned) > MAX_BRAND_LENGTH:
        cut = cleaned[:MAX_BRAND_LENGTH]
        cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        cleaned = cut

    # Punctuation left hanging by the cut.
    return DANGLING_BRAND_PUNCTUATION.sub("", cleaned).strip()


def curated_brand(url: str) -> str:
    """The curated name for a host, exact or as a parent of a subdomain."""
    host = _host_label(url).lower()
    if not host:
        return ""
    if host in CURATED_BRANDS:
        return CURATED_BRANDS[host]
    for domain, name in CURATED_BRANDS.items():
        if host.endswith("." + domain):
            return name
    return ""


class BrandCanonicalAgent:
    """Brand and title resolution for one page."""

    def brand(self, soup: BeautifulSoup, url: str) -> str:
        """The site's own name, in five steps, and never empty.

        Each step is weaker evidence than the one before it: the publisher's own
        Open Graph tag, then its structured data, then its Twitter handle, then
        what it puts after the separator in its <title>, then the domain. The
        last two are guesses, but a guess that reads "India Today" beats a card
        with a blank line where the publisher should be.
        """
        # The page's own name leads. A department that calls itself
        # "Karnataka Health Department" knows something the curated list cannot,
        # and the list is a guess about a host while this is the publisher
        # speaking. The curated map answers when the page says nothing.
        from_meta = trim_brand(self._brand_from_meta(soup))
        if len(from_meta) >= 3:
            return from_meta

        curated = curated_brand(url)
        if curated:
            return trim_brand(curated)

        name = self._brand_from_json_ld(soup)
        if name:
            return name

        name = self._brand_from_twitter(soup)
        if name:
            return name

        name = self._brand_from_title_tail(soup)
        if name:
            return name

        # A card with a blank line where the publisher should be looks broken,
        # so the last resort is a name rather than nothing.
        return trim_brand(brand_from_domain(url) or _host_label(url)) or "Web Service"

    @staticmethod
    def _brand_from_meta(soup: BeautifulSoup) -> str:
        meta = soup.find("meta", attrs={"property": "og:site_name"})
        if meta and meta.get("content"):
            return _collapse(meta["content"])
        return ""

    def _brand_from_json_ld(self, soup: BeautifulSoup) -> str:
        for node in self._json_ld(soup):
            publisher = node.get("publisher") or node.get("sourceOrganization")
            if isinstance(publisher, dict) and publisher.get("name"):
                name = _collapse(str(publisher["name"]))
                if name:
                    return name
            if node.get("@type") in ("Organization", "WebSite", "NewsMediaOrganization") and node.get(
                "name"
            ):
                name = _collapse(str(node["name"]))
                if name:
                    return name
        return ""

    @staticmethod
    def _brand_from_twitter(soup: BeautifulSoup) -> str:
        """`twitter:site` is a handle — "@thehindu" — not a name.

        It is used as a name only when it reads like one: a handle of several
        joined words is split and title-cased, and a single lowercase word is
        left alone because "thehindu" is not a better brand than the domain.
        """
        meta = soup.find("meta", attrs={"name": "twitter:site"})
        if not (meta and meta.get("content")):
            return ""
        handle = _collapse(meta["content"]).lstrip("@")
        if not handle:
            return ""
        return _humanise_label(handle)

    @staticmethod
    def _brand_from_title_tail(soup: BeautifulSoup) -> str:
        """The publisher a site puts after the separator in its <title>.

        "District Health Notifications | Karnataka Health Department" ends with
        the publisher. Only a *trailing* part is taken, and only when the title
        actually has a separator — otherwise the whole title is the subject, not
        the brand.
        """
        title = soup.find("title")
        text = _collapse(title.get_text() if title else "")
        if not text:
            return ""
        for separator in ("|", " - ", " – ", " — ", " :: "):
            if separator in text:
                tail = text.rsplit(separator, 1)[-1].strip()
                if 2 < len(tail) <= 40:
                    return tail
        return ""

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


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "10_brand_canonical_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("TITLE_SEPARATORS", globals().get("TITLE_SEPARATORS"), _contract_rules.has_items(1), "TITLE_SEPARATORS must satisfy has_items(1)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
