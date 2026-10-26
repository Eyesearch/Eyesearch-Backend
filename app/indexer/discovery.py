"""Autonomous Indian domain discovery.

When a crawled page links out to another Indian authoritative host, that host
joins the backlog and gets crawled later. The filters are deliberately narrow:
a domain has to look like an Indian public or academic entity, and the usual
social networks, ad networks, CDNs and login traps are rejected outright.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.engine.urls import normalise_url

logger = logging.getLogger(__name__)

# Suffixes that mark an Indian authoritative target.
INDIAN_SUFFIXES: tuple[str, ...] = (
    ".gov.in", ".nic.in", ".ac.in", ".res.in", ".edu.in", ".org.in",
    ".co.in", ".net.in", ".gen.in", ".firm.in", ".ind.in", ".gov", ".in",
)

# Hosts that never belong in the index: social, ad networks, CDNs, trackers,
# login redirectors and shorteners.
BLOCKED_FRAGMENTS: tuple[str, ...] = (
    "facebook.", "fb.com", "instagram.", "twitter.", "x.com", "linkedin.",
    "youtube.", "youtu.be", "whatsapp.", "telegram.", "t.me", "pinterest.",
    "tiktok.", "snapchat.", "reddit.", "quora.",
    "google.", "googleapis.", "gstatic.", "googletagmanager.", "doubleclick.",
    "google-analytics.", "adservice.", "adsystem.", "adnxs.", "taboola.",
    "outbrain.", "criteo.", "scorecardresearch.", "quantserve.", "moatads.",
    "cloudflare.", "akamai", "cloudfront.", "fastly.", "jsdelivr.", "unpkg.",
    "amazonaws.", "azureedge.", "cdn.", "cdnjs.", "bootstrapcdn.",
    "bit.ly", "tinyurl.", "t.co", "ow.ly", "goo.gl", "is.gd",
    "login.", "signin.", "accounts.", "auth.", "sso.", "oauth.",
    "apple.com", "microsoft.com", "adobe.com", "oracle.com", "salesforce.com",
    "wordpress.com", "blogspot.", "medium.com", "substack.com",
)

# Paths that are redirect traps rather than content.
BLOCKED_PATH_MARKERS: tuple[str, ...] = (
    "/login", "/signin", "/sign-in", "/signup", "/register", "/auth/", "/sso/",
    "/cart", "/checkout", "/logout", "/wp-admin", "/wp-login",
)


def _host_of(url: str) -> str:
    return urlparse(normalise_url(url)).netloc.lower()


def is_blocked_host(host: str) -> bool:
    return any(fragment in host for fragment in BLOCKED_FRAGMENTS)


def is_indian_authoritative(host: str) -> bool:
    """True for a host that looks like an Indian public/academic authority."""
    if not host or "." not in host:
        return False
    if is_blocked_host(host):
        return False
    # an IP literal is never an authoritative Indian domain
    if host.replace(".", "").isdigit():
        return False
    return any(host == suffix.lstrip(".") or host.endswith(suffix) for suffix in INDIAN_SUFFIXES)


def discover_domains(
    html: str,
    base_url: str,
    *,
    limit: int = 20,
) -> list[str]:
    """Indian authoritative hosts linked from a page, external ones only."""
    if not html:
        return []

    own_host = _host_of(base_url)
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    seen: set[str] = {own_host}

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            target = normalise_url(href if href.startswith("http") else f"https://{own_host}{href}" if href.startswith("/") else href)
        except Exception:
            continue

        parsed = urlparse(target)
        host = parsed.netloc.lower()
        if not host or host in seen:
            continue
        # Same site means the same host or a parent/child of it — never another
        # entity that merely shares a suffix like `.gov.in`.
        if host == own_host or host.endswith(f".{own_host}") or own_host.endswith(f".{host}"):
            continue
        if any(marker in parsed.path.lower() for marker in BLOCKED_PATH_MARKERS):
            continue
        if not is_indian_authoritative(host):
            continue

        seen.add(host)
        found.append(host)
        if len(found) >= limit:
            break

    return found


__all__ = [
    "INDIAN_SUFFIXES",
    "BLOCKED_FRAGMENTS",
    "is_indian_authoritative",
    "is_blocked_host",
    "discover_domains",
]
