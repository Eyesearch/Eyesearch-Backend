"""Favicons, discovered rather than guessed.

The search service builds an icon URL as `https://host/favicon.ico`, which is a
guess: plenty of sites serve their icon from a hashed asset path and return 404
at the root, so the badge breaks. The page itself says where the icon is, in a
`<link rel="icon">`, and that is what this reads.

It also keeps the local cache honest: a path is only reported when the file is
actually there, so a broken badge never reaches a card.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urljoin, urlsplit

logger = logging.getLogger(__name__)

#: `rel` values that point at an icon, best first. Apple's touch icon is usually
#: the largest, so it is preferred when a page offers both.
ICON_RELS = ("apple-touch-icon", "icon", "shortcut icon", "mask-icon")

#: Extensions a browser will render in an <img>.
IMAGE_EXTENSIONS = (".ico", ".png", ".svg", ".jpg", ".jpeg", ".webp", ".gif")


def icon_from_html(soup, page_url: str) -> str:
    """The page's declared icon, made absolute. Empty when it declares none."""
    best = ""
    for rel in ICON_RELS:
        for link in soup.find_all("link"):
            values = [str(value).lower() for value in (link.get("rel") or [])]
            if rel not in values:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            absolute = urljoin(page_url, href)
            if any(absolute.lower().split("?")[0].endswith(ext) for ext in IMAGE_EXTENSIONS):
                return absolute
            best = best or absolute
    return best


def fallback_icon(url: str) -> str:
    """`https://host/favicon.ico`, which is where most sites still serve it."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return f"https://{host}/favicon.ico" if host else ""


def cached_icon_name(url: str) -> str:
    """A stable local filename for a page's host, safe on a filesystem."""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    return re.sub(r"[^a-z0-9._-]+", "_", host) + ".icon"


class VisualHeraldAgent:
    """Discover a page's icon and report only paths that exist."""

    def __init__(self, cache_dir: str = "") -> None:
        self._cache_dir = cache_dir

    def discover(self, soup, page_url: str) -> str:
        return icon_from_html(soup, page_url) or fallback_icon(page_url)

    def cached_path(self, url: str) -> str:
        """The local cache file for a host, or "" when it is not there.

        Reporting a path that does not exist is how a card ends up with a broken
        image, so the check is the point of this method.
        """
        if not self._cache_dir:
            return ""
        name = cached_icon_name(url)
        if not name:
            return ""
        path = os.path.join(self._cache_dir, name)
        return path if os.path.isfile(path) else ""
