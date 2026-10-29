"""The links a person actually wants, not every anchor on the page.

A government portal has hundreds of anchors — breadcrumbs, footer columns,
social icons, language switches. The handful that matter are the actions:
"Contact Us", "Apply Online", "Track Status", "Download Form", "Login".

They are found by intent rather than by position, because the position differs on
every site. The patterns are verbs and nouns a person uses, in English and in the
Indian languages this index covers — no site is named, so nothing goes stale when
a portal redesigns.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin

logger = logging.getLogger(__name__)

#: Action intents, in the order worth showing. Each entry is (intent, patterns).
INTENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apply", ("apply online", "apply now", "apply for", "registration form", "आवेदन", "ಅರ್ಜಿ")),
    ("track", ("track status", "check status", "track application", "status enquiry", "स्थिति", "ಸ್ಥಿತಿ")),
    ("download", ("download", "download form", "pdf", "डाउनलोड", "ಡೌನ್ಲೋಡ್")),
    ("contact", ("contact us", "contact", "helpline", "support", "संपर्क", "ಸಂಪರ್ಕ")),
    ("login", ("log in", "login", "sign in", "signin", "साइन इन", "ಲಾಗಿನ್")),
    ("register", ("sign up", "signup", "register", "create account", "नोंदणी", "ನೋಂದಣಿ")),
    ("pay", ("pay now", "make payment", "pay online", "भुगतान", "ಪಾವತಿ")),
)

#: An anchor longer than this is a sentence, not a link label.
MAX_LABEL_CHARS = 40
#: How many sitelinks to return.
MAX_SITELINKS = 5

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Sitelink:
    intent: str
    label: str
    url: str

    def as_dict(self) -> dict[str, str]:
        return {"intent": self.intent, "label": self.label, "url": self.url}


def classify_label(text: str) -> str:
    """The intent an anchor label expresses, or "" for none."""
    label = _WHITESPACE.sub(" ", (text or "").strip()).lower()
    if not label or len(label) > MAX_LABEL_CHARS:
        return ""
    for intent, patterns in INTENTS:
        if any(pattern in label for pattern in patterns):
            return intent
    return ""


def extract_sitelinks(soup, page_url: str, limit: int = MAX_SITELINKS) -> list[Sitelink]:
    """The highest-value actions on a page, one per intent, in intent order."""
    seen: dict[str, Sitelink] = {}

    for anchor in soup.find_all("a", href=True):
        label = _WHITESPACE.sub(" ", anchor.get_text(" ").strip())
        intent = classify_label(label)
        if not intent or intent in seen:
            continue
        href = urljoin(page_url, anchor["href"].strip())
        if not href.startswith(("http://", "https://")):
            continue
        seen[intent] = Sitelink(intent=intent, label=label[:MAX_LABEL_CHARS], url=href)

    order = [intent for intent, _ in INTENTS]
    return sorted(seen.values(), key=lambda link: order.index(link.intent))[:limit]
