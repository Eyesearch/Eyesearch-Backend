"""Refuse what we must not index.

A search index that stores and serves pirated films and cracked software is a
liability, not a product. The check has to run before storage, because deleting
afterwards means the content was served at least once.

The rules are structural — a magnet link, a torrent tracker path, a warez
pattern — not a list of site names. A name list goes stale the week it is
written, and the sites that matter most are the ones nobody has listed yet.
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Path fragments that only appear on distribution sites.
PIRACY_PATHS = (
    "/torrent/", "/torrents/", "/magnet", "/warez/", "/crack/", "/keygen",
    "/serial-key", "/nulled/", "/full-version", "/free-download-crack",
    "/watch-free", "/download-movie", "/hdrip", "/camrip", "/dvdrip",
)

#: File extensions that mean a media dump rather than a page.
MEDIA_EXTENSIONS = (".torrent", ".magnet", ".mkv", ".avi", ".iso", ".rar")

#: Text that appears on a piracy page and almost nowhere else.
PIRACY_PHRASES = (
    "magnet link", "download torrent", "full movie download", "cracked version",
    "activation key free", "serial number free", "watch online free hd",
    "nulled script", "premium account free",
)

_SAFE_SCHEMES = frozenset({"http", "https"})


def piracy_signal(url: str = "", title: str = "", body_text: str = "") -> str:
    """Why a page looks like illegal distribution, or "" when it does not."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return ""

    scheme = (parts.scheme or "").lower()
    if scheme and scheme not in _SAFE_SCHEMES:
        return f"scheme:{scheme}"

    path = (parts.path or "").lower()
    for marker in PIRACY_PATHS:
        if marker in path:
            return f"path:{marker}"
    for extension in MEDIA_EXTENSIONS:
        if path.endswith(extension):
            return f"file:{extension}"

    haystack = f"{title} {body_text}".lower()
    for phrase in PIRACY_PHRASES:
        if phrase in haystack:
            return f"phrase:{phrase}"

    return ""


class CopyrightLegalAgent:
    """Drop piracy before it reaches storage."""

    def verdict(self, url: str = "", title: str = "", body_text: str = "") -> str:
        return piracy_signal(url, title, body_text)

    def allowed(self, url: str = "", title: str = "", body_text: str = "") -> bool:
        return piracy_signal(url, title, body_text) == ""

    def screen(self, candidates) -> tuple[list[str], dict[str, str]]:
        """Split candidates into allowed and refused, with the reason."""
        allowed: list[str] = []
        refused: dict[str, str] = {}
        for url in candidates:
            reason = piracy_signal(url)
            if reason:
                refused[url] = reason
            else:
                allowed.append(url)
        return allowed, refused
