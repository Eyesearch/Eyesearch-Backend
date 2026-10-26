"""The category-seed crawler — our own fleet, not a third-party snippet dump.

Seeds are organised by the category a page belongs to, and that category travels
with the document all the way into storage:

    government  india.gov.in, karnataka.gov.in, .nic.in portals, KSRTC
    sports      iplt20.com, bcci.tv, prokabaddi.com, espncricinfo.com
    software    github.com, python.org, developer.mozilla.org, nodejs.org, …
    knowledge   main-namespace Wikipedia articles

Depth is root + one level (Wikipedia articles excepted), pages are fetched with
robots.txt respected and a byte cap, and every document is stored with the same
1–2 KB budget as everything else via `INSERT OR REPLACE`.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field

import httpx

from app.crawler.crawler import RobotsCache, fetch_page
from app.engine.config import EngineSettings, get_engine_settings
from app.engine.store import get_store
from app.engine.text import ExtractedDocument, extract_document, extract_links
from app.engine.urls import is_indexable, normalise_url

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CategorySeed:
    url: str
    label: str
    category: str
    # Restrict link-following to these path prefixes (empty = the whole host).
    paths: tuple[str, ...] = ()


CATEGORY_SEEDS: tuple[CategorySeed, ...] = (
    # --- government and public services ---------------------------------
    CategorySeed("https://www.india.gov.in", "National Portal of India", "government"),
    CategorySeed("https://www.karnataka.gov.in", "Government of Karnataka", "government"),
    CategorySeed("https://www.bengaluruurban.nic.in", "Bengaluru Urban district", "government"),
    CategorySeed("https://www.ksrtc.in", "KSRTC", "government"),
    CategorySeed("https://english.bmrc.co.in", "Namma Metro (BMRC)", "government"),
    # --- sports ----------------------------------------------------------
    CategorySeed("https://www.iplt20.com", "Indian Premier League", "sports"),
    CategorySeed("https://www.bcci.tv", "BCCI", "sports"),
    CategorySeed("https://www.prokabaddi.com", "Pro Kabaddi League", "sports"),
    CategorySeed("https://www.espncricinfo.com", "ESPNcricinfo", "sports"),
    # --- software and technology ----------------------------------------
    CategorySeed("https://github.com", "GitHub", "software"),
    CategorySeed("https://www.python.org", "Python", "software"),
    CategorySeed("https://developer.mozilla.org", "MDN Web Docs", "software"),
    CategorySeed("https://nodejs.org", "Node.js", "software"),
    CategorySeed("https://kotlinlang.org", "Kotlin", "software"),
    # --- knowledge --------------------------------------------------------
    CategorySeed("https://en.wikipedia.org/wiki/India", "Wikipedia — India", "knowledge"),
    CategorySeed("https://en.wikipedia.org/wiki/Karnataka", "Wikipedia — Karnataka", "knowledge"),
    CategorySeed("https://en.wikipedia.org/wiki/Bengaluru", "Wikipedia — Bengaluru", "knowledge"),
    CategorySeed("https://en.wikipedia.org/wiki/Indian_Space_Research_Organisation", "Wikipedia — ISRO", "knowledge"),
)

CATEGORIES: tuple[str, ...] = ("government", "sports", "software", "knowledge")


@dataclass
class SeedReport:
    category: str
    fetched: int = 0
    extracted: int = 0
    stored: int = 0
    skipped: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "fetched": self.fetched,
            "extracted": self.extracted,
            "stored": self.stored,
            "skipped": len(self.skipped),
        }


def seeds_for(categories: list[str] | None = None) -> list[CategorySeed]:
    wanted = set(categories or CATEGORIES)
    return [seed for seed in CATEGORY_SEEDS if seed.category in wanted]


async def crawl_seed(
    seed: CategorySeed,
    *,
    client: httpx.AsyncClient,
    settings: EngineSettings,
    robots: RobotsCache,
    max_pages: int | None = None,
) -> tuple[SeedReport, list[ExtractedDocument]]:
    """Crawl one seed: its entry page, then same-host level-1 links."""
    report = SeedReport(category=seed.category)
    documents: list[ExtractedDocument] = []
    budget = max_pages or settings.seed_crawl_pages_per_domain

    queue: list[str] = [normalise_url(seed.url)]
    seen: set[str] = set()

    while queue and len(documents) < budget:
        url = queue.pop(0)
        if url in seen or not is_indexable(url):
            continue
        seen.add(url)

        if settings.respect_robots and not await robots.allowed(url):
            report.skipped.append(url)
            continue

        fetched = await fetch_page(client, url, settings)
        report.fetched += 1
        if fetched is None:
            report.skipped.append(url)
            continue

        html, status = fetched
        document = extract_document(url, html, http_status=status, category=seed.category)
        if document is None:
            report.skipped.append(url)
            continue

        documents.append(document)
        report.extracted += 1

        # Level-1 only: follow links from the entry page, never from its children.
        if len(seen) == 1:
            for link in list(extract_links(html, url))[: budget * 2]:
                if link not in seen:
                    queue.append(link)

        await asyncio.sleep(settings.crawl_delay_seconds)

    return report, documents


async def run_seed_pass(
    categories: list[str] | None = None,
    *,
    settings: EngineSettings | None = None,
    store: object | None = None,
) -> dict[str, object]:
    """Crawl every seed in the requested categories and store what comes back."""
    cfg = settings or get_engine_settings()
    index = store or get_store()
    report: dict[str, object] = {"categories": {}, "documents": 0, "stored": 0, "fetched": 0}

    if not cfg.seed_crawl_enabled or not cfg.crawl_enabled:
        logger.info("seed crawl disabled")
        return report

    seeds = seeds_for(categories)
    if not seeds:
        return report

    async with httpx.AsyncClient(follow_redirects=True) as client:
        robots = RobotsCache(client, cfg.crawl_user_agent)
        for seed in seeds:
            try:
                seed_report, documents = await crawl_seed(
                    seed, client=client, settings=cfg, robots=robots
                )
            except Exception as exc:  # one dead domain must not stop the pass
                logger.warning("seed %s failed: %s", seed.url, exc)
                continue

            if documents:
                written = await index.replace_documents([document.as_row() for document in documents])
                seed_report.stored = int(written.get("pages", 0))
                report["documents"] = int(report["documents"]) + len(documents)
                report["stored"] = int(report["stored"]) + seed_report.stored

            report["fetched"] = int(report["fetched"]) + seed_report.fetched
            report["categories"][seed.label] = seed_report.as_dict()
            logger.info("seed %s (%s): %s", seed.url, seed.category, seed_report.as_dict())

    logger.info(
        "seed pass complete: %d documents, %d stored, %d fetched",
        report["documents"],
        report["stored"],
        report["fetched"],
    )
    return report


async def _main() -> None:
    parser = argparse.ArgumentParser(description="EyeSearch category-seed crawler")
    parser.add_argument("--category", action="append", choices=list(CATEGORIES))
    parser.add_argument("--list", action="store_true", help="print the seed list and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    if args.list:
        for seed in seeds_for(args.category):
            print(f"{seed.category:12} {seed.url:52} {seed.label}")
        return

    from app import database
    from app.config import get_settings

    settings = get_settings()
    await database.connect(settings)
    try:
        await database.initialize_schema()
        print(await run_seed_pass(args.category))
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = [
    "CATEGORY_SEEDS",
    "CATEGORIES",
    "CategorySeed",
    "SeedReport",
    "seeds_for",
    "crawl_seed",
    "run_seed_pass",
]
