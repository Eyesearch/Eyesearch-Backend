"""URL parsing that treats its input as data.

Every helper here returns `None` instead of raising, because the URLs these
functions receive come from web pages: a href, a sitemap `<loc>`, a redirect
target. A single malformed one must cost that one URL, never the batch.

Three separate production crashes led to this module, all the same shape — the
stdlib parser raising where the caller assumed it would not:

* `urlparse('http://[::1/ path')` raises `ValueError: Invalid IPv6 URL`.
* `parts.port` raises `ValueError: Port could not be cast to integer value as
  '\\xa0https:'` — lazily, on attribute access, long after the parse returned.
* `urljoin(base, 'http://[::1/x')` raises the same IPv6 `ValueError`.

Keeping them in one place means the next call site is guarded by construction
rather than by discovering it the same way.
"""

from __future__ import annotations

from urllib.parse import (
    ParseResult,
    SplitResult,
    urljoin,
    urlparse,
    urlsplit,
)

__all__ = [
    "safe_port",
    "safe_urljoin",
    "safe_urlparse",
    "safe_urlsplit",
]


def safe_urlparse(url: str) -> ParseResult | None:
    """`urlparse`, or None when the authority cannot be parsed."""
    if not isinstance(url, str):
        return None
    try:
        return urlparse(url)
    except (ValueError, UnicodeError):
        return None


def safe_urlsplit(url: str) -> SplitResult | None:
    """`urlsplit`, or None when the authority cannot be parsed."""
    if not isinstance(url, str):
        return None
    try:
        return urlsplit(url)
    except (ValueError, UnicodeError):
        return None


def safe_urljoin(base: str, href: str) -> str | None:
    """`urljoin`, or None when either side cannot be resolved.

    The common failure is an unclosed IPv6 literal in the href — a truncated
    `http://[::1/` — which the stdlib rejects at join time.
    """
    if not isinstance(base, str) or not isinstance(href, str):
        return None
    try:
        return urljoin(base, href)
    except (ValueError, UnicodeError):
        return None


def safe_port(parts: ParseResult | SplitResult | None) -> int | None:
    """The port from a parse result, or None when it is absent or invalid.

    `parts.port` is a property that parses on access, so it raises for a dirty
    authority such as `example.com:\\xa0https:` even though the parse succeeded.
    """
    if parts is None:
        return None
    try:
        return parts.port
    except (ValueError, UnicodeError):
        return None
