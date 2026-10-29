"""Breadcrumbs as structure, not as a string.

`clean_url_for` in the brand agent already renders a breadcrumb for a card:
"karnataka.gov.in › health › notifications". A card wants that string. A client
building its own UI wants the parts, and a date segment is noise in both.

This returns the parts. It reuses the same noise list rather than keeping a
second one, so the two cannot disagree about which segment to skip.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from app.agents import load_agent

_brand = load_agent("10_brand_canonical_agent")
NOISE_SEGMENTS = _brand.NOISE_SEGMENTS

#: A pure year is navigation, not a subject.
_YEAR = re.compile(r"^(19|20)\d{2}$")


@dataclass
class Breadcrumb:
    host: str
    segments: list[str]

    def as_list(self) -> list[str]:
        return list(self.segments)

    def as_string(self) -> str:
        return " › ".join([self.host, *self.segments]) if self.segments else self.host


class BreadcrumbBuilderAgent:
    """Turn a URL path into the words a person would use for it."""

    def __init__(self, max_segments: int = 4) -> None:
        self.max_segments = max(1, int(max_segments))

    def build(self, url: str) -> Breadcrumb:
        try:
            parts = urlsplit(url or "")
        except ValueError:
            return Breadcrumb(host="", segments=[])

        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]

        segments: list[str] = []
        for raw in parts.path.split("/"):
            if not raw:
                continue
            text = unquote(raw)
            text = re.sub(r"\.(html?|php|aspx?|jsp|txt|pdf)$", "", text, flags=re.I)
            text = re.sub(r"\s+", " ", text.replace("-", " ").replace("_", " ")).strip()
            if not text or text.lower() in NOISE_SEGMENTS or _YEAR.match(text):
                continue
            if len(text) > 40:
                text = text[:39].rstrip() + "…"
            segments.append(text[:1].upper() + text[1:] if text.islower() else text)
            if len(segments) >= self.max_segments:
                break

        return Breadcrumb(host=host, segments=segments)
