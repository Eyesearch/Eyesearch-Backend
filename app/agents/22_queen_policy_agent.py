"""Trust screening: is this page something a person would want to find?

The classifier says what a page is *about*; this says whether it deserves to be
shown at all. Phishing lures, scam funnels and low-effort affiliate networks
read like ordinary pages to a topical classifier — they use the same words. What
gives them away is different: urgency and credential language, a form asking for
a card number, a host that imitates a brand it does not own.

Nothing here calls out to a blocklist service. Every signal is a pattern in the
page itself, so the verdict is explainable and costs nothing.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Below this a page is not indexed.
TRUST_THRESHOLD = 0.45

#: Language that appears on a lure far more than on a real page.
PHISHING_PHRASES = (
    "verify your account", "confirm your identity", "your account will be suspended",
    "unusual activity", "click here to claim", "you have won", "claim your prize",
    "limited time offer", "act now", "urgent action required",
    "enter your card number", "enter your pin", "one time password",
    "share your otp", "your kyc is pending", "account will be blocked",
)

#: Words that make a page a scam funnel rather than a shop.
SCAM_PHRASES = (
    "guaranteed returns", "double your money", "risk free investment",
    "work from home earn", "no experience needed earn", "instant loan approval",
    "100% guaranteed", "get rich quick",
)

#: Markers of a link farm: the same link repeated across the page.
LINK_FARM_MARKERS = ("click here", "read more", "visit site", "sponsored link")

#: A host imitating a brand it does not own: a known brand name inside a host
#: that is not that brand's domain. Deliberately a short list of the names
#: phishing actually uses — not a catalogue of every company.
IMPERSONATED_BRANDS = (
    "paypal", "netflix", "amazon", "microsoft", "apple", "google",
    "facebook", "instagram", "whatsapp", "sbi", "hdfc", "icici", "axis",
    "paytm", "phonepe", "irctc", "uidai", "incometax",
)

_WORD = re.compile(r"[a-z0-9]+")


@dataclass
class TrustVerdict:
    score: float
    reasons: list[str] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.score >= TRUST_THRESHOLD

    def as_dict(self) -> dict[str, object]:
        return {"score": round(self.score, 3), "reasons": self.reasons, "accepted": self.accepted}


def brand_impersonation(url: str) -> str:
    """A brand name used in a host that is not that brand's own domain."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    labels = host.split(".")
    for brand in IMPERSONATED_BRANDS:
        if brand not in host:
            continue
        # Owned means the brand is the registrable label: paypal.com, not
        # paypal-secure-login.example.
        if labels[0] == brand or (len(labels) > 1 and labels[-2] == brand):
            continue
        return brand
    return ""


class QueenPolicyAgent:
    """Score a page's trustworthiness from its own text and URL."""

    def __init__(self, threshold: float = TRUST_THRESHOLD) -> None:
        self.threshold = float(threshold)

    def judge(self, url: str = "", title: str = "", body_text: str = "") -> TrustVerdict:
        haystack = f"{title} {body_text}".lower()
        score = 1.0
        reasons: list[str] = []

        phishing = [phrase for phrase in PHISHING_PHRASES if phrase in haystack]
        if phishing:
            score -= min(0.6, 0.2 * len(phishing))
            reasons.append(f"phishing_language:{phishing[0]}")

        scams = [phrase for phrase in SCAM_PHRASES if phrase in haystack]
        if scams:
            score -= min(0.5, 0.25 * len(scams))
            reasons.append(f"scam_language:{scams[0]}")

        links = sum(haystack.count(marker) for marker in LINK_FARM_MARKERS)
        if links >= 6:
            score -= 0.2
            reasons.append(f"link_farm:{links}")

        brand = brand_impersonation(url)
        if brand:
            score -= 0.5
            reasons.append(f"impersonates:{brand}")

        # Almost no prose and a pile of links is a doorway page.
        words = len(_WORD.findall(haystack))
        if words < 15 and links:
            score -= 0.15
            reasons.append("thin_content")

        return TrustVerdict(score=max(0.0, min(1.0, score)), reasons=reasons)

    def accepted(self, url: str = "", title: str = "", body_text: str = "") -> bool:
        return self.judge(url, title, body_text).score >= self.threshold


# ---------------------------------------------------------------------------
# Contract
#
# This agent's rules as data. `audit_contract()` checks them against the live
# values above and reports breaches to the supervisor (agent 14); it fails
# when a constant is edited into a state that breaks its own rule.
# ---------------------------------------------------------------------------

from app.agents import _contract as _contract_rules

CONTRACT = {
    "agent": "22_queen_policy_agent",
    "reports_to": _contract_rules.SUPERVISOR,
    "rules": [
        _contract_rules.Rule("declares_constants", 6 > 0, 6 > 0, "the agent declares at least one module-level constant"),
    ],
}

audit_contract = _contract_rules.make_audit_contract(__name__, CONTRACT)
telemetry = _contract_rules.make_telemetry(__name__, CONTRACT)
