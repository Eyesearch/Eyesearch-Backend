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
from bs4 import BeautifulSoup

from app.agents import FetcherAgent, IndexerAgent, ParserAgent, QualityAgent
from app.crawler.crawler import RobotsCache, fetch_page, fetch_page_status
from app.indexer.discovery import discover_domains, is_indian_authoritative
from app.indexer.guard import storage_paused
from app.indexer.seed_registry import CATEGORY_ORDER, SEED_ROWS
from app.engine.config import EngineSettings, get_engine_settings
from app import database
from app.database import execute_safe, get_client
from app.engine.store import get_store
from app.engine.text import (
    ExtractedDocument,
    detect_language,
    extract_links,
    fallback_document,
    normalise_whitespace,
    strip_artefacts,
    truncate,
    url_terms,
)
from app.engine.urls import (
    domain_class,
    infer_category,
    is_authoritative_indian,
    is_indexable,
    normalise_url,
    root_domain,
)

logger = logging.getLogger(__name__)

# The agents are the live path for every page: fetch, parse and quality all run
# through them, so the modular architecture is what actually executes.
def _low_level_fetch(client: Any, url: str, settings: Any) -> Any:
    """The pipeline's own fetch, resolved at call time so it stays stubbable."""
    return fetch_page_status(client, url, settings)


_fetcher_agent = FetcherAgent(fetch_fn=_low_level_fetch)
_parser_agent = ParserAgent()
_quality_agent = QualityAgent()
_indexer_agent = IndexerAgent()

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
    fetcher: FetcherAgent | None = None,
) -> tuple[SeedReport, list[ExtractedDocument]]:
    """Crawl one seed: its entry page, then same-host level-1 links."""
    report = SeedReport(category=seed.category)
    documents: list[ExtractedDocument] = []
    active_fetcher = fetcher or _fetcher_agent
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

        fetched = await active_fetcher.fetch(url)
        html, status = fetched.html, fetched.status
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
        parsed = await _parser_agent.parse(url, html, status=status, category=seed.category)
        document, reason = parsed.document, parsed.reason
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


def _basic_parse_document(
    url: str, html: str, status: int, settings: EngineSettings
) -> ExtractedDocument:
    """Minimal HTML-to-row parser with no agent quality gate."""
    if status >= 400:
        raise ValueError(f"HTTP status {status}")
    if not html or len(html.strip()) < 40:
        raise ValueError("response body is empty or too short")

    canonical = normalise_url(url)
    if not is_indexable(canonical):
        raise ValueError("URL is not indexable")
    host = urlparse(canonical).netloc
    if not host:
        raise ValueError("URL has no hostname")

    soup = BeautifulSoup(html, "html.parser")
    for node in soup(["script", "style", "noscript", "svg", "template"]):
        node.decompose()

    title = normalise_whitespace(soup.title.get_text(" ", strip=True)) if soup.title else ""
    if not title:
        first_heading = soup.find("h1")
        if first_heading is not None:
            title = normalise_whitespace(first_heading.get_text(" ", strip=True))
    title = truncate(strip_artefacts(title) or host.removeprefix("www."), settings.title_max_chars)

    meta = soup.find("meta", attrs={"name": lambda value: str(value or "").lower() == "description"})
    if meta is None:
        meta = soup.find("meta", attrs={"property": lambda value: str(value or "").lower() == "og:description"})
    description = strip_artefacts(str(meta.get("content") or "")) if meta else ""

    body_text = normalise_whitespace(soup.get_text(" ", strip=True))
    headings = normalise_whitespace(" ".join(
        node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2", "h3"])
    ))
    # Bound each field before fitting the total payload. The store remains the
    # authority on final per-page and per-domain limits.
    body_text = truncate(body_text, min(1200, max(200, settings.max_payload_bytes // 3)))
    headings = truncate(headings, 320)
    snippet = truncate(description or body_text, 220)
    category = infer_category(canonical)
    root = root_domain(canonical)

    document = ExtractedDocument(
        url=canonical,
        canonical_url=canonical,
        domain=host,
        root_domain=root,
        domain_class=domain_class(canonical),
        title=title,
        snippet=snippet,
        content=body_text,
        language=detect_language(body_text),
        favicon_url=f"https://{host}/favicon.ico",
        category=category,
        headings=headings or title,
        body_text=body_text,
        nav_terms="",
        site_terms=truncate(f"{root.replace('.', ' ')} {title}", 120),
        category_terms=category,
        url_terms=url_terms(canonical),
    )
    return document.fit_to_budget(settings.max_payload_bytes)


async def run_url_pass(
    *,
    batch_size: int = 10,
    settings: EngineSettings | None = None,
    store: object | None = None,
    fetcher: FetcherAgent | None = None,
    parser: ParserAgent | None = None,
    quality: QualityAgent | None = None,
    indexer: IndexerAgent | None = None,
) -> dict[str, object]:
    """Baseline ingestion only: fetch -> basic parse -> direct Turso store.

    The optional agent parameters remain for API compatibility but are
    intentionally not used by this isolated production path. Extra agents and
    quality gates must not sit between a valid fetched page and persistence.
    """
    cfg = settings or get_engine_settings()
    limit = max(1, min(10, int(batch_size)))
    report: dict[str, object] = {"attempted": 0, "processed": 0, "failed": 0}

    try:
        result = database._normalise_result(
            await database._execute(
                "SELECT rowid AS id, url, domain FROM crawl_queue "
                "WHERE status = 'pending' ORDER BY rowid LIMIT ?",
                [limit],
            )
        )
        rows = list(getattr(result, "rows", ()) or ())
    except Exception as exc:
        logger.exception("BASELINE queue poll failed (limit=%s): %s", limit, exc)
        report["error"] = str(exc)
        return report

    if not rows:
        report["empty"] = True
        return report

    from app.engine.store import get_store

    active_store = store or get_store()

    async def mark_failed(row_id: object, url: str, reason: str) -> None:
        try:
            await database._execute(
                "UPDATE crawl_queue SET status = 'failed' WHERE rowid = ?",
                [row_id],
            )
            logger.error(
                "BASELINE row failed rowid=%s url=%s reason=%s queue_status=failed",
                row_id, url, reason,
            )
        except Exception:
            logger.exception(
                "BASELINE could not mark rowid=%s failed (url=%s)", row_id, url
            )

    async with httpx.AsyncClient(
        timeout=cfg.crawl_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": cfg.crawl_user_agent},
    ) as client:
        for row in rows:
            row_id = row[0]
            raw_url = str(row[1] or "").strip()
            report["attempted"] = int(report["attempted"]) + 1

            try:
                if not raw_url:
                    raise ValueError("crawl_queue row has an empty URL")

                html, status = await fetch_page_status(client, raw_url, cfg)
                if html is None:
                    raise RuntimeError(
                        f"fetch returned no HTML (http_status={status!r})"
                    )

                document = _basic_parse_document(
                    raw_url, html, int(status or 200), cfg
                )
                outcome = await active_store.replace_documents(
                    [document.as_row()],
                    queue_rowid=int(row_id),
                )
                pages = int(outcome.get("pages", 0) or 0)
                if pages < 1:
                    raise RuntimeError(
                        "direct store wrote zero pages; "
                        f"outcome={outcome!r}; queue row was not marked processed"
                    )

                # replace_documents marks the queue row processed in the same
                # Turso batch as the page write. Confirm it instead of assuming.
                try:
                    check = database._normalise_result(
                        await database._execute(
                            "SELECT status FROM crawl_queue WHERE rowid = ?",
                            [row_id],
                        )
                    )
                    check_rows = list(getattr(check, "rows", ()) or ())
                    queue_status = str(check_rows[0][0]) if check_rows else "missing"
                except Exception:
                    # The page write and processed transition were in one atomic
                    # batch. A failed follow-up read must not reverse that success.
                    queue_status = "verification-unavailable"
                    logger.exception(
                        "page write committed for rowid=%s but queue status verification failed",
                        row_id,
                    )
                if queue_status not in ("processed", "verification-unavailable"):
                    raise RuntimeError(
                        f"page write returned {pages} but queue status is {queue_status!r}"
                    )

                report["processed"] = int(report["processed"]) + 1
                logger.info(
                    "BASELINE DB COMMIT SUCCESS rowid=%s url=%s pages=%s queue_status=%s",
                    row_id, raw_url, pages, queue_status,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                report["failed"] = int(report["failed"]) + 1
                logger.exception(
                    "BASELINE ingestion failed rowid=%s url=%s: %s",
                    row_id, raw_url, exc,
                )
                await mark_failed(row_id, raw_url, str(exc))

    return report


async def run_seed_pass(
    categories: list[str] | None = None,
    *,
    seeds: list[CategorySeed] | None = None,
    settings: EngineSettings | None = None,
    store: object | None = None,
) -> dict[str, object]:
    """Crawl every seed in the requested categories through the orchestrator.

    The seeds are pushed into the frontier agent, which owns the queue; the
    per-domain work (root, sitelinks, fallback) is the orchestrator's domain
    worker. There is no second crawl loop here any more.
    """
    from app.orchestrator import Orchestrator

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

    orchestrator = Orchestrator(store=index, settings=cfg)
    labels: dict[str, str] = {}
    seed_objects: dict[str, CategorySeed] = {}
    hosts: list[str] = []
    for seed in targets:
        host = _seed_domain(seed)
        await orchestrator.frontier.enqueue(host, reason="seed")
        labels[host] = seed.label
        seed_objects[host] = seed
        hosts.append(host)

    outcome = await orchestrator.run_domains(
        hosts,
        labels=labels,
        seed_objects=seed_objects,
        settings=cfg,
        concurrency=cfg.seed_crawl_concurrency,
    )
    if outcome.get("paused"):
        report["paused"] = True
        return report

    for host, seed_report in (outcome.get("reports") or {}).items():
        report["categories"][labels.get(host, host)] = seed_report
        report["fetched"] = int(report["fetched"]) + int(seed_report.get("fetched", 0))
        report["stored"] = int(report["stored"]) + int(seed_report.get("stored", 0))
        report["documents"] = int(report["documents"]) + int(seed_report.get("extracted", 0))

    report["sites"] = int(outcome.get("sites", 0))
    report["sitelinks"] = max(0, int(outcome.get("indexed_pages", 0)) - int(outcome.get("sites", 0)))
    logger.info(
        "seed pass complete: %s documents, %s stored, %s fetched (errors: %s)",
        report["documents"], report["stored"], report["fetched"], outcome.get("errors", {}),
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
    """Drain the discovered-domain backlog through the orchestrator.

    Ten domains at a time, a polite pause between batches, and a hard ceiling on
    the number of indexed sites. Stops when the frontier is empty.
    """
    from app.orchestrator import Orchestrator

    cfg = settings or get_engine_settings()
    index = store or get_store()
    batch = max(1, batch_size or cfg.discovery_batch_size)
    report: dict[str, object] = {
        "batches": 0, "attempted": 0, "stored": 0, "skipped": 0, "ceiling": False
    }

    if await storage_paused(index, cfg):
        report["paused"] = True
        return report

    sites = await index.count_documents()
    if sites >= cfg.max_indexed_sites:
        logger.info("discovery pass idle: %d sites at the ceiling of %d", sites, cfg.max_indexed_sites)
        report["ceiling"] = True
        return report

    orchestrator = Orchestrator(store=index, settings=cfg)
    seen_hosts: set[str] = set()
    max_batches = max(1, cfg.discovery_max_batches_per_pass)

    while sites < cfg.max_indexed_sites and int(report["batches"]) < max_batches:
        hosts = await orchestrator.frontier.next_batch(batch)
        if not hosts:
            logger.info("discovery pass: queue empty, nothing to do")
            break

        # A host that comes back in a later batch was not marked done; never let
        # that turn the pass into a spin against the database.
        fresh = [host for host in hosts if host not in seen_hosts]
        if not fresh:
            logger.warning(
                "discovery pass: the same %d domains came back — closing them and stopping",
                len(hosts),
            )
            for host in hosts:
                await orchestrator.frontier.mark_done(host, status="processed")
            break
        seen_hosts.update(fresh)
        hosts = fresh

        report["batches"] = int(report["batches"]) + 1
        outcome = await orchestrator.run_domains(hosts, settings=cfg)
        if outcome.get("paused"):
            report["paused"] = True
            break

        attempted = int(outcome.get("domains", 0))
        report["attempted"] = int(report["attempted"]) + attempted
        report["stored"] = int(report["stored"]) + int(outcome.get("indexed_pages", 0))
        report["skipped"] = int(report["skipped"]) + max(0, attempted - int(outcome.get("accepted", 0)))

        for host in hosts:
            seed_report = (outcome.get("reports") or {}).get(host, {})
            status = "processed" if seed_report.get("stored") else "failed"
            await orchestrator.frontier.mark_done(host, status=status)
            logger.info(
                "discovery indexed %s: %d pages (sites now %d)",
                host, int(seed_report.get("stored", 0)), await index.count_documents(),
            )

        sites = await index.count_documents()
        stored_now = int(outcome.get("indexed_pages", 0))
        logger.info(
            "discovery batch %s done: %s attempted, %s pages stored, %s sites",
            report["batches"], report["attempted"], report["stored"], sites,
        )
        if sites >= cfg.max_indexed_sites:
            report["ceiling"] = True
            break
        if stored_now:
            await asyncio.sleep(max(0.0, cfg.discovery_batch_pause_seconds))
        else:
            # Nothing landed: back off properly instead of polling again at once.
            backoff = max(1.0, cfg.worker_idle_backoff_seconds)
            logger.info("discovery pass: no pages stored this batch, sleeping %.0fs", backoff)
            await asyncio.sleep(backoff)

    return report


def _seed_domain(seed: CategorySeed) -> str:
    return urlparse(seed.url).netloc.lower().removeprefix("www.")


async def refill_frontier(
    *,
    target: int | None = None,
    store: object | None = None,
    settings: EngineSettings | None = None,
) -> dict[str, object]:
    """Top the frontier up so discovery always has work.

    Sources, in order: registry seeds that are not indexed yet, then the sitelink
    URLs already stored (a cheap way to keep the buffer warm without inventing
    URLs), then nothing — the queue simply stays short until discovery finds
    more. Returns the buffer depth so the caller can log it.
    """
    from app.orchestrator import Orchestrator

    cfg = settings or get_engine_settings()
    index = store or get_store()
    wanted = max(1, target or cfg.frontier_buffer_target)
    orchestrator = Orchestrator(store=index, settings=cfg)
    frontier = orchestrator.frontier

    before = await frontier.queue_depth()
    # A thin queue is an emergency: harvest harder, not just top up.
    aggressive = before < max(0, cfg.frontier_refill_trigger)
    host_budget = (
        max(1, cfg.harvest_hosts_per_pass_aggressive) if aggressive
        else max(1, cfg.harvest_hosts_per_pass)
    )
    if aggressive:
        logger.info("frontier low (%d < %d): aggressive sitemap harvest", before, cfg.frontier_refill_trigger)
    queued_seeds = 0
    queued_links = 0
    harvested_total = 0

    for seed in seeds_for(None):
        if await frontier.queue_depth() >= wanted:
            break
        host = _seed_domain(seed)
        if await frontier.enqueue(host, reason="seed-refill"):
            queued_seeds += 1

    # Sitemaps next: a domain that publishes one tells us its canonical pages,
    # which beats recycling URLs we already hold.
    if await frontier.queue_depth() < wanted:
        try:
            harvested, hosts_used = 0, 0
            result = await database.execute_safe(
                "SELECT domain FROM sites WHERE category IS NOT NULL "
                "ORDER BY COALESCE(domain, '') ASC LIMIT ?",
                [max(1, host_budget) * 6],
            )
            hosts = [str(row[0]) for row in (getattr(result, "rows", ()) or ()) if row and row[0]]
            for host in hosts:
                if await frontier.queue_depth() >= wanted:
                    break
                if hosts_used >= host_budget:
                    break
                urls = await frontier.harvest(host)
                if not urls:
                    continue
                hosts_used += 1
                for url in urls:
                    if await frontier.queue_depth() >= wanted:
                        break
                    if await index.url_is_known(url):
                        continue
                    await index.queue_url(url, domain=host, reason="sitemap")
                    # the discovery pass works in domains, so the host joins the
                    # domain backlog too — otherwise the queue fills and nothing consumes it
                    try:
                        await index.enqueue_domain(host, reason="sitemap")
                    except Exception:
                        pass
                    harvested += 1
            harvested_total = harvested
            if harvested:
                logger.info("frontier refill: %d sitemap urls from %d domains", harvested, hosts_used)
        except Exception as exc:
            logger.warning("frontier refill from sitemaps failed: %s", exc)

    if await frontier.queue_depth() < wanted:
        try:
            result = await database.execute_safe(
                "SELECT p.url FROM pages p WHERE p.kind = 'sitelink' "
                "ORDER BY COALESCE(p.last_crawled_at, 0) ASC LIMIT ?",
                [max(0, wanted - await frontier.queue_depth()) * 2],
            )
            for row in getattr(result, "rows", ()) or ():
                if await frontier.queue_depth() >= wanted:
                    break
                url = str(row[0])
                host = urlparse(url).netloc.lower().removeprefix("www.")
                if not host:
                    continue
                if await frontier.enqueue(host, reason="link-refill"):
                    queued_links += 1
        except Exception as exc:
            logger.warning("frontier refill from stored links failed: %s", exc)

    after = await frontier.queue_depth()
    logger.info(
        "frontier refill: %d → %d queued (%d seeds, %d sitemap urls, %d stored links, target %d)",
        before, after, queued_seeds, harvested_total, queued_links, wanted,
    )
    return {
        "before": before,
        "aggressive": aggressive,
        "after": after,
        "seeds": queued_seeds,
        "sitemap": harvested_total,
        "links": queued_links + harvested_total,
        "target": wanted,
    }


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
    parser.add_argument("--refill", action="store_true", help="top the discovery queue up to the buffer target")
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
        if args.refill:
            refill = await refill_frontier(settings=engine_settings)
            print(refill)
            await database.close()
            return

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
    "run_url_pass",
    "run_seed_pass",
]
