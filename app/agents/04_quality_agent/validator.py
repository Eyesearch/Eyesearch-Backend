"""The quality rules, in one place."""

from __future__ import annotations

import hashlib

from app.engine.text import JUNK_MARKERS, MIN_BODY_CHARS

# Kept for callers that import the thresholds from the agent layer.
MIN_DENSITY = MIN_BODY_CHARS
JUNK = JUNK_MARKERS


def content_hash(title: str, body: str = "", url: str = "") -> str:
    """A stable de-duplication hash for a page."""
    return hashlib.sha256("\n".join((url, title, body)).encode("utf-8")).hexdigest()[:32]


def is_junk(title: str, description: str = "") -> bool:
    haystack = f"{title} {description}".lower()
    return any(marker in haystack for marker in JUNK)


__all__ = ["MIN_DENSITY", "JUNK", "content_hash", "is_junk"]
