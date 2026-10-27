"""The pipeline: one shared HTTP client, six agents, one batch loop.

`Orchestrator` is the only place the agents meet. It owns the pooled
`httpx.AsyncClient`, pulls a batch of ten URLs from the frontier, runs them
through fetch → parse → quality → index, and closes or fails each queue row as
its own outcome dictates.

The counters it reports are honest by construction: `pages` comes from the
store's confirmed commit, never from the number of documents it handed over.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Sequence

import httpx

from app.agents import load_agent
from app.engine.config import Settings, get_settings
from app.engine.store import IndexStore, PageDocument, QueuedUrl
from app.indexer.guard import StorageGuard
from app.libsql_http import LibsqlClient

logger = logging.getLogger(__name__)

#: When the frontier is dry and nothing can be re-armed yet, back off longer
#: than the ordinary idle sleep so the worker is not polling the database
#: every 15 seconds for the whole of a six-hour cooldown.
IDLE_REFILL_FAILED_BACKOFF_SECONDS = 60.0

_frontier_module = load_agent("01_frontier_agent")
_fetcher_module = load_agent("02_fetcher_agent")
_parser_module = load_agent("03_parser_agent")
_quality_module = load_agent("04_quality_agent")
_indexer_module = load_agent("05_indexer_agent")
_revalidation_module = load_agent("06_revalidation_agent")
_classifier_module = load_agent("07_classifier_agent")
_language_module = load_agent("08_language_agent")

FrontierAgent = _frontier_module.FrontierAgent
FetcherAgent = _fetcher_module.FetcherAgent
ParserAgent = _parser_module.ParserAgent
QualityAgent = _quality_module.QualityAgent
IndexerAgent = _indexer_module.IndexerAgent
RevalidationAgent = _revalidation_module.RevalidationAgent
ClassifierAgent = _classifier_module.ClassifierAgent
LanguageAgent = _language_module.LanguageAgent


@dataclass
class BatchOutcome:
    """What one batch of URLs achieved."""

    attempted: int = 0
    fetched: int = 0
    parsed: int = 0
    accepted: int = 0
    rejected: int = 0
    indexed_pages: int = 0
    queue_closed: int = 0
    queue_failed: int = 0
    skipped: int = 0
    discovered: int = 0
    harvested: int = 0
    sitemap_domains_scheduled: int = 0
    paused: bool = False
    skip_reasons: dict[str, int] = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def empty(self) -> bool:
        return self.attempted == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "attempted": self.attempted,
            "fetched": self.fetched,
            "parsed": self.parsed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "indexed_pages": self.indexed_pages,
            "queue_closed": self.queue_closed,
            "queue_failed": self.queue_failed,
            "skipped": self.skipped,
            "discovered": self.discovered,
            "harvested": self.harvested,
            "sitemap_domains_scheduled": self.sitemap_domains_scheduled,
            "paused": self.paused,
            "skip_reasons": dict(self.skip_reasons),
            "elapsed_ms": self.elapsed_ms,
        }


class Orchestrator:
    """Wires the agents together over one client and one store."""

    def __init__(
        self,
        client: LibsqlClient,
        settings: Settings | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._store = IndexStore(client, self._settings)
        self._guard = StorageGuard(
            client,
            limit=self._settings.storage_page_limit,
            ttl_seconds=self._settings.storage_count_ttl_seconds,
        )
        self._fetcher = FetcherAgent(self._settings, transport=transport)
        self._parser = ParserAgent()
        self._quality = QualityAgent()
        self._classifier = ClassifierAgent()
        self._language = LanguageAgent()
        self._indexer = IndexerAgent(self._store, self._guard)
        self._frontier = FrontierAgent(
            self._store, self._settings, client=self._fetcher.client
        )
        self._revalidation = RevalidationAgent(
            self._store,
            self._fetcher,
            self._settings,
            parser=self._parser,
        )
        self.batches = 0
        self.pages_indexed = 0
        self._pending_sitemap_domains: set[str] = set()
        self._sitemap_harvest_task: asyncio.Task[None] | None = None

    # -- accessors ---------------------------------------------------------

    @property
    def store(self) -> IndexStore:
        return self._store

    @property
    def guard(self) -> StorageGuard:
        return self._guard

    @property
    def frontier(self) -> object:
        return self._frontier

    @property
    def fetcher(self) -> object:
        return self._fetcher

    @property
    def revalidation(self) -> object:
        return self._revalidation

    # -- one batch ---------------------------------------------------------

    async def run_batch(self, limit: int | None = None) -> BatchOutcome:
        """Fetch, parse, gate and store one batch of queued URLs."""
        started = time.monotonic()
        outcome = BatchOutcome()

        queue: list[QueuedUrl] = await self._frontier.next_batch(limit)
        if not queue:
            return outcome

        outcome.attempted = len(queue)

        # One storage read for the whole batch — never one per URL.
        state = await self._guard.storage_state()
        if state.paused:
            outcome.paused = True
            logger.warning("batch skipped, storage paused: %s", state.reason)
            return outcome

        documents: list[PageDocument] = []
        queue_ids: list[int] = []
        failed_ids: list[int] = []
        skipped_ids: list[int] = []
        discovered: list[str] = []

        # Per-domain page cap: one count query for the whole batch, and URLs on
        # a domain that is already full are parked before any fetch happens.
        cap = self._settings.max_pages_per_domain
        counts = await self._store.domain_page_counts([item.domain for item in queue])
        eligible: list[QueuedUrl] = []
        for item in queue:
            if counts.get(item.domain, 0) >= cap:
                outcome.skipped += 1
                self._tally(outcome.skip_reasons, "domain_cap")
                skipped_ids.append(item.rowid)
            else:
                eligible.append(item)

        self._quality.reset()
        results = await self._fetcher.fetch_many([item.url for item in eligible])

        for item, result in zip(eligible, results):
            # A domain can fill up part-way through a batch.
            if counts.get(item.domain, 0) >= cap:
                outcome.skipped += 1
                self._tally(outcome.skip_reasons, "domain_cap")
                skipped_ids.append(item.rowid)
                continue

            if not result.ok:
                outcome.rejected += 1
                self._tally(outcome.skip_reasons, result.reason or "fetch_failed")
                failed_ids.append(item.rowid)
                continue

            outcome.fetched += 1
            parsed = self._parser.parse(item.url, result.html)
            if not parsed.ok:
                outcome.rejected += 1
                self._tally(outcome.skip_reasons, parsed.reason or "parse_failed")
                failed_ids.append(item.rowid)
                continue

            outcome.parsed += 1

            # Every page that parsed contributes its outbound links, whether
            # or not it passed the gate: a thin page can still point at good
            # ones. The frontier applies the crawl-scope rules.
            discovered.extend(
                self._frontier.filter_links(
                    parsed.links,
                    item.url,
                    allow_global=self._settings.allow_global_domains,
                )
            )

            document = PageDocument(
                url=item.url,
                domain=item.domain,
                title=parsed.title,
                description=parsed.description,
                body_text=parsed.body_text,
            )
            verdict = self._quality.check(document)
            if not verdict.ok:
                outcome.rejected += 1
                self._tally(outcome.skip_reasons, verdict.reason or "rejected")
                failed_ids.append(item.rowid)
                continue

            document.digest = verdict.digest
            document.quality_score = verdict.score

            # Tagged here, at ingestion, so a search can filter without a
            # backfill later.
            document.category = self._classifier.classify(
                item.url,
                title=document.title,
                description=document.description,
                body_text=document.body_text,
            ).category
            document.language = self._language.detect(
                f"{document.title} {document.body_text}"
            )

            documents.append(document)
            queue_ids.append(item.rowid)
            counts[item.domain] = counts.get(item.domain, 0) + 1

        outcome.accepted = len(documents)

        if failed_ids or skipped_ids:
            failed, _skipped = await self._indexer.park(failed_ids, skipped_ids)
            outcome.queue_failed = failed
            # Park the rest of the backlog for those domains too, so the depth
            # reflects real work and new domains are not dropped at the cap.
            for domain in {item.domain for item in queue if item.rowid in set(skipped_ids)}:
                await self._store.skip_pending_for_domain(domain)

        if documents:
            index_outcome = await self._indexer.persist(documents, queue_ids)
            if index_outcome.committed:
                # Only a confirmed commit may move the counters.
                outcome.indexed_pages = index_outcome.pages_written
                outcome.queue_closed = index_outcome.queue_closed
                self.pages_indexed += index_outcome.counted
                self.batches += 1
            else:
                outcome.paused = index_outcome.paused
                logger.warning("batch not persisted: %s", index_outcome.reason)

        if discovered and not outcome.paused:
            # Do not grow the frontier with URLs whose domain is already full.
            discovered = [
                url
                for url in discovered
                if counts.get(_frontier_module.domain_of(url), 0) < cap
            ]

        if discovered and not outcome.paused:
            # Queue after the commit, so a failed write never grows the frontier.
            outcome.discovered = await self._frontier.enqueue(discovered)

            # A newly discovered authoritative domain gets its sitemap read now
            # rather than waiting for the frontier to run dry, which on a busy
            # queue never happens. The per-domain cooldown inside
            # harvest_sitemap() is what stops this running on every batch.
            new_domains = tuple(
                {
                    domain
                    for domain in (
                        _frontier_module.domain_of(url) for url in discovered
                    )
                    if domain and _frontier_module.is_authoritative(domain)
                }
            )
            if new_domains:
                outcome.sitemap_domains_scheduled = len(new_domains)
                self._schedule_sitemap_harvest(new_domains)

        outcome.elapsed_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "batch done: %d queued, %d fetched, %d accepted, %d rejected, "
            "%d skipped, %d indexed, %d discovered, %d sitemap domains scheduled, "
            "queue_closed=%d queue_failed=%d paused=%s skip_reasons=%s (%dms)",
            outcome.attempted,
            outcome.fetched,
            outcome.accepted,
            outcome.rejected,
            outcome.skipped,
            outcome.indexed_pages,
            outcome.discovered,
            outcome.sitemap_domains_scheduled,
            outcome.queue_closed,
            outcome.queue_failed,
            outcome.paused,
            outcome.skip_reasons,
            outcome.elapsed_ms,
        )
        return outcome

    # -- background sitemap discovery ------------------------------------

    def _schedule_sitemap_harvest(self, domains: Sequence[str]) -> None:
        """Schedule sitemap work without making page indexing wait for it."""
        self._pending_sitemap_domains.update(domain for domain in domains if domain)
        task = self._sitemap_harvest_task
        if task is None or task.done():
            self._sitemap_harvest_task = asyncio.create_task(self._drain_sitemap_harvests())
        logger.info(
            "sitemap harvest scheduled: domains=%d pending_domains=%d",
            len(domains),
            len(self._pending_sitemap_domains),
        )

    async def _drain_sitemap_harvests(self) -> None:
        """Drain accumulated sitemap work; the frontier bounds network concurrency."""
        while self._pending_sitemap_domains:
            domains = tuple(sorted(self._pending_sitemap_domains))
            self._pending_sitemap_domains.clear()
            try:
                queued = await self._frontier.harvest_sitemap(domains, force=True)
                logger.info(
                    "background sitemap harvest finished: domains=%d urls_queued=%d",
                    len(domains),
                    queued,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("background sitemap harvest failed for %d domains", len(domains))

    # -- the loop ----------------------------------------------------------

    async def run_forever(self) -> None:
        """Consume the frontier until cancelled.

        2.0 s between batches; a 15.0 s non-blocking backoff when the queue is
        empty, so an idle deployment costs nothing and still recovers the
        moment there is work.
        """
        while True:
            # The whole iteration is guarded, not just the batch. `refill()`
            # does network and database work of its own, and an exception from
            # it used to escape this loop and end the worker for good — which
            # is indistinguishable from a crawl that has simply stopped.
            try:
                outcome = await self.run_batch()

                if outcome is not None and not outcome.empty and not outcome.paused:
                    await asyncio.sleep(self._settings.crawl_batch_sleep_seconds)
                    continue

                delay = self._settings.crawl_empty_backoff_seconds

                # Every pass, not only an empty one: the queue can be full of
                # work from a single domain while every authoritative one sits
                # idle.
                added = await self._frontier.refill(minimum=1)
                if outcome is not None and outcome.empty and not added:
                    # Nothing to crawl and nothing past its cooldown yet.
                    delay = max(delay, IDLE_REFILL_FAILED_BACKOFF_SECONDS)

                logger.debug("crawl worker sleeping %.1fs (queue empty)", delay)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception("crawl loop error, continuing: %s", error)
                await asyncio.sleep(IDLE_REFILL_FAILED_BACKOFF_SECONDS)

    async def run_domains(self, domains: Sequence[str]) -> BatchOutcome:
        """Seed specific domains and process exactly one batch of them."""
        await self._frontier.enqueue([f"https://{domain}/" for domain in domains])
        return await self.run_batch(limit=len(domains) or self._settings.crawl_batch_size)

    async def close(self) -> None:
        task = self._sitemap_harvest_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self._fetcher.aclose()

    @staticmethod
    def _tally(counter: dict[str, int], reason: str) -> None:
        counter[reason] = counter.get(reason, 0) + 1


__all__ = ["BatchOutcome", "Orchestrator"]
