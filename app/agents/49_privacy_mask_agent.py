"""What goes out on a federated request.

A search engine asking a third party a question is telling that third party
something. The least it can do is not also hand over who is asking: no client
IP, no cookies, no fingerprinting User-Agent, no Referer chain back to us.

The mask is a header set, kept here so every outbound call uses the same one.
"""

from __future__ import annotations

import logging

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
