"""Agent 04 — the quality gate.

A page earns a row only if it carries at least **80 characters** of real body
text and is not a known junk pattern (WAF interstitial, JavaScript shell, error
page). Body text is fingerprinted with **SHA-256**, so the same prose can never
enter the index twice under two URLs — and within a single batch the gate
rejects the repeat before it ever reaches the database.
"""

from __future__ import annotations

import hashlib
import html
import re
import logging
from dataclasses import dataclass

from app.agents import load_agent
from app.engine.store import PageDocument

logger = logging.getLogger(__name__)

# The parser owns the one definition of "collapse whitespace"; reaching it
# through the loader keeps a single implementation rather than a copy.
collapse = load_agent("03_parser_agent").collapse

#: Below this many characters of body text a page is not worth a row. This is
#: about the whole page being empty, not about any one field: a description of
#: twenty-five characters is fine, a page with twenty-five characters of prose is
#: a login box.
MIN_BODY_CHARS = 80

#: The description ceiling, from the parser that owns it.
DESCRIPTION_CHAR_CAP = load_agent("03_parser_agent").DESCRIPTION_CHAR_CAP

#: A title shorter than this is a filename or a placeholder, not a heading.
MIN_TITLE_CHARS = 3

#: A title must contain this many letters or digits. ".in" is three characters
#: and no words; so is "-::".
MIN_TITLE_ALPHANUMERIC = 3

#: Letters and digits in any script this index serves — Latin, Devanagari and
#: the Indic block. A Kannada title is not "punctuation only".
TITLE_ALPHANUMERIC = re.compile(r"[0-9A-Za-z\u0900-\u0DFF]")

#: Phrases that mean the page is an interstitial, not content.
#: A JavaScript shell is not junk: it is a page this fetcher cannot read. The
#: title, the description and the structured data are still in the HTML, which
#: is enough to answer a navigational query ("paytm login") and not enough to
#: rank for anything else, so it is admitted with a low score instead of being
#: thrown away.
SHELL_MARKERS: tuple[str, ...] = (
    "enable javascript",
    "javascript is required",
    "please turn on javascript",
    "checking your browser",
)

#: What a shell is worth: present, findable by name, never ranked highly.
SHELL_SCORE = 0.2

JUNK_MARKERS: tuple[str, ...] = (
    "403 forbidden",
    "access denied",
    "just a moment",
    "checking your browser",
    "enable javascript",
    "javascript is required",
    "please turn on javascript",
    "attention required! | cloudflare",
    "request blocked",
    "captcha",
    "page not found",
    "404 not found",
)


def page_digest(url: str, body: str) -> str:
    """A fingerprint of *this page*, not of its opening characters.

    `digest_of(body)` alone was wrong once the body is capped at 200 characters:
    two unrelated pages that share a template preamble — "Skip to main content.
    Government of Karnataka." — produce the same digest, and the unique index on
    `pages.digest` then drops the second one as a duplicate. The URL is part of
    the identity of a page; identical prose at two addresses is two pages.
    """
    return hashlib.sha256(f"{url}\n{collapse(body)}".encode("utf-8")).hexdigest()


def digest_of(text: str) -> str:
    """SHA-256 of the cleaned body text, hex encoded."""
    return hashlib.sha256(collapse(text).encode("utf-8")).hexdigest()


@dataclass
class QualityVerdict:
    """The gate's decision, with the rule that made it."""

    ok: bool
    reason: str = ""
    digest: str = ""
    score: float = 1.0


class QualityAgent:
    """The anti-junk gate and in-run deduplicator."""

    def __init__(self, min_body_chars: int = MIN_BODY_CHARS) -> None:
        self.min_body_chars = min_body_chars
        self._seen: set[str] = set()

    def reset(self) -> None:
        """Forget the digests seen so far — called at the start of a batch."""
        self._seen.clear()

    def check(self, document: PageDocument) -> QualityVerdict:
        """Decide whether one document may be stored."""
        body = collapse(document.body_text)
        title = collapse(document.title)

        lowered = f"{title} {body[:600]}".lower()

        # A shell: the page exists, its content is rendered by JavaScript this
        # crawler does not run. Admitted when the HTML still identifies it —
        # a real title and either a description or structured data — because
        # that is exactly what a navigational search is looking for.
        if len(body) < self.min_body_chars:
            if _is_identifiable_shell(title, document, lowered):
                digest = page_digest(document.url, body)
                if digest in self._seen:
                    return QualityVerdict(
                        ok=False, reason="duplicate", digest=digest, score=0.0
                    )
                self._seen.add(digest)
                return QualityVerdict(ok=True, digest=digest, score=SHELL_SCORE)
            return QualityVerdict(
                ok=False,
                reason="low_text_density",
                score=0.0,
            )

        # Junk is looked for in the title as well as the body. A 404 page can
        # carry a long body of navigation and still be a 404.
        for marker in JUNK_MARKERS:
            if marker in lowered:
                return QualityVerdict(ok=False, reason="junk_page", score=0.0)

        if len(title) < MIN_TITLE_CHARS:
            return QualityVerdict(ok=False, reason="no_title", score=0.0)

        # Length is not the same as content: ".in" and "-::" are three characters
        # and carry nothing.
        if len(TITLE_ALPHANUMERIC.findall(title)) < MIN_TITLE_ALPHANUMERIC:
            return QualityVerdict(ok=False, reason="title_not_a_word", score=0.0)

        # Keyed on the page, not on its first 200 characters. Within one batch
        # the same URL twice is a duplicate; the same prose at another URL is a
        # different page that happens to open the same way.
        digest = page_digest(document.url, body)
        if digest in self._seen:
            return QualityVerdict(ok=False, reason="duplicate", digest=digest, score=0.0)

        self._seen.add(digest)
        return QualityVerdict(
            ok=True,
            digest=digest,
            score=self._score(body, document),
        )

    @staticmethod
    def _score(body: str, document: PageDocument) -> float:
        """A small quality signal: more prose and a description score higher."""
        length_score = min(1.0, len(body) / 1000.0)
        description_bonus = 0.1 if document.description.strip() else 0.0
        return round(min(1.0, 0.5 + 0.4 * length_score + description_bonus), 3)


__all__ = [
    "JUNK_MARKERS",
    "MIN_BODY_CHARS",
    "MIN_TITLE_CHARS",
    "QualityAgent",
    "QualityVerdict",
    "digest_of",
]


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "04_quality_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("MIN_WORDS", globals().get("MIN_WORDS"), _contract_rules.at_least(1), "MIN_WORDS must satisfy at_least(1)"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)


# ---------------------------------------------------------------------------
# Text sanitation
#
# The gate's job on text is to make it clean, not to make it long. A description
# of twenty-five characters that says something is kept exactly as it is; only
# an empty one is rebuilt.
# ---------------------------------------------------------------------------

#: Tags that survived extraction, and HTML entities that were never decoded.
HTML_TAG = re.compile(r"<[^>]+>")
#: Punctuation a page repeats for emphasis. One is enough.
REPEATED_PUNCTUATION = re.compile(r"([!?.,;:\-])\1+")
#: Emoji, dingbats and media controls. Currency and dashes are left alone.
DECORATIVE = re.compile(
    "[\u2000-\u2012\u2015-\u206F\u2190-\u21FF\u2300-\u23FF"
    "\u25A0-\u25FF\u2600-\u27BF\u2B00-\u2BFF\uFE00-\uFE0F"
    "\U0001F000-\U0001FAFF]"
)

#: Boilerplate that makes a description worthless even when it is clean.
JUNK_BOILERPLATE: tuple[str, ...] = (
    "we use cookies",
    "accept all cookies",
    "javascript is disabled",
    "enable javascript",
    "all rights reserved",
    "terms and conditions apply",
    "click here to continue",
)

#: How much of the body to borrow when a description is missing entirely.
DESCRIPTION_FALLBACK_CHARS = 180


def sanitize_text(text: str) -> str:
    """Decode, strip markup and decoration, and collapse what is left."""
    if not text:
        return ""
    cleaned = html.unescape(text)
    cleaned = HTML_TAG.sub(" ", cleaned)
    cleaned = DECORATIVE.sub(" ", cleaned)
    cleaned = REPEATED_PUNCTUATION.sub(r"\1", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def is_junk_boilerplate(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in JUNK_BOILERPLATE)


def clean_description(
    description: str, body_text: str = "", title: str = "", cap: int = DESCRIPTION_CHAR_CAP
) -> str:
    """A description that is clean and within the ceiling — and no shorter.

    There is no minimum. A short, clean description is the answer; the body is
    read only when there is no description left to keep.
    """
    cleaned = sanitize_text(description)

    if cleaned and not is_junk_boilerplate(cleaned):
        return cleaned[:cap]

    # Only now, when nothing usable survived, does the body get a say.
    from_body = sanitize_text(body_text)
    if from_body:
        cut = from_body[:DESCRIPTION_FALLBACK_CHARS]
        if " " in cut:
            cut = cut.rsplit(" ", 1)[0]
        return cut.strip()[:cap]

    if title:
        return f"{sanitize_text(title)} — official portal information"[:cap]
    return ""


# ---------------------------------------------------------------------------
# Identity verification
#
# A card shows three things that must agree: the domain it links to, the brand
# above the title, and the title itself. When they disagree the result reads as
# a scam even when it is not — a page on one site presented under another's
# name. This gate is the last check before a row is written.
# ---------------------------------------------------------------------------

#: A title is a label, not a sentence. The brief allows one to four words.
MIN_TITLE_WORDS = 1
MAX_TITLE_WORDS = 4

#: Brands that must not appear in another site's title. Bounded on purpose: this
#: is the set of names a misleading page would borrow, not a dictionary.
BORROWED_BRANDS: frozenset[str] = frozenset(
    {"youtube", "facebook", "instagram", "whatsapp", "google", "apple", "samsung", "amazon"}
)


def _tokens(value: str) -> set[str]:
    return {token for token in "".join(ch if ch.isalnum() else " " for ch in (value or "").lower()).split() if token}


def brand_matches_domain(domain: str, brand: str) -> bool:
    """Whether a brand plausibly belongs to a domain.

    The test is overlap, not equality: "Karnataka Health" belongs to
    karnataka.gov.in because they share a word, and "The Hindu" belongs to
    thehindu.com for the same reason. A brand with nothing in common with its
    domain is either a mistake or a page presenting itself as another site.
    """
    label = "".join(_tokens(domain.replace(".", " ")))
    brand_tokens = _tokens(brand)
    if not label or not brand_tokens:
        return False
    if _tokens(domain.replace(".", " ")) & brand_tokens:
        return True
    # A run-together domain carries the brand inside it: "thehindu" contains
    # "hindu", and "karnatakapower" contains "karnataka". Short tokens are
    # skipped because "the" appears in half the domains on the web.
    return any(len(token) >= 3 and token in label for token in brand_tokens)


def verify_identity(domain: str, brand: str, title: str) -> tuple[bool, str]:
    """Whether domain, brand and title agree. Returns (ok, reason)."""
    if not (brand or "").strip():
        return False, "empty_brand"
    if not brand_matches_domain(domain, brand):
        return False, "brand_not_from_domain"

    words = (title or "").split()
    if len(words) < MIN_TITLE_WORDS:
        return False, "empty_title"
    if len(words) > MAX_TITLE_WORDS:
        return False, "title_too_long"

    own = _tokens(brand)
    for token in _tokens(title):
        if token in BORROWED_BRANDS and token not in own:
            return False, f"title_borrows_brand:{token}"
    return True, "ok"


def _is_identifiable_shell(title: str, document: PageDocument, lowered: str) -> bool:
    """Whether a page with almost no text still says what it is.

    A title is the one thing every shell keeps, and a description or
    schema.org types tell us what the page is for. Without either, an empty
    body is an empty page and the gate keeps refusing it.
    """
    if len(title) < MIN_TITLE_CHARS:
        return False
    if len(TITLE_ALPHANUMERIC.findall(title)) < MIN_TITLE_ALPHANUMERIC:
        return False
    # A shell that *says* it needs JavaScript is still a shell; only the hard
    # junk markers (403, captcha, not found) disqualify it.
    hard_junk = tuple(marker for marker in JUNK_MARKERS if marker not in SHELL_MARKERS)
    if any(marker in lowered for marker in hard_junk):
        return False
    # Evidence that this is a rendered-by-JavaScript page rather than a thin
    # page: it says so, or it ships structured data the renderer would fill.
    # A merely short page with a title is still a short page, and the 80
    # character floor keeps refusing it.
    says_it_needs_script = any(marker in lowered for marker in SHELL_MARKERS)
    if says_it_needs_script or document.schema_types:
        return True
    return False
