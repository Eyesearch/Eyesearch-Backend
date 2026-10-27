"""Realistic browser header profiles for the fetcher.

A request that looks like a bot gets a 403, a 202 challenge or an empty body from
most Indian government gateways. These are the headers a current desktop Chrome
and Safari actually send, including the `sec-ch-ua` client hints, and the agent
rotates between them per attempt so one blocked fingerprint does not sink a pass.
"""

from __future__ import annotations

from itertools import cycle
from typing import Iterator

from app.crawler.crawler import browser_headers

# Chrome 128 on Windows 10, and Safari 17 on macOS: two current, common profiles.
CHROME_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,"
        "image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

SAFARI_HEADERS: dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.4 Safari/605.1.15"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}

PROFILES: tuple[dict[str, str], ...] = (CHROME_HEADERS, SAFARI_HEADERS)

_rotation: Iterator[dict[str, str]] = cycle(PROFILES)


def next_profile() -> dict[str, str]:
    """The next header profile in the rotation."""
    return dict(next(_rotation))


def profile_for(attempt: int) -> dict[str, str]:
    """A stable profile per attempt, so a retry presents a different fingerprint."""
    return dict(PROFILES[attempt % len(PROFILES)])


def headers_for(attempt: int = 0, *, user_agent: str | None = None) -> dict[str, str]:
    """Headers for one attempt; `user_agent` overrides the profile's agent."""
    headers = profile_for(attempt)
    if user_agent:
        headers["User-Agent"] = user_agent
    return headers


__all__ = [
    "CHROME_HEADERS",
    "SAFARI_HEADERS",
    "PROFILES",
    "browser_headers",
    "headers_for",
    "next_profile",
    "profile_for",
]
