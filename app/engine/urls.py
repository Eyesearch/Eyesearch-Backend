"""URL policy: normalisation, dedupe key, domain caps and crawl filtering.

Everything the crawler is allowed to index passes through `is_indexable`, and
everything stored is keyed by `normalise_url`. The rules exist to keep the index
small and answerable: entry portals, not application paths, and never two rows
for the same page.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.engine.config import get_engine_settings

# Query parameters that never identify a page.
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "gclid", "dclid", "fbclid", "msclkid", "yclid", "twclid", "igshid", "mc_cid",
    "mc_eid", "ref", "ref_src", "referrer", "source", "campaign", "spm", "_ga",
    "_gl", "share", "share_id", "s_cid", "cmpid", "ncid",
}

# Path fragments that mean "application", not "document".
BLOCKED_PATH_PATTERNS = (
    "/commit/", "/commits", "/pulls", "/pull/", "/issues", "/issue/", "/blob/",
    "/tree/", "/releases", "/tags", "/branches", "/compare", "/wiki/", "/actions",
    "/login", "/signin", "/signup", "/register", "/cart", "/checkout", "/account",
    "/search", "/print/", "/amp", "/feed", "/rss", "/wp-admin", "/wp-json",
)

# Pagination and dynamic-chain markers.
BLOCKED_QUERY_KEYS = {"page", "p", "paged", "offset", "start", "limit", "sort", "order", "filter", "q", "s"}

# File types we never index as documents.
BLOCKED_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".mp4", ".mp3",
    ".zip", ".gz", ".exe", ".dmg", ".css", ".js", ".json", ".xml", ".rss",
)

# Registrable-domain suffixes we must treat as one unit (in addition to the
# plain last-two-labels rule).
MULTI_LABEL_SUFFIXES = (
    "gov.in", "nic.in", "ac.in", "edu.in", "org.in", "co.in", "net.in", "res.in",
    "gov.uk", "co.uk", "org.uk", "com.au", "co.jp",
)


def normalise_url(url: str) -> str:
    """Canonical dedupe key: lowercase host, no tracking, no trailing slash, no hash."""
    candidate = (url or "").strip()
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = f"https:{candidate}"
    parsed = urlparse(candidate)
    if not parsed.scheme:
        parsed = urlparse(f"https://{candidate}")

    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]

    query_pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() not in TRACKING_PARAMS
    ]
    query_pairs.sort()
    path = parsed.path.rstrip("/") or "/"

    return urlunparse(("https", host, path, "", urlencode(query_pairs), ""))


def root_domain(url: str) -> str:
    """The registrable domain, e.g. `www.karnataka.gov.in/x` → `karnataka.gov.in`."""
    host = urlparse(normalise_url(url)).netloc
    if not host:
        return ""
    labels = host.split(".")
    for suffix in MULTI_LABEL_SUFFIXES:
        if host.endswith(f".{suffix}") or host == suffix:
            parts = suffix.count(".") + 1
            return ".".join(labels[-(parts + 1):]) if len(labels) > parts else host
    return ".".join(labels[-2:]) if len(labels) >= 2 else host


def path_depth(url: str) -> int:
    path = urlparse(normalise_url(url)).path
    return len([segment for segment in path.split("/") if segment])


def domain_class(url: str) -> str:
    """`news`, `government` or `static` — drives the revalidation cadence."""
    host = urlparse(normalise_url(url)).netloc
    if host.endswith((".gov.in", ".nic.in", ".gov", ".mil", ".ac.in")) or "ksrtc" in host:
        return "government"
    news_markers = ("news", "thehindu", "deccanherald", "prajavani", "timesofindia", "ndtv", "hindustantimes", "reuters", "bbc")
    if any(marker in host for marker in news_markers):
        return "news"
    return "static"


def is_indexable(url: str) -> bool:
    """Crawl/ingest gate: entry portals only, no app paths, no pagination chains."""
    settings = get_engine_settings()
    normalised = normalise_url(url)
    if not normalised.startswith("https://"):
        return False

    parsed = urlparse(normalised)
    if not parsed.netloc or "." not in parsed.netloc:
        return False

    lower_path = parsed.path.lower()
    if any(lower_path.endswith(ext) for ext in BLOCKED_EXTENSIONS):
        return False
    if any(pattern in lower_path for pattern in BLOCKED_PATH_PATTERNS):
        return False
    # Repository loops such as github.com/owner/repo/tree/... are already blocked
    # above; this also stops bare owner/repo pairs from multiplying.
    if parsed.netloc.endswith("github.com") and path_depth(normalised) > 1:
        return False

    if path_depth(normalised) > settings.max_path_depth:
        return False

    query_keys = {key.lower() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    if query_keys & BLOCKED_QUERY_KEYS:
        return False
    # Long dynamic chains are pagination by another name.
    if len(parse_qsl(parsed.query, keep_blank_values=True)) > 2:
        return False

    return True


__all__ = [
    "normalise_url",
    "root_domain",
    "path_depth",
    "domain_class",
    "is_indexable",
    "TRACKING_PARAMS",
    "BLOCKED_PATH_PATTERNS",
]
