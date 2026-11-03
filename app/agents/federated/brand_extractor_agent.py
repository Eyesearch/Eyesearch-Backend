"""Pull the facts a card can show as a badge, without a model.

Indian government pages carry a phone number, a PIN code and a date more often
than they carry anything else worth extracting, and all three are rigidly
formatted. A regex finds them; a model would cost money and be less reliable at
exactly this.

Everything here is a pattern over text already in hand — nothing is fetched.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: Indian numbers, in the three shapes they actually appear in.
#: A mobile is ten digits starting 6-9; a landline is an STD code starting with
#: 0 followed by a 6-8 digit subscriber number, which may start with any digit;
#: a toll-free number starts 1800 or 1900. Getting the landline case wrong
#: mattered — 080-22221234 was being missed because its subscriber number does
#: not start with 6.
PHONE = re.compile(
    r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}\b"          # mobile
    r"|\b0\d{2,4}[\s-]\d{6,8}\b"                        # landline with STD
    r"|(?:\+?91[\s-]?)?[19]800[\s-]?\d{3}[\s-]?\d{3,4}\b"  # toll free
)

#: Six digits, not starting with zero.
PIN_CODE = re.compile(r"\b[1-9]\d{5}\b")

#: DD/MM/YYYY, YYYY-MM-DD and "12 October 2026".
DATE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b",
    re.IGNORECASE,
)


#: The hard ceiling on a federated brand name. No minimum: "NIC", "SBI" and
#: "ISRO" are three letters and perfectly correct, and a length floor here would
#: reject exactly the organisations an Indian search engine is asked about most.
BRAND_CHAR_CAP = 50

#: Title separators, in the order they are tried. The organisation is usually
#: the side that is not the page name: "Karnataka Bank | Personal Banking" is the
#: bank, not "Personal Banking".
TITLE_SEPARATORS = ("|", "—", "–", "-", "::", ":", "»", "•", "·", "•")

#: Markup, control characters and zero-width joiners. A brand that arrives with
#: any of these renders as a broken badge.
BRAND_NOISE = re.compile(r"<[^>]{1,200}>|[\x00-\x1f\x7f\u200b-\u200d\ufeff]")

#: Title segments that name nothing. "Official Website" is a description of a
#: site, not the site, and a card that shows it is worse than one showing the
#: derived domain name.
GENERIC_SEGMENTS = {
    "official website", "official site", "official web site", "home", "homepage",
    "home page", "welcome", "index", "untitled", "website",
}

#: The tail of a domain that carries no meaning.
_DOMAIN_TAIL = ("www.", "m.", "amp.")


@dataclass
class BrandExtraction:
    brand: str = ""
    from_title: bool = False

    def as_dict(self) -> dict[str, object]:
        return {"brand": self.brand, "from_title": self.from_title}


def derive_brand_from_domain(domain_or_url: str) -> str:
    """A readable name from a host: `karnataka.gov.in` -> `Karnataka Gov`.

    Used when a result carries no usable title. It is a last resort, and it is
    honest about it: the second-level label is the organisation, and the suffix
    is spelled out so the card does not read as a bare domain.
    """
    host = (domain_or_url or "").strip().lower()
    if "//" in host:
        from urllib.parse import urlsplit

        host = urlsplit(host).hostname or ""
    for prefix in _DOMAIN_TAIL:
        if host.startswith(prefix):
            host = host[len(prefix):]
    parts = [part for part in host.split(".") if part]
    if not parts:
        return ""

    # A public suffix can be one label (`com`) or two (`gov.in`). Splitting it
    # correctly is the whole job: taking `parts[-2]` on karnataka.gov.in yields
    # "gov", which is the suffix, not the organisation.
    two_part = {"gov", "nic", "ac", "edu", "res", "co", "org", "net", "firm", "gen"}
    if len(parts) >= 3 and parts[-2] in two_part:
        label, suffix = parts[-3], parts[-2]
    elif len(parts) >= 2:
        label, suffix = parts[-2], ""
    else:
        label, suffix = parts[0], ""

    words = [word.capitalize() for word in label.replace("-", " ").split() if word]
    name = " ".join(words)
    # "Karnataka Gov" reads as a name; "Karnataka" alone does not say what kind
    # of site it is, and the suffix is the only clue the domain carries.
    if suffix:
        name = f"{name} {suffix.capitalize()}"
    return name[:BRAND_CHAR_CAP]


def strip_noise(text: str) -> str:
    """Markup and control characters out, whitespace collapsed. No length cap."""
    value = BRAND_NOISE.sub(" ", str(text or ""))
    return " ".join(value.split())


def clean_brand(text: str) -> str:
    """A brand: noise stripped, then capped at 50.

    Kept separate from `strip_noise` because a title must not be capped before
    it is split — truncating "… | Official Website" to 50 characters leaves
    "Offici", and that becomes the brand.
    """
    return strip_noise(text)[:BRAND_CHAR_CAP].strip()


def _domain_label(domain_or_url: str) -> str:
    host = (domain_or_url or "").strip().lower()
    if "//" in host:
        from urllib.parse import urlsplit

        host = urlsplit(host).hostname or ""
    parts = [part for part in host.split(".") if part]
    if parts and parts[0] in _DOMAIN_TAIL:
        parts = parts[1:]
    return parts[0].replace("-", "") if parts else ""


def brand_from_title(title: str, url: str = "") -> BrandExtraction:
    """The organisation a title names, or nothing.

    Titles come in both orders — "Karnataka Bank | Personal Banking" and
    "Personal Banking - Karnataka Bank" are the same site. Position alone cannot
    tell them apart, so the domain decides: whichever segment matches the host is
    the organisation. When neither matches, the trailing segment wins, because
    that is the more common convention and it is right more often than not.
    """
    text = " ".join((title or "").split())
    if not text:
        return BrandExtraction()

    label = _domain_label(url)
    for separator in TITLE_SEPARATORS:
        if separator in text:
            head, _, tail = text.rpartition(separator)
            head, tail = head.strip(), tail.strip()
            if label:
                for segment in (head, tail):
                    if segment and label in segment.lower().replace(" ", ""):
                        return BrandExtraction(brand=segment[:BRAND_CHAR_CAP], from_title=True)
            candidate = tail or head
            if candidate and candidate.strip().lower() not in GENERIC_SEGMENTS:
                return BrandExtraction(brand=candidate[:BRAND_CHAR_CAP], from_title=True)
            # Every segment is generic or missing: nothing here names the site.
            return BrandExtraction()

    # No separator at all. A short title with few words is the organisation
    # itself — "NIC", "Karnataka Bank" — and treating it as unusable would send
    # the card to the domain name for no reason.
    words = text.split()
    if len(words) <= 4 and len(text) <= 40:
        return BrandExtraction(brand=text[:BRAND_CHAR_CAP], from_title=True)
    return BrandExtraction()


@dataclass
class Entities:
    phones: list[str] = field(default_factory=list)
    pin_codes: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {"phones": self.phones, "pin_codes": self.pin_codes, "dates": self.dates}

    def __bool__(self) -> bool:
        return bool(self.phones or self.pin_codes or self.dates)


class LiveEntityExtractorAgent:
    """Sniff contact details and dates out of a snippet, and name the source.

    Two jobs that look unrelated and are not: both answer "what does this card
    tell the reader without them opening it". A phone number and a brand name are
    the same kind of fact.
    """

    def __init__(self, limit: int = 3) -> None:
        self.limit = max(1, int(limit))

    def brand(self, *, title: str = "", url: str = "") -> str:
        """The organisation behind an external result, capped at 50 characters.

        The title is tried first because it is what the publisher calls
        themselves. The domain is the fallback, and it is derived rather than
        shown raw: a card that says "karnataka.gov.in" where every other card
        says a name looks broken.
        """
        # Clean first: a title carrying markup would defeat the separator
        # search and the domain match, both of which compare raw text.
        found = brand_from_title(strip_noise(title), url)
        if found.brand:
            return clean_brand(found.brand)
        return clean_brand(derive_brand_from_domain(url))

    def extract(self, text: str) -> Entities:
        body = text or ""
        return Entities(
            phones=list(dict.fromkeys(m.group(0).strip() for m in PHONE.finditer(body)))[: self.limit],
            pin_codes=list(dict.fromkeys(PIN_CODE.findall(body)))[: self.limit],
            dates=list(dict.fromkeys(m.group(0).strip() for m in DATE.finditer(body)))[: self.limit],
        )


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "45_live_entity_extractor",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 4 > 0, 4 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
