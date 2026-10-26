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
from urllib.parse import urlparse
from dataclasses import dataclass, field

import httpx

from app.crawler.crawler import RobotsCache, fetch_page, fetch_page_status
from app.indexer.discovery import discover_domains, is_indian_authoritative
from app.indexer.guard import storage_paused
from app.indexer.seed_registry import CATEGORY_ORDER, SEED_ROWS
from app.engine.config import EngineSettings, get_engine_settings
from app.database import execute_safe, get_client
from app.engine.store import get_store
from app.engine.text import (
    ExtractedDocument,
    extract_document_with_reason,
    extract_links,
    fallback_document,
)
from app.engine.urls import infer_category, is_authoritative_indian, is_indexable, normalise_url

logger = logging.getLogger(__name__)

# Wikipedia entry points for the knowledge category.
WIKI_ARTICLES: tuple[tuple[str, str], ...] = (
    ("India", "Wikipedia — India"),
    ("Karnataka", "Wikipedia — Karnataka"),
    ("Bengaluru", "Wikipedia — Bengaluru"),
    ("Indian_Space_Research_Organisation", "Wikipedia — ISRO"),
)


@dataclass(frozen=True)
class CategorySeed:
    url: str
    label: str
    category: str
    # Restrict link-following to these path prefixes (empty = the whole host).
    paths: tuple[str, ...] = ()


def _build_seeds() -> tuple[CategorySeed, ...]:
    """The registry as seeds, with Wikipedia articles as their entry-point paths."""
    seeds: list[CategorySeed] = []
    for host, label, category in SEED_ROWS:
        if category == "knowledge":
            continue  # articles are added below, not the bare host
        seeds.append(CategorySeed(f"https://www.{host}" if not host.startswith("www.") else f"https://{host}", label, category))

    for article, label in WIKI_ARTICLES:
        seeds.append(
            CategorySeed(f"https://en.wikipedia.org/wiki/{article}", label, "knowledge")
        )
    return tuple(seeds)


CATEGORY_SEEDS: tuple[CategorySeed, ...] = _build_seeds()

CATEGORIES: tuple[str, ...] = CATEGORY_ORDER

# The default seed run: every India-first category in the registry.
DEFAULT_CATEGORIES: tuple[str, ...] = (
    "government", "utilities", "education", "regulators", "services",
    "banking", "sports", "ecommerce",
)


@dataclass
class SeedReport:
    category: str
    fetched: int = 0
    extracted: int = 0
    stored: int = 0
    skipped: list[str] = field(default_factory=list)
    reasons: dict[str, int] = field(default_factory=dict)

    def skip(self, url: str, reason: str) -> None:
        self.skipped.append(url)
        self.reasons[reason] = self.reasons.get(reason, 0) + 1

    def as_dict(self) -> dict[str, object]:
        return {
            "category": self.category,
            "fetched": self.fetched,
            "extracted": self.extracted,
            "stored": self.stored,
            "skipped": len(self.skipped),
            "reasons": dict(self.reasons),
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
        logger.debug("seed page: %s", url)

        if settings.respect_robots and not await robots.allowed(url):
            report.skip(url, "robots_disallowed")
            continue

        html, status = await fetch_page_status(client, url, settings)
        report.fetched += 1
        host = urlparse(url).netloc or url
        logger.debug("fetched %s -> %s", host, status or "no response")

        if html is None:
            # A refused or unreachable domain still exists. Persist its root
            # rather than discarding the seed — only invalid syntax is skipped.
            reason = f"status_{status}" if status else "network_error"
            report.skip(url, reason)
            if url == normalise_url(seed.url) and is_authoritative_indian(url):
                document = fallback_document(
                    url,
                    label=seed.label,
                    category=seed.category,
                    http_status=status or 0,
                    use_hostname=True,
                )
                if document is not None:
                    logger.info(
                        "official portal refused us (%s) — keeping a root card for %s",
                        reason, _seed_domain(seed),
                    )
                    documents.append(document)
            continue
        document, reason = extract_document_with_reason(
            url, html, http_status=status or 200, category=seed.category
        )
        if document is None:
            report.skip(url, reason or "not_extracted")
            logger.debug("skipped: %s (%s)", reason or "not_extracted", url)
            # The seed's own entry page must persist even when extraction fails:
            # a root record from the hostname beats losing the domain entirely.
            if url == normalise_url(seed.url) and is_authoritative_indian(url):
                document = fallback_document(
                    url, label=seed.label, category=seed.category, http_status=status or 200
                )
                if document is not None:
                    logger.info(
                        "official portal had nothing extractable (%s) — keeping a root card for %s",
                        reason, _seed_domain(seed),
                    )
            if document is None:
                continue

        documents.append(document)
        report.extracted += 1

        # Indian authoritative domains linked from this page join the backlog.
        try:
            await enqueue_discovered(html, url)
        except Exception as exc:  # discovery must never break a crawl
            logger.debug("discovery skipped for %s: %s", url, exc)

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
    seeds: list[CategorySeed] | None = None,
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

    if await storage_paused(index, cfg):
        report["paused"] = True
        return report

    targets = seeds if seeds is not None else seeds_for(categories)
    if not targets:
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

        tasks = [asyncio.create_task(run(seed)) for seed in targets]

        # Persist each domain the moment it finishes. A long pass that waited for
        # every seed before writing lost all of its work when the instance
        # restarted mid-flight.
        for finished in asyncio.as_completed(tasks):
            seed, seed_report, documents = await finished
            if documents:
                try:
                    written = await index.replace_documents(
                        [document.as_row() for document in documents]
                    )
                    seed_report.stored = int(written.get("pages", 0))
                    report["documents"] = int(report["documents"]) + len(documents)
                    report["stored"] = int(report["stored"]) + seed_report.stored
                    report["sitelinks"] = int(report.get("sitelinks", 0)) + int(written.get("sitelinks", 0))
                    report["sites"] = int(report.get("sites", 0)) + int(written.get("sites", 0))
                    total = await index.count_documents()
                    logger.info(
                        "DB COMMIT SUCCESS: Indexed %s with %d pages (Running total: %d)",
                        _seed_domain(seed), seed_report.stored, total,
                    )
                except Exception as exc:
                    logger.error(
                        "index write failed for %s: %s: %s",
                        _seed_domain(seed), type(exc).__name__, exc,
                    )
            else:
                logger.info(
                    "seed %s produced no indexable pages (fetched %d, skipped %d%s)",
                    _seed_domain(seed), seed_report.fetched, len(seed_report.skipped),
                    ": " + ", ".join(f"{k}={v}" for k, v in seed_report.reasons.items())
                    if seed_report.reasons else "",
                )

            report["fetched"] = int(report["fetched"]) + seed_report.fetched
            report["categories"][seed.label] = seed_report.as_dict()
            logger.debug("seed %s (%s): %s", seed.url, seed.category, seed_report.as_dict())

    logger.info(
        "seed pass complete: %d documents, %d stored, %d fetched",
        report["documents"],
        report["stored"],
        report["fetched"],
    )
    return report


async def enqueue_discovered(html: str, base_url: str, store: object | None = None) -> int:
    """Queue Indian authoritative domains linked from a crawled page."""
    index = store or get_store()
    hosts = discover_domains(html, base_url)
    if not hosts:
        return 0
    added = 0
    for host in hosts:
        try:
            if await index.site_exists(host):
                continue
            if await index.enqueue_domain(host, reason="discovered"):
                added += 1
        except Exception as exc:
            logger.debug("could not queue %s: %s", host, exc)
    if added:
        logger.info("discovery: queued %d Indian domains from %s", added, base_url)
    return added


async def run_discovery_pass(
    *,
    batch_size: int | None = None,
    store: object | None = None,
    settings: EngineSettings | None = None,
) -> dict[str, object]:
    """Drain the discovered-domain backlog in bounded batches.

    Ten domains at a time, a polite pause between batches, and a hard ceiling on
    the number of indexed sites. Stops when the queue is empty.
    """
    cfg = settings or get_engine_settings()
    index = store or get_store()
    batch = max(1, batch_size or cfg.discovery_batch_size)
    report: dict[str, object] = {"batches": 0, "attempted": 0, "stored": 0, "skipped": 0, "ceiling": False}

    if await storage_paused(index, cfg):
        report["paused"] = True
        return report

    sites = await index.count_documents()
    if sites >= cfg.max_indexed_sites:
        logger.info("discovery pass idle: %d sites at the ceiling of %d", sites, cfg.max_indexed_sites)
        report["ceiling"] = True
        return report

    async with httpx.AsyncClient(follow_redirects=True) as client:
        robots = RobotsCache(client, cfg.crawl_user_agent)
        while sites < cfg.max_indexed_sites:
            hosts = await index.pending_domains(batch)
            if not hosts:
                break

            report["batches"] = int(report["batches"]) + 1
            for host in hosts:
                report["attempted"] = int(report["attempted"]) + 1
                seed = CategorySeed(f"https://{host}/", host, infer_category(f"https://{host}/"))
                try:
                    seed_report, documents = await crawl_seed(
                        seed, client=client, settings=cfg, robots=robots, max_pages=cfg.seed_crawl_pages_per_domain
                    )
                except Exception as exc:
                    logger.warning("discovered domain %s failed: %s", host, exc)
                    await index.mark_domain_processed(host, status="failed")
                    report["skipped"] = int(report["skipped"]) + 1
                    continue

                if documents:
                    written = await index.replace_documents([doc.as_row() for doc in documents])
                    report["stored"] = int(report["stored"]) + int(written.get("pages", 0))
                    sites = await index.count_documents()
                else:
                    report["skipped"] = int(report["skipped"]) + 1
                await index.mark_domain_processed(host, status="processed")

            logger.info(
                "discovery batch %s done: %s attempted, %s pages stored, %s sites",
                report["batches"], report["attempted"], report["stored"], sites,
            )
            if sites >= cfg.max_indexed_sites:
                report["ceiling"] = True
                break
            await asyncio.sleep(max(0.0, cfg.discovery_batch_pause_seconds))

    return report


def _seed_domain(seed: CategorySeed) -> str:
    return urlparse(seed.url).netloc.lower().removeprefix("www.")


async def coverage(store: object | None = None) -> dict[str, object]:
    """Which registry seeds are actually indexed — not merely how many sites exist.

    A site count says nothing about *which* domains are present: 61 old sites
    once made this report "sufficient" while none of the registry seeds were in
    the index. This compares the registry against `sites` directly, in a single
    query, and returns the uncovered seeds so the crawl can target them.
    """
    settings = get_engine_settings()
    seeds = seeds_for(None)
    wanted = {_seed_domain(seed): seed for seed in seeds}

    indexed: set[str] = set()
    try:
        result = await execute_safe("SELECT domain FROM sites")
        for row in getattr(result, "rows", ()) or ():
            indexed.add(str(row[0] or "").lower().removeprefix("www."))
    except Exception as exc:
        logger.warning("coverage read failed: %s", exc)

    covered = sorted(set(wanted) & indexed)
    uncovered = sorted(set(wanted) - indexed)
    ratio = (len(covered) / len(wanted)) if wanted else 1.0

    return {
        "sites": len(indexed),
        "seed_domains": len(wanted),
        "covered": len(covered),
        "uncovered": uncovered,
        "ratio": round(ratio, 4),
        "threshold": settings.seed_coverage_min_ratio,
        "sufficient": ratio >= settings.seed_coverage_min_ratio,
    }


async def run_autonomous_seed_crawl(store: object | None = None) -> dict[str, object]:
    """The lifecycle's own crawl: never raises, always logs what it did.

    Called from the FastAPI lifespan as a background task, so the server binds
    its port and answers /health while this runs.
    """
    index = store or get_store()
    if await storage_paused(index, get_engine_settings()):
        logger.warning("autonomous crawl paused: storage safety threshold reached")
        return {"skipped": True, "paused": True}

    try:
        state = await coverage(index)
    except Exception as exc:
        logger.warning("coverage check failed, crawling anyway: %s", exc)
        state = {"sufficient": False, "sites": 0, "covered": 0, "seed_domains": 0}

    if state.get("sufficient"):
        logger.info(
            "autonomous crawl skipped: %s/%s registry seeds indexed (%.1f%%)",
            state.get("covered"), state.get("seed_domains"), float(state.get("ratio") or 0) * 100,
        )
        return {"skipped": True, **state}

    uncovered = set(state.get("uncovered") or ())
    missing = [seed for seed in seeds_for(None) if _seed_domain(seed) in uncovered]
    logger.info(
        "autonomous crawl starting: %s/%s registry seeds indexed, %s missing",
        state.get("covered"), state.get("seed_domains"), len(missing),
    )
    try:
        report = await run_seed_pass(seeds=missing)
    except Exception as exc:  # a background task must never take the app down
        logger.warning("autonomous crawl failed: %s", exc)
        return {"skipped": False, "error": str(exc), **state}

    # Then drain whatever the crawl discovered: ten domains at a time, with a
    # polite pause between batches, until the queue is empty or the ceiling hits.
    try:
        discovery = await run_discovery_pass(store=index)
        report["discovery"] = discovery
    except Exception as exc:
        logger.warning("discovery pass failed: %s", exc)
        report["discovery"] = {"error": str(exc)}

    try:
        after = await coverage(index)
    except Exception:
        after = {}
    logger.info(
        "autonomous crawl done: %s documents, %s pages stored, now %s sites / %s seed domains covered",
        report.get("documents"), report.get("stored"), after.get("sites"), after.get("covered"),
    )
    return {"skipped": False, **state, **report, "coverage_after": after}


async def _main() -> None:
    parser = argparse.ArgumentParser(description="EyeSearch category-seed crawler")
    parser.add_argument("--category", action="append", choices=list(CATEGORIES))
    parser.add_argument("--list", action="store_true", help="print the seed list and exit")
    parser.add_argument("--verify", action="store_true", help="print index and search verification")
    parser.add_argument("--probe", action="store_true", help="check the database is reachable and writable")
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
        if args.probe:
            report = await database.verify_database()
            print(report)
            if not report.get("writable"):
                raise SystemExit(1)
        elif args.verify:
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
    "enqueue_discovered",
    "run_discovery_pass",
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
