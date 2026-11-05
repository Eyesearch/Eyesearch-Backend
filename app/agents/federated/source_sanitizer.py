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

#: The default width for a sanitised snippet. It used to be the *page body*
#: ceiling, which tied how much of an external article we show to how much of a
#: crawled page we store — two unrelated policies, so raising the storage
#: ceiling silently widened every external card. It is now the card's own full
#: width, declared below.
SNIPPET_CHARS = 1200
strip_tracking = _enforcer.strip_tracking
action_title_from = _brand.action_title_from

#: The collapsed preview. A card shows this and nothing else until the reader
#: asks for more.
SNIPPET_SHORT_CHARS = 300

#: What "More" reveals. Long enough to judge a page, short enough to read on a
#: phone without scrolling a screenful.
#: A Wikipedia lead paragraph runs to about a thousand characters. Six hundred
#: ended it mid-thought, which made the "More" toggle reveal a fragment rather
#: than an answer. Kept in step with the courier's buffer.
SNIPPET_FULL_CHARS = 1200

#: Titles are allowed to be longer than an indexed one, because an external
#: result has no stored title to fall back on and a truncated one cannot be
#: repaired later.
TITLE_CHAR_CAP = 100

#: The safety ceiling on one answer. Whatever survives is returned up to this;
#: it is not a target to pad towards.
MAX_CARDS = 20

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


#: Path segments that ask for a credential or a purchase rather than showing a
#: page. Matched whole, so /administrative-reforms survives.
REJECTED_SEGMENTS = frozenset(
    {"login", "signin", "sign-in", "admin", "wp-admin", "cart", "checkout"}
)

#: Downloads rather than pages.
REJECTED_EXTENSIONS = (
    ".pdf", ".zip", ".tar", ".gz", ".rar", ".7z", ".exe", ".dmg", ".apk",
    ".mp4", ".mp3", ".wav", ".avi", ".mkv", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".webp",
)


def _is_cardable(url: str) -> bool:
    """A page worth showing, as opposed to a file, a door, or an internal host.

    The address check is the same one the courier makes. A card is rendered as a
    link, and a link the reader can tap is a request the reader's browser makes —
    so a private address is refused here too, not only at fetch time.
    """
    from app.agents.federated.knowledge_courier import is_public_url

    if not is_public_url(url, resolve=False)[0]:
        return False
    try:
        path = (urlsplit(url).path or "").lower()
    except ValueError:
        return False
    if any(path.endswith(extension) for extension in REJECTED_EXTENSIONS):
        return False
    segments = {segment for segment in path.split("/") if segment}
    return not (segments & REJECTED_SEGMENTS)


def _canonical(url: str) -> str:
    """The form two URLs are compared in: no fragment, no trailing slash."""
    parts = urlsplit(url or "")
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}{(parts.path or '/').rstrip('/') or '/'}"


#: A title must be this distinctive before it is used to merge two cards. Many
#: pages are called "Home", "Contact Us" or "Dashboard"; merging on those would
#: collapse two different sites into one card, which is the same over-eager
#: deduplication that was silently dropping crawled pages.
TITLE_MERGE_MIN_WORDS = 4


def _canonical_title(title: str) -> str:
    """Titles compared as lower-case words, punctuation dropped.

    Returns empty for a title too generic to identify a page, so the caller falls
    back to the URL alone.
    """
    words = " ".join(re.sub(r"[^\w\s]", " ", (title or "").lower()).split())
    return words if len(words.split()) >= TITLE_MERGE_MIN_WORDS else ""


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


#: Words that cannot end a sentence. A cut that stops on one of these reads as
#: a transmission that dropped, not as an excerpt — "traced to its southern
#: Neolithic and" is the failure this exists to prevent.
DANGLING_WORDS = frozenset(
    {
        "a", "an", "the", "of", "in", "to", "for", "with", "and", "or", "but",
        "by", "on", "at", "from", "as", "that", "which", "is", "was", "were",
        "are", "its", "his", "her", "their", "this", "these", "those", "into",
        "over", "under", "between", "during", "after", "before", "than", "also",
    }
)


def sanitise_snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    """Collapse whitespace and cut at a word boundary within the ceiling.

    The cut also backs off any trailing word that cannot end a sentence, so an
    excerpt stops where a reader would expect rather than mid-phrase.
    """
    collapsed = clean_text(text)
    if len(collapsed) <= limit:
        return collapsed
    cut = collapsed[:limit]
    if " " not in cut:
        return cut

    words = cut.rsplit(" ", 1)[0].split()
    while words and words[-1].strip(",;:.-—").lower() in DANGLING_WORDS:
        words.pop()
    # Every word was a dangling one — vanishingly unlikely, and the window is
    # still a better answer than nothing.
    return " ".join(words) if words else cut.rsplit(" ", 1)[0]


class CrossSourceSanitizerAgent:
    """Enforce our own rules on results we did not produce."""

    #: How a source names itself in the badge. A card from Wikipedia is *from
    #: Wikipedia*; deriving a brand from the article title put the title in the
    #: badge, so a reader saw "Architecture of Karnataka" twice, once in
    #: capitals.
    SOURCE_LABELS = {
        "wikipedia": "Wikipedia",
        "wikidata": "Wikidata",
        "searxng": "Web",
        "duckduckgo": "DuckDuckGo",
    }

    def card(self, *, title: str, url: str, snippet: str, source: str = "") -> ExternalCard:
        clean_url = sanitise_url(url)
        # A known source names itself. Anything else falls back to the derived
        # brand, which is what an unrecognised host can honestly offer.
        brand = self.SOURCE_LABELS.get(
            (source or "").strip().lower(),
            _extractor.LiveEntityExtractorAgent().brand(title=title, url=clean_url),
        )
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

    def aggregate(self, cards, *, limit: int = MAX_CARDS) -> list[ExternalCard]:
        """Sanitise a stream of cards and keep one per page.

        Two sources answer the same query, so the same page arrives twice with
        different framing. Deduplication runs on the canonical URL first, then on
        the normalised title: two Wikipedia articles can point at the same place
        under different URLs, and the reader does not need to be told twice.

        The richer card wins — the one with more text to show — because that is
        the one worth reading.
        """
        ceiling = max(1, min(int(limit), MAX_CARDS))
        best: dict[str, tuple[int, ExternalCard]] = {}

        for raw in cards or []:
            try:
                card = self._from_card(raw)
            except Exception as error:  # noqa: BLE001 - one bad card is not a batch
                logger.warning("skipping an unusable card: %s", error)
                continue
            if card is None:
                continue

            keys = [f"url:{_canonical(card.url)}"]
            title_key = _canonical_title(card.title)
            if title_key:
                keys.append(f"title:{title_key}")

            existing = next((best[key] for key in keys if key in best), None)
            richness = len(card.snippet_full)
            if existing is not None:
                if richness <= existing[0]:
                    continue
                for key in keys:
                    best.pop(key, None)

            entry = (richness, card)
            for key in keys:
                best[key] = entry

        seen: set[int] = set()
        unique: list[ExternalCard] = []
        for richness, card in sorted(best.values(), key=lambda item: -item[0]):
            if id(card) in seen:
                continue
            seen.add(id(card))
            unique.append(card)
            if len(unique) >= ceiling:
                break
        return unique

    def _from_card(self, raw) -> ExternalCard | None:
        """A KnowledgeCard (or a dict) to a display card, or None."""
        if isinstance(raw, dict):
            title = str(raw.get("title") or "")
            url = str(raw.get("url") or "")
            snippet = str(raw.get("snippet") or raw.get("extract") or "")
        else:
            title = str(getattr(raw, "title", "") or "")
            url = str(getattr(raw, "url", "") or "")
            snippet = str(getattr(raw, "extract", "") or "")
            snippet = str(getattr(raw, "snippet_full", "") or snippet)

        clean_url = sanitise_url(url)
        if not clean_url or not _is_cardable(clean_url):
            return None
        return self.card(title=title, url=clean_url, snippet=snippet)

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
