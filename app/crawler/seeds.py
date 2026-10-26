"""Seed domains: Indian government, regional news and reference sites.

Chosen for being (a) Indian or regional, (b) largely static, and (c) useful to
answer real queries without a general crawl. Extend by adding entries; the
crawler treats every entry the same way.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Seed:
    url: str
    label: str
    # Restrict the crawl to these path prefixes (empty = whole host).
    paths: tuple[str, ...] = ()


SEED_DOMAINS: tuple[Seed, ...] = (
    Seed("https://www.india.gov.in", "National Portal of India", ("/topics", "/spotlight")),
    Seed("https://www.isro.gov.in", "ISRO", ("/",)),
    Seed("https://www.meity.gov.in", "MeitY", ("/",)),
    Seed("https://english.bmrc.co.in", "Namma Metro (BMRC)", ("/",)),
    Seed("https://www.irctc.co.in", "IRCTC", ("/",)),
    Seed("https://kn.wikipedia.org/wiki/%E0%B2%AD%E0%B2%BE%E0%B2%B0%E0%B2%A4", "Kannada Wikipedia — India"),
    Seed("https://en.wikipedia.org/wiki/Karnataka", "Wikipedia — Karnataka"),
    Seed("https://www.thehindu.com/news/national/karnataka/", "The Hindu — Karnataka"),
    Seed("https://www.prajavani.net", "Prajavani"),
    Seed("https://www.deccanherald.com/india/karnataka", "Deccan Herald — Karnataka"),
    Seed("https://www.bengaluruurban.nic.in", "Bengaluru Urban district"),
    Seed("https://www.karnataka.gov.in", "Government of Karnataka"),
)

# Hosts that must never be crawled, whatever a link says.
BLOCKED_HOSTS: tuple[str, ...] = (
    "localhost",
    "127.0.0.1",
    "0.0.0.0",
    "169.254.169.254",  # link-local / IMDS
)

__all__ = ["Seed", "SEED_DOMAINS", "BLOCKED_HOSTS"]
