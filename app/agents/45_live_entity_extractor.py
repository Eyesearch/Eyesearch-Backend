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
    """Sniff contact details and dates out of a snippet."""

    def __init__(self, limit: int = 3) -> None:
        self.limit = max(1, int(limit))

    def extract(self, text: str) -> Entities:
        body = text or ""
        return Entities(
            phones=list(dict.fromkeys(m.group(0).strip() for m in PHONE.finditer(body)))[: self.limit],
            pin_codes=list(dict.fromkeys(PIN_CODE.findall(body)))[: self.limit],
            dates=list(dict.fromkeys(m.group(0).strip() for m in DATE.finditer(body)))[: self.limit],
        )
