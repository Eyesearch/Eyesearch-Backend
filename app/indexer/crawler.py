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
from app.database import get_client
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
    # --- banking and finance ---------------------------------------------
    CategorySeed("https://www.rbi.org.in", "Reserve Bank of India", "banking"),
    CategorySeed("https://www.sbi.co.in", "State Bank of India", "banking"),
    CategorySeed("https://www.hdfcbank.com", "HDFC Bank", "banking"),
    CategorySeed("https://www.icicibank.com", "ICICI Bank", "banking"),
    CategorySeed("https://www.npci.org.in", "NPCI (UPI)", "banking"),
    # --- e-commerce -------------------------------------------------------
    CategorySeed("https://www.flipkart.com", "Flipkart", "ecommerce"),
    CategorySeed("https://www.myntra.com", "Myntra", "ecommerce"),
    CategorySeed("https://www.nykaa.com", "Nykaa", "ecommerce"),
    CategorySeed("https://www.irctc.co.in", "IRCTC", "ecommerce"),
    CategorySeed("https://www.amazon.in", "Amazon India", "ecommerce"),
    # --- education --------------------------------------------------------
    CategorySeed("https://www.ugc.gov.in", "UGC", "education"),
    CategorySeed("https://www.iitb.ac.in", "IIT Bombay", "education"),
    CategorySeed("https://www.iisc.ac.in", "IISc Bengaluru", "education"),
    CategorySeed("https://www.nta.ac.in", "National Testing Agency", "education"),
    CategorySeed("https://www.aicte-india.org", "AICTE", "education"),
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

CATEGORIES: tuple[str, ...] = (
    "government", "banking", "sports", "ecommerce", "education", "software", "knowledge",
)

# The default seed run: the five India-first categories from the brief.
DEFAULT_CATEGORIES: tuple[str, ...] = ("government", "banking", "sports", "ecommerce", "education")


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
    wanted = set(categories or DEFAULT_CATEGORIES)
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
    report: dict[str, object] = {
        "categories": {}, "documents": 0, "stored": 0, "fetched": 0, "sites": 0, "sitelinks": 0
    }

    if not cfg.seed_crawl_enabled or not cfg.crawl_enabled:
        logger.info("seed crawl disabled")
        return report

    seeds = seeds_for(categories)
    if not seeds:
        return report

    # A single shared HTTP client and a single shared database client: the
    # concurrency limit protects both the target sites and the Turso connection.
    semaphore = asyncio.Semaphore(max(1, cfg.seed_crawl_concurrency))

    async with httpx.AsyncClient(follow_redirects=True) as client:
        robots = RobotsCache(client, cfg.crawl_user_agent)

        async def run(seed: CategorySeed) -> tuple[CategorySeed, SeedReport, list]:
            async with semaphore:
                try:
                    seed_report, documents = await crawl_seed(
                        seed, client=client, settings=cfg, robots=robots
                    )
                    return seed, seed_report, documents
                except Exception as exc:  # one dead domain must not stop the pass
                    logger.warning("seed %s failed: %s", seed.url, exc)
                    return seed, SeedReport(category=seed.category), []

        for seed, seed_report, documents in await asyncio.gather(*(run(seed) for seed in seeds)):
            if documents:
                written = await index.replace_documents([document.as_row() for document in documents])
                seed_report.stored = int(written.get("pages", 0))
                report["documents"] = int(report["documents"]) + len(documents)
                report["stored"] = int(report["stored"]) + seed_report.stored
                report["sitelinks"] = int(report.get("sitelinks", 0)) + int(written.get("sitelinks", 0))
                report["sites"] = int(report.get("sites", 0)) + int(written.get("sites", 0))

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


async def coverage(store: object | None = None) -> dict[str, object]:
    """How much of the seed list the index already covers."""
    index = store or get_store()
    settings = get_engine_settings()
    sites = await index.count_documents()
    seeds = seeds_for(None)
    domains = {seed.url.split("/")[2].removeprefix("www.") for seed in seeds}
    covered = 0
    client = get_client()
    for domain in domains:
        try:
            row = await client.execute("SELECT 1 FROM sites WHERE domain = ? LIMIT 1", [domain])
            if getattr(row, "rows", ()):
                covered += 1
        except Exception as exc:
            logger.debug("coverage probe failed for %s: %s", domain, exc)

    return {
        "sites": sites,
        "seed_domains": len(domains),
        "covered": covered,
        "threshold": settings.seed_coverage_min_sites,
        "sufficient": sites >= settings.seed_coverage_min_sites,
    }


async def run_autonomous_seed_crawl(store: object | None = None) -> dict[str, object]:
    """The lifecycle's own crawl: never raises, always logs what it did.

    Called from the FastAPI lifespan as a background task, so the server binds
    its port and answers /health while this runs.
    """
    index = store or get_store()
    try:
        state = await coverage(index)
    except Exception as exc:
        logger.warning("coverage check failed, crawling anyway: %s", exc)
        state = {"sufficient": False, "sites": 0, "covered": 0, "seed_domains": 0}

    if state.get("sufficient"):
        logger.info(
            "autonomous crawl skipped: %s sites already cover the seeds", state.get("sites")
        )
        return {"skipped": True, **state}

    logger.info(
        "autonomous crawl starting: %s sites, %s/%s seed domains covered (threshold %s)",
        state.get("sites"), state.get("covered"), state.get("seed_domains"), state.get("threshold"),
    )
    try:
        report = await run_seed_pass()
    except Exception as exc:  # a background task must never take the app down
        logger.warning("autonomous crawl failed: %s", exc)
        return {"skipped": False, "error": str(exc), **state}

    try:
        after = await coverage(index)
    except Exception:
        after = {}
    logger.info(
        "autonomous crawl done: %s documents, %s pages stored, now %s sites / %s seed domains covered",
        report.get("documents"), report.get("stored"), after.get("sites"), after.get("covered"),
    )
    return {"skipped": False, **report, "coverage_after": after}


async def _main() -> None:
    parser = argparse.ArgumentParser(description="EyeSearch category-seed crawler")
    parser.add_argument("--category", action="append", choices=list(CATEGORIES))
    parser.add_argument("--list", action="store_true", help="print the seed list and exit")
    parser.add_argument("--verify", action="store_true", help="print index and search verification")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s :: %(message)s")

    if args.list:
        for seed in seeds_for(args.category):
            print(f"{seed.category:12} {seed.url:52} {seed.label}")
        return

    from dataclasses import replace

    from app import database
    from app.config import get_settings
    from app.engine.config import get_engine_settings

    settings = get_settings()
    # An explicit CLI run is always allowed: SEED_CRAWL_ENABLED gates the
    # background scheduler, not the operator asking for a crawl.
    engine_settings = replace(get_engine_settings(), seed_crawl_enabled=True)
    await database.connect(settings)
    try:
        await database.initialize_schema()
        if args.verify:
            await verify_index()
        else:
            print(await run_seed_pass(args.category, settings=engine_settings))
    finally:
        await database.close()


async def verify_index(sample_queries: tuple[str, ...] | None = None) -> dict[str, object]:
    """Report what the crawl produced and prove it is searchable."""
    client = get_client()
    totals = await client.execute(
        "SELECT (SELECT COUNT(*) FROM sites), (SELECT COUNT(*) FROM pages), "
        "(SELECT COUNT(*) FROM pages WHERE kind = 'root'), "
        "(SELECT COUNT(*) FROM pages WHERE kind = 'sitelink'), "
        "(SELECT COUNT(*) FROM page_fts)"
    )
    rows = getattr(totals, "rows", ()) or ()
    sites, pages, roots, sitelinks, indexed = (int(value) for value in rows[0]) if rows else (0, 0, 0, 0, 0)

    per_category = await client.execute(
        "SELECT category, COUNT(*) FROM sites GROUP BY category ORDER BY COUNT(*) DESC"
    )
    categories = {str(row[0]): int(row[1]) for row in (getattr(per_category, "rows", ()) or ())}

    logger.info(
        "verify: %d sites, %d pages (%d root + %d sitelink), %d indexed",
        sites, pages, roots, sitelinks, indexed,
    )
    print(
        f"SITES {sites} | PAGES {pages} ({roots} root + {sitelinks} sitelink) | FTS {indexed} | "
        f"categories {categories}"
    )

    queries = sample_queries or ("india", "bank", "ipl", "metro", "karnataka")
    store = get_store()
    for query in queries:
        hits = await store.search_local(query, limit=3)
        top = hits[0] if hits else None
        print(
            f"SEARCH {query!r:14} hits={len(hits)}"
            + (f" -> {top.title[:48]!r} ({top.url}) sitelinks={len(top.sections)}" if top else " -> none")
        )

    return {
        "sites": sites, "pages": pages, "roots": roots, "sitelinks": sitelinks,
        "indexed": indexed, "categories": categories,
    }


if __name__ == "__main__":
    asyncio.run(_main())


__all__ = [
    "coverage",
    "run_autonomous_seed_crawl",
    "DEFAULT_CATEGORIES",
    "verify_index",
    "CATEGORY_SEEDS",
    "CATEGORIES",
    "CategorySeed",
    "SeedReport",
    "seeds_for",
    "crawl_seed",
    "run_seed_pass",
]
