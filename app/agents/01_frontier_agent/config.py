"""Frontier agent configuration: which URLs are worth crawling."""

from __future__ import annotations

from dataclasses import dataclass, field

# Domains that are always worth a fetch, whatever the queue pressure.
AUTHORITATIVE_SUFFIXES: tuple[str, ...] = (
    ".gov.in", ".nic.in", ".ac.in", ".res.in", ".edu.in",
)

# Never queued, however they were discovered.
EXCLUDED_HOST_FRAGMENTS: tuple[str, ...] = (
    "facebook.", "instagram.", "twitter.", "x.com", "linkedin.", "youtube.",
    "pinterest.", "tiktok.", "reddit.", "quora.", "google.", "doubleclick.",
    "google-analytics.", "googletagmanager.", "taboola.", "outbrain.",
)


@dataclass(frozen=True)
class FrontierConfig:
    """Prioritisation and queue limits for one crawl frontier."""

    max_sitelinks_per_domain: int = 5
    max_pages_per_domain: int = 5
    batch_size: int = 10
    pause_seconds: float = 0.5
    authority_boost: float = 3.0
    suffixes: tuple[str, ...] = field(default=AUTHORITATIVE_SUFFIXES)

    def is_authoritative(self, host: str) -> bool:
        value = (host or "").lower().removeprefix("www.")
        return any(value == suffix.lstrip(".") or value.endswith(suffix) for suffix in self.suffixes)

    def is_excluded(self, host: str) -> bool:
        value = (host or "").lower()
        return any(fragment in value for fragment in EXCLUDED_HOST_FRAGMENTS)


__all__ = ["FrontierConfig", "AUTHORITATIVE_SUFFIXES", "EXCLUDED_HOST_FRAGMENTS"]
