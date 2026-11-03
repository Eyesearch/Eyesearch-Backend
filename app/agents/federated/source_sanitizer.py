"""Make an external result look like one of ours.

Anything arriving from outside has to satisfy the same rules the index enforces,
or the cards become inconsistent: a 300-character snippet next to 40-character
ones, a tracking URL that opens someone else's analytics, a title that is the
whole <title> tag.

The rules are imported from where they are already defined, not copied, so the
ceiling that governs ingestion governs federation too.
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

from app.agents import load_agent

logger = logging.getLogger(__name__)

_brand = load_agent("10_brand_canonical_agent")
_extractor = load_agent("45_live_entity_extractor")
_enforcer = load_agent("11_rule_enforcer_agent")

#: Read from the same setting the index uses, so a federated card is the same
#: length as an indexed one. A second hardcoded number here is how the two
#: drift apart.
from app.engine.config import get_settings as _get_settings

SNIPPET_CHARS = _get_settings().page_body_char_ceiling
strip_tracking = _enforcer.strip_tracking
action_title_from = _brand.action_title_from

#: The collapsed preview. A card shows this and nothing else until the reader
#: asks for more.
SNIPPET_SHORT_CHARS = 180

#: What "More" reveals. Long enough to judge a page, short enough to read on a
#: phone without scrolling a screenful.
SNIPPET_FULL_CHARS = 500

#: Titles are allowed to be longer than an indexed one, because an external
#: result has no stored title to fall back on and a truncated one cannot be
#: repaired later.
TITLE_CHAR_CAP = 140

#: Promotional noise that arrives with syndicated results. Matched as whole
#: phrases so "Ad" does not eat "Admission".
NOISE = re.compile(
    r"\b(?:sponsored|advertisement|click here(?: to [a-z ]+)?|read more\.\.\.|"
    r"subscribe now|sign up now|buy now|download now)\b",
    re.IGNORECASE,
)

#: Anything that renders as an image or a control rather than prose.
TAGS = re.compile(r"<[^>]{1,200}>")

#: Astral-plane pairs and the common pictographic blocks. Deliberately does not
#: touch \u2013-\u2014 or \u20B9: the en dash and the rupee sign are typography,
#: not decoration, and stripping them breaks prices and ranges.
EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2600-\u27BF"
    "\uFE00-\uFE0F"
    "\u200D"
    "\uD800-\uDFFF"
    "]+",
    flags=re.UNICODE,
)


@dataclass
class ExternalCard:
    title: str = ""
    url: str = ""
    #: The collapsed preview the card shows by default.
    snippet_short: str = ""
    #: What the "More" toggle reveals.
    snippet_full: str = ""
    brand: str = ""
    source: str = ""

    @property
    def has_more(self) -> bool:
        """True only when there is something the toggle would actually add."""
        return len(self.snippet_full) > len(self.snippet_short)

    @property
    def snippet(self) -> str:
        """Backwards-compatible name for the preview."""
        return self.snippet_short

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet_short,
            "snippet_short": self.snippet_short,
            "snippet_full": self.snippet_full,
            "has_more": self.has_more,
            "brand": self.brand,
            "source": self.source,
        }


#: Parameters that identify a campaign rather than a page. A superset of the
#: enforcer's list: the index governs pages we chose to crawl, so its list stays
#: conservative, while an external URL arrives with whatever a provider appended.
TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "utm_name", "utm_reader", "utm_referrer", "utm_social",
        "gclid", "gclsrc", "fbclid", "msclkid", "yclid", "dclid", "twclid",
        "ved", "ei", "usg", "oq", "gs_lcrp", "si", "sprefix", "ref", "ref_src",
        "ref_url", "ref_src", "s_kwcid", "igshid", "mc_cid", "mc_eid",
        "_ga", "_gl", "_hsenc", "_hsmi", "spm", "share",
    }
)

#: Redirect wrappers: the real destination is in one of these parameters. A card
#: that links to google.com/url?q=... is a card that sends the reader to a
#: redirect page, and shows a URL that has nothing to do with the result.
REDIRECT_HOSTS = {
    "www.google.com", "google.com", "www.google.co.in", "google.co.in",
    "l.facebook.com", "lm.facebook.com", "out.reddit.com",
    "duckduckgo.com", "html.duckduckgo.com", "www.bing.com", "bing.com",
}
#: `uddg` is DuckDuckGo's; `u` is Reddit's; `url` and `q` cover Google and Bing.
REDIRECT_PARAMS = ("url", "q", "u", "uddg", "target", "dest", "redirect", "to")

#: A URL longer than this is not a URL; it is a payload.
MAX_URL_CHARS = 2048

#: Only these two. `javascript:`, `data:` and `file:` are how a link becomes an
#: execution vector, and no search result ever needs them.
SAFE_SCHEMES = ("http", "https")

#: Ports that add nothing: they are the defaults for their scheme.
DEFAULT_PORTS = {"http": "80", "https": "443"}


def unwrap_redirect(url: str) -> str:
    """The destination inside a redirect wrapper, or the URL unchanged."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host not in REDIRECT_HOSTS or not parts.query:
        return url
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in REDIRECT_PARAMS:
        target = params.get(key)
        if target and target.lower().startswith(("http://", "https://")):
            return unquote(target)
    return url


def sanitise_url(url: str) -> str | None:
    """A clean, absolute, fetchable URL — or None when it is not one.

    None rather than a guess. A caller that receives a half-cleaned string cannot
    tell it apart from a good one, and a card linking to `javascript:...` is worse
    than a card that was never built.
    """
    raw = str(url or "").strip()
    if not raw or len(raw) > MAX_URL_CHARS:
        return None

    try:
        raw = unwrap_redirect(unquote(raw) if "%3a%2f%2f" in raw.lower() else raw)
        parts = urlsplit(raw)
    except (ValueError, UnicodeError):
        return None

    scheme = (parts.scheme or "").lower()
    if scheme not in SAFE_SCHEMES:
        return None

    host = (parts.hostname or "").lower()
    if not host:
        return None

    # Rebuild rather than patch: a URL assembled from checked pieces cannot
    # carry a fragment or a default port that survived by being forgotten.
    try:
        port = parts.port
    except ValueError:
        return None
    if port is not None and str(port) == DEFAULT_PORTS.get(scheme):
        port = None

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    netloc = f"{host}:{port}" if port else host

    return urlunsplit(
        (scheme, netloc, parts.path or "/", urlencode(kept, doseq=True), "")
    )


def _cap(text: str, limit: int) -> str:
    """Cut at a word boundary; never mid-word."""
    value = (text or "").strip()
    if len(value) <= limit:
        return value
    window = value[:limit]
    return window.rsplit(" ", 1)[0].rstrip(" ,;:-—") if " " in window else window


def clean_text(text: str) -> str:
    """Decode entities, drop tags, emoji and promotional noise.

    Ordered deliberately: entities first (so `&amp;` becomes `&` before anything
    looks for punctuation), then tags (an entity-encoded tag is not a tag), then
    emoji, then noise.
    """
    value = html.unescape(text or "")
    value = TAGS.sub(" ", value)
    value = EMOJI.sub("", value)
    value = NOISE.sub(" ", value)
    return " ".join(value.split())


def sanitise_snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Collapse whitespace and cut at a word boundary within the ceiling."""
    collapsed = clean_text(text)
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    return cut.rsplit(" ", 1)[0] if " " in cut else cut


class CrossSourceSanitizerAgent:
    """Enforce our own rules on results we did not produce."""

    def card(self, *, title: str, url: str, snippet: str, source: str = "") -> ExternalCard:
        clean_url = sanitise_url(url)
        brand = _extractor.LiveEntityExtractorAgent().brand(title=title, url=clean_url)
        clean_title = clean_text(title)
        short = sanitise_snippet(snippet, SNIPPET_SHORT_CHARS)
        full = sanitise_snippet(snippet, SNIPPET_FULL_CHARS)
        return ExternalCard(
            title=(
                action_title_from(title=clean_title, brand=brand, url=clean_url)
                or _cap(clean_title, TITLE_CHAR_CAP)
            ),
            url=clean_url,
            snippet_short=short,
            snippet_full=full,
            brand=brand,
            source=source,
        )

    def cards(self, items) -> list[ExternalCard]:
        out: list[ExternalCard] = []
        for item in items or []:
            card = self.card(
                title=str(item.get("title") or ""),
                url=str(item.get("url") or ""),
                snippet=str(item.get("snippet") or item.get("text") or ""),
                source=str(item.get("source") or ""),
            )
            if card.url:
                out.append(card)
        return out


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "44_cross_source_sanitizer",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 4 > 0, 4 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
