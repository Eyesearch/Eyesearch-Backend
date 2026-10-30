"""What goes out on a federated request.

A search engine asking a third party a question is telling that third party
something. The least it can do is not also hand over who is asking: no client
IP, no cookies, no fingerprinting User-Agent, no Referer chain back to us.

The mask is a header set, kept here so every outbound call uses the same one.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: Headers that identify the caller and have no business leaving.
STRIPPED_HEADERS = frozenset(
    {
        "cookie", "set-cookie", "authorization", "proxy-authorization",
        "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "x-real-ip",
        "forwarded", "via", "from", "referer", "origin",
        "x-client-ip", "cf-connecting-ip", "true-client-ip",
    }
)

#: A generic, honest identity: what we are, not which browser we pretend to be.
OUTBOUND_USER_AGENT = "EyesearchBot/1.0 (+https://eyesearch.github.io/Eyesearch/)"


class PrivacyMaskAgent:
    """Build the header set for an outbound request."""

    def __init__(self, user_agent: str = OUTBOUND_USER_AGENT) -> None:
        self.user_agent = user_agent

    def mask(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        """Strip identifying headers, then set ours."""
        masked = {
            key: value
            for key, value in (headers or {}).items()
            if key.lower() not in STRIPPED_HEADERS
        }
        masked["User-Agent"] = self.user_agent
        # Ask for no tracking and accept nothing that could identify us.
        masked.setdefault("Accept", "application/json")
        masked["DNT"] = "1"
        return masked

    def clean(self, headers: dict[str, str] | None = None) -> dict[str, str]:
        return self.mask(headers)


# ---------------------------------------------------------------------------
# PII redaction
#
# What is redacted by default is what is protected by law or by expectation: an
# Aadhaar number, and an e-mail address. A phone number is *not* redacted by
# default, and that is a deliberate choice — a district office's helpline is
# published so that people can call it, and blanking it out would take away the
# most useful thing on the page. Set `redact_phones=True` where a corpus really
# does carry personal numbers.
# ---------------------------------------------------------------------------

#: Twelve digits, printed as 4-4-4 or run together. An Aadhaar number never
#: begins with 0 or 1, which is what keeps the pattern from eating order IDs.
AADHAAR = re.compile(r"\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
#: Indian mobile numbers, with or without the country code.
PHONE = re.compile(r"(?:(?:\+|0{0,2})91[\s-]?)?\b[6-9]\d{4}[\s-]?\d{5}\b")

REDACTED = {
    "aadhaar": "[redacted:aadhaar]",
    "email": "[redacted:email]",
    "phone": "[redacted:phone]",
}


class PiiRedactor:
    """Removes personal identifiers from text before it is stored or shown."""

    def __init__(self, *, redact_phones: bool = False) -> None:
        self.redact_phones = redact_phones

    def redact(self, text: str) -> str:
        if not text:
            return text
        # Aadhaar first: a twelve-digit run could otherwise be partly consumed
        # by the phone pattern, leaving an unredacted fragment behind.
        cleaned = AADHAAR.sub(REDACTED["aadhaar"], text)
        cleaned = EMAIL.sub(REDACTED["email"], cleaned)
        if self.redact_phones:
            cleaned = PHONE.sub(REDACTED["phone"], cleaned)
        return cleaned

    def found(self, text: str) -> dict[str, int]:
        """What would be redacted, without changing anything."""
        counts = {
            "aadhaar": len(AADHAAR.findall(text or "")),
            "email": len(EMAIL.findall(text or "")),
        }
        if self.redact_phones:
            counts["phone"] = len(PHONE.findall(text or ""))
        return {kind: count for kind, count in counts.items() if count}
